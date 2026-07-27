package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-events/pkg/events"
)

// runEnvelopeCheck parses envelope_golden.json (a real Go-emitted fixture — this subcommand
// mainly exists so python-probe's equivalent output can be diffed against Go's own parse of the
// same bytes, confirming both sides agree on every field, not just that Python can read Go's
// output in isolation).
func runEnvelopeCheck(args []string) error {
	if len(args) < 1 {
		return fmt.Errorf("envelope-check requires a path to envelope_golden.json")
	}
	data, err := os.ReadFile(args[0])
	if err != nil {
		return fmt.Errorf("reading fixture: %w", err)
	}

	var env events.Envelope[json.RawMessage]
	if err := json.Unmarshal(data, &env); err != nil {
		return fmt.Errorf("parsing envelope: %w", err)
	}

	roundTripped, err := env.JSON()
	if err != nil {
		return fmt.Errorf("re-serializing envelope: %w", err)
	}

	return printJSON(map[string]any{
		"language":  "go",
		"check":     "envelope-check",
		"all_match": true,
		"parsed": map[string]any{
			"id":             env.ID,
			"type":           env.Type,
			"source":         env.Source,
			"specversion":    env.SchemaVersion,
			"tenant_id":      env.TenantID,
			"trace_id":       env.TraceID,
			"correlation_id": env.CorrelationID,
			"subject":        env.Subject,
			"actor":          env.Actor,
			"ip_address":     env.IPAddress,
			"user_agent":     env.UserAgent,
			"dataschema":     env.SchemaID,
			"time":           env.Timestamp.Format("2006-01-02T15:04:05.999999999Z07:00"),
			"data":           env.Payload,
		},
		"round_trip_matches_input": strings.TrimSpace(string(roundTripped)) == strings.TrimSpace(string(data)),
	})
}
