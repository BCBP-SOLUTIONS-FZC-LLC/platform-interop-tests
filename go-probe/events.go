package main

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-events/pkg/events"
	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-events/pkg/outbox"
	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-pgcommon/pkg/migrate"
	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-pgcommon/pkg/pgcommon"
	"github.com/jackc/pgx/v5"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/baggage"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

// The events-* subcommands publish and consume real messages through SNS/SQS (floci)
// with platform-events, recording what was sent and what a handler saw. python-probe has
// the same subcommands with the same JSON shapes; compare.py checks each direction
// (Go publishes → Python consumes, and the reverse). See README.md § events-wire-check.

type eventCase struct {
	Key       string          `json:"key"`
	EventType string          `json:"event_type"`
	Source    string          `json:"source"`
	Payload   json.RawMessage `json:"payload"`
	Opts      map[string]any  `json:"opts"`
	Group     string          `json:"group"`
	SkipFIFO  bool            `json:"skip_fifo"`
	// PayloadBlobBytes > 0 makes the payload {"blob":"xxx…"} of that many x's (kept out of
	// the fixture file).
	PayloadBlobBytes int `json:"payload_blob_bytes"`
}

type sentEvent struct {
	Key             string          `json:"key"`
	Group           string          `json:"group"`
	Envelope        json.RawMessage `json:"envelope"`
	ProducerTraceID string          `json:"producer_trace_id"`
	Signature       string          `json:"signature,omitempty"`
}

type receivedEvent struct {
	Envelope       json.RawMessage   `json:"envelope"`
	PayloadRaw     string            `json:"payload_raw"`
	TraceIDCtx     string            `json:"trace_id_ctx"`
	TenantGUC      string            `json:"tenant_guc"`
	HasTenantGUC   bool              `json:"has_tenant_guc"`
	Attributes     map[string]string `json:"attributes"`
	Baggage        map[string]string `json:"baggage"`
	HMACOK         *bool             `json:"hmac_ok,omitempty"`
	LinkedTraceIDs []string          `json:"linked_trace_ids"`
	spanID         trace.SpanID
}

const signatureAttribute = "XSignature"

// glueEncodeCodec writes the AWS Glue Schema Registry wire format (version 0x03,
// compression 0x00, 16-byte schema version UUID, payload); decoding is the library's.
type glueEncodeCodec struct{ events.GlueDecodeCodec }

func (glueEncodeCodec) Encode(_ context.Context, _ string, payload json.RawMessage) ([]byte, string, error) {
	var id [16]byte
	_, _ = rand.Read(id[:])
	id[6] = (id[6] & 0x0f) | 0x40
	id[8] = (id[8] & 0x3f) | 0x80
	out := append([]byte{0x03, 0x00}, id[:]...)
	return append(out, payload...), fmt.Sprintf("%x-%x-%x-%x-%x", id[0:4], id[4:6], id[6:8], id[8:10], id[10:16]), nil
}

// eventsTracing installs an SDK tracer provider (recording spans, so consumer span links
// can be read back) and the W3C trace-context + baggage propagators, and returns a context
// carrying the producer's baggage.
func eventsTracing() (*tracetest.SpanRecorder, *sdktrace.TracerProvider, context.Context) {
	recorder := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(recorder))
	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	ctx := context.Background()
	if m, err := baggage.NewMember("probe.lang", "go"); err == nil {
		if b, err := baggage.New(m); err == nil {
			ctx = baggage.ContextWithBaggage(ctx, b)
		}
	}
	return recorder, tp, ctx
}

func envelopeOptions(o map[string]any) []events.EnvelopeOpt {
	str := func(k string) string { s, _ := o[k].(string); return s }
	var opts []events.EnvelopeOpt
	add := func(v string, opt func(string) events.EnvelopeOpt) {
		if v != "" {
			opts = append(opts, opt(v))
		}
	}
	add(str("tenant_id"), events.WithTenantID)
	if b, _ := o["system_tenant"].(bool); b {
		opts = append(opts, events.WithSystemTenant())
	}
	add(str("trace_id"), events.WithTraceID)
	add(str("correlation_id"), events.WithCorrelationID)
	add(str("specversion"), events.WithSchemaVersion)
	add(str("subject"), events.WithSubject)
	add(str("actor"), events.WithActor)
	add(str("ip_address"), events.WithIPAddress)
	add(str("user_agent"), events.WithUserAgent)
	return opts
}

