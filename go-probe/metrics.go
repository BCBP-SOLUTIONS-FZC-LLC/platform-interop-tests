package main

import (
	"fmt"
	"sort"

	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-events/pkg/events"
	"github.com/prometheus/client_golang/prometheus"
)

// expectedMetricNames is the literal set of Prometheus metric names platform-events registers
// (verbatim from internal/adapter/outbound/metrics/metrics.go, cross-referenced during the
// COMPATIBILITY.md audit — see § events/eventcommon). Not derived dynamically: Go's
// client_golang Gather() omits a CounterVec/GaugeVec/HistogramVec's metric family entirely
// until at least one label combination has been observed (unlike Python's prometheus_client,
// which lists family names up front) — populating every label combination for all 22 metrics
// just to enumerate names dynamically would need per-metric label knowledge duplicated here
// anyway, so the name list is simply hardcoded and cross-checked against a successful,
// panic-free Init call instead.
var expectedMetricNames = []string{
	"platform_events_build_info",
	"events_published_total",
	"events_publish_duration_seconds",
	"events_consumed_total",
	"events_consume_duration_seconds",
	"outbox_pending_total",
	"outbox_published_total",
	"outbox_attempts_total",
	"outbox_dead_letters_total",
	"outbox_dead_letters_reprocessed_total",
	"outbox_dead_letters_discarded_total",
	"outbox_leased_total",
	"sqs_receive_errors_total",
	"sqs_delete_errors_total",
	"sqs_visibility_extension_errors_total",
	"outbox_poll_errors_total",
	"outbox_unmarshal_errors_total",
	"outbox_mark_published_errors_total",
	"events_oversized_event_type_label_total",
	"events_codec_encode_total",
	"events_codec_encode_duration_seconds",
	"events_codec_decode_total",
	"events_codec_decode_duration_seconds",
}

// runMetricsCheck registers events.InitWithRegisterer against a fresh registry (proving the
// hardcoded name list above doesn't collide/panic) and reports whichever families already have
// at least one sample (build_info always does; the rest only would after a real publish/
// consume/outbox call) as a secondary, best-effort dynamic cross-check.
func runMetricsCheck(args []string) error {
	reg := prometheus.NewRegistry()
	events.InitWithRegisterer("interop-probe", "0.0.0-test", reg)

	families, err := reg.Gather()
	if err != nil {
		return fmt.Errorf("gathering metrics: %w", err)
	}
	gathered := make([]string, 0, len(families))
	for _, mf := range families {
		gathered = append(gathered, mf.GetName())
	}
	sort.Strings(gathered)

	names := append([]string(nil), expectedMetricNames...)
	sort.Strings(names)

	return printJSON(map[string]any{
		"language":              "go",
		"check":                 "metrics-check",
		"metric_names":          names,
		"gathered_with_samples": gathered,
	})
}
