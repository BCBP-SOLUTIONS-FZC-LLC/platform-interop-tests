package main

import (
	"fmt"
	"sort"

	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-events/pkg/events"
	"github.com/prometheus/client_golang/prometheus"
)

// runMetricsCheck reports platform-events' Tier 1 (platform_*) metric
// contract: the names of every Tier 1 entry in the library's own registry
// (events.MetricsRegistry). The pre-standard legacy names are not part of
// the cross-language contract — they were removed from both libraries with no
// compatibility period — so they are excluded on releases that still carry
// them, which keeps this check valid against any platform-events with the
// Observability Standard (v1.6.0+).
//
// InitMetrics against a fresh registry proves the set registers without
// error; gathered_with_samples is a best-effort dynamic cross-check (Go's
// client_golang only gathers a vector once a label combination exists).
func runMetricsCheck(args []string) error {
	reg := prometheus.NewRegistry()
	id := events.MetricsIdentity{Domain: "iam", Service: "interop-probe", Environment: "test"}
	if _, err := events.InitMetrics(id, reg); err != nil {
		return fmt.Errorf("InitMetrics: %w", err)
	}

	names := []string{}
	for _, e := range events.MetricsRegistry() {
		if string(e.Tier) == "platform" {
			names = append(names, e.Name)
		}
	}
	sort.Strings(names)

	families, err := reg.Gather()
	if err != nil {
		return fmt.Errorf("gathering metrics: %w", err)
	}
	gathered := make([]string, 0, len(families))
	for _, mf := range families {
		gathered = append(gathered, mf.GetName())
	}
	sort.Strings(gathered)

	return printJSON(map[string]any{
		"language":              "go",
		"check":                 "metrics-check",
		"metric_names":          names,
		"gathered_with_samples": gathered,
	})
}