func runEventsPublish(args []string) error {
	fs := flag.NewFlagSet("events-publish", flag.ContinueOnError)
	topic := fs.String("topic", "", "SNS topic ARN")
	casesPath := fs.String("cases", "", "events_wire_cases.json")
	mode := fs.String("mode", "single", "single|batch")
	codec := fs.String("codec", "none", "none|glue")
	hmacKey := fs.String("hmac", "", "sign each event and send the signature as an attribute")
	fifo := fs.Bool("fifo", false, "FIFO topic: group by each case's group")
	endpoint := fs.String("endpoint", "", "AWS endpoint URL")
	if err := fs.Parse(args); err != nil {
		return err
	}
	raw, err := os.ReadFile(*casesPath)
	if err != nil {
		return err
	}
	var cases []eventCase
	if err := json.Unmarshal(raw, &cases); err != nil {
		return err
	}
	_, tp, ctx := eventsTracing()

	groups := map[string]string{}
	var envs []events.Envelope[json.RawMessage]
	var sent []sentEvent
	for _, c := range cases {
		if *fifo && c.SkipFIFO {
			continue
		}
		payload := c.Payload
		if c.PayloadBlobBytes > 0 {
			payload = json.RawMessage(`{"blob":"` + strings.Repeat("x", c.PayloadBlobBytes) + `"}`)
		}
		env := events.NewEnvelope[json.RawMessage](c.EventType, c.Source, payload, envelopeOptions(c.Opts)...)
		groups[env.ID] = c.Group
		envs = append(envs, env)
		b, err := json.Marshal(env)
		if err != nil {
			return err
		}
		sent = append(sent, sentEvent{Key: c.Key, Group: c.Group, Envelope: b})
	}
	newPublisher := func(attrs map[string]string) (events.Publisher, error) {
		var opts []events.PublisherOption
		if *fifo {
			opts = append(opts, events.WithMessageGroupID(func(e events.Envelope[json.RawMessage]) string { return groups[e.ID] }))
		}
		if *codec == "glue" {
			opts = append(opts, events.WithCodec(glueEncodeCodec{}))
		}
		if attrs != nil {
			opts = append(opts, events.WithAttributes(attrs))
		}
		return events.NewSNSPublisher(events.SNSConfig{TopicARN: *topic, Region: "us-east-1", EndpointURL: *endpoint}, opts...)
	}
	tracer := tp.Tracer("go-probe")
	if *mode == "batch" {
		pub, err := newPublisher(nil)
		if err != nil {
			return err
		}
		sctx, span := tracer.Start(ctx, "probe.publish_batch")
		err = pub.PublishBatch(sctx, envs)
		span.End()
		if err != nil {
			return err
		}
		for i := range sent {
			sent[i].ProducerTraceID = span.SpanContext().TraceID().String()
		}
	} else {
		for i, env := range envs {
			var attrs map[string]string
			if *hmacKey != "" {
				sig, err := events.SignEnvelope([]byte(*hmacKey), env)
				if err != nil {
					return err
				}
				sent[i].Signature = sig
				attrs = map[string]string{signatureAttribute: sig}
			}
			pub, err := newPublisher(attrs)
			if err != nil {
				return err
			}
			sctx, span := tracer.Start(ctx, "probe.publish")
			err = pub.Publish(sctx, env)
			span.End()
			if err != nil {
				return err
			}
			sent[i].ProducerTraceID = span.SpanContext().TraceID().String()
		}
	}
	return printJSON(map[string]any{"language": "go", "sent": sent})
}

func runEventsConsume(args []string) error {
	fs := flag.NewFlagSet("events-consume", flag.ContinueOnError)
	queue := fs.String("queue", "", "SQS queue URL")
	n := fs.Int("n", 0, "stop after this many handled messages")
	codec := fs.String("codec", "none", "none|glue")
	hmacKey := fs.String("hmac", "", "verify each event's signature attribute")
	timeout := fs.Duration("timeout", 60*time.Second, "stop after this long")
	endpoint := fs.String("endpoint", "", "AWS endpoint URL")
	if err := fs.Parse(args); err != nil {
		return err
	}
	recorder, _, ctx := eventsTracing()

	var mu sync.Mutex
	var got []*receivedEvent
	done := make(chan struct{})
	handler := func(hctx context.Context, env events.Envelope[json.RawMessage]) error {
		r := &receivedEvent{PayloadRaw: string(env.Payload), TraceIDCtx: events.TraceIDFromContext(hctx), Baggage: map[string]string{}}
		var err error
		if r.Envelope, err = json.Marshal(env); err != nil {
			return err
		}
		if g, ok := pgcommon.GUCSetFromContext(hctx); ok {
			r.TenantGUC, r.HasTenantGUC = g.TenantID, true
		}
		for _, m := range baggage.FromContext(hctx).Members() {
			r.Baggage[m.Key()] = m.Value()
		}
		if src, ok := events.SourceMessageFromContext(hctx); ok {
			r.Attributes = src.Attributes
			if *hmacKey != "" {
				valid, err := events.VerifyEnvelope([]byte(*hmacKey), env, src.Attributes[signatureAttribute])
				ok := valid && err == nil
				r.HMACOK = &ok
			}
		}
		r.spanID = trace.SpanFromContext(hctx).SpanContext().SpanID()
		mu.Lock()
		defer mu.Unlock()
		got = append(got, r)
		if len(got) == *n {
			close(done)
		}
		return nil
	}
	opts := []events.ConsumerOption{events.WithVisibilityTimeout(30 * time.Second)}
	if *codec == "glue" {
		opts = append(opts, events.WithConsumerCodec(events.GlueDecodeCodec{}))
	}
	consumer, err := events.NewSQSConsumer(events.SQSConfig{QueueURL: *queue, Region: "us-east-1", EndpointURL: *endpoint, WaitSeconds: 1}, handler, opts...)
	if err != nil {
		return err
	}
	go func() { _ = consumer.Start(ctx) }()
	select {
	case <-done:
	case <-time.After(*timeout):
	}
	if err := consumer.Stop(); err != nil {
		return err
	}
	links := map[trace.SpanID][]string{}
	for _, s := range recorder.Ended() {
		for _, l := range s.Links() {
			links[s.SpanContext().SpanID()] = append(links[s.SpanContext().SpanID()], l.SpanContext.TraceID().String())
		}
	}
	mu.Lock()
	defer mu.Unlock()
	for _, r := range got {
		r.LinkedTraceIDs = links[r.spanID]
	}
	return printJSON(map[string]any{"language": "go", "received": got})
}

// runEventsOutboxEnqueue applies the outbox schema and enqueues --count events (even ones
// under one ordering key) in their own transactions; prints their IDs in enqueue order.
func runEventsOutboxEnqueue(args []string) error {
	fs := flag.NewFlagSet("events-outbox-enqueue", flag.ContinueOnError)
	dsn := fs.String("dsn", "", "Postgres DSN (DDL privileges: applies the schema)")
	count := fs.Int("count", 5, "")
	key := fs.String("ordering-key", "order/go", "")
	if err := fs.Parse(args); err != nil {
		return err
	}
	ctx := context.Background()
	if err := outbox.ApplySchema(ctx, &migrate.Runner{DSN: *dsn}); err != nil {
		return err
	}
	pool, err := pgcommon.NewPool(ctx, pgcommon.Config{DSN: *dsn})
	if err != nil {
		return err
	}
	defer pool.Close()
	var ids, ordered []string
	for i := 0; i < *count; i++ {
		env := events.NewEnvelope[json.RawMessage]("interop.outbox", "go-probe",
			json.RawMessage(fmt.Sprintf(`{"n": %d, "f": 100.50, "s": "<&>"}`, i)), events.WithTenantID("acme"))
		err := pgcommon.RunInTx(ctx, pool, pgx.TxOptions{}, func(ctx context.Context, tx pgx.Tx) error {
			if i%2 == 0 {
				return outbox.EnqueueOrdered(ctx, tx, env, *key)
			}
			return outbox.Enqueue(ctx, tx, env)
		})
		if err != nil {
			return err
		}
		ids = append(ids, env.ID)
		if i%2 == 0 {
			ordered = append(ordered, env.ID)
		}
	}
	return printJSON(map[string]any{"language": "go", "ids": ids, "ordered_ids": ordered})
}

// runEventsOutboxRun runs the outbox runner with an SNS publisher for --seconds.
func runEventsOutboxRun(args []string) error {
	fs := flag.NewFlagSet("events-outbox-run", flag.ContinueOnError)
	dsn := fs.String("dsn", "", "Postgres DSN")
	topic := fs.String("topic", "", "SNS topic ARN")
	endpoint := fs.String("endpoint", "", "AWS endpoint URL")
	seconds := fs.Duration("for", 4*time.Second, "")
	if err := fs.Parse(args); err != nil {
		return err
	}
	ctx := context.Background()
	pool, err := pgcommon.NewPool(ctx, pgcommon.Config{DSN: *dsn})
	if err != nil {
		return err
	}
	defer pool.Close()
	pub, err := events.NewSNSPublisher(events.SNSConfig{TopicARN: *topic, Region: "us-east-1", EndpointURL: *endpoint})
	if err != nil {
		return err
	}
	runner, err := outbox.NewRunner(outbox.Config{Pool: pool, Publisher: pub, PollInterval: 100 * time.Millisecond})
	if err != nil {
		return err
	}
	rctx, cancel := context.WithTimeout(ctx, *seconds)
	defer cancel()
	_ = runner.Start(rctx)
	return printJSON(map[string]any{"language": "go", "ran": true})
}
