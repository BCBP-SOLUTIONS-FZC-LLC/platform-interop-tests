package main

import (
	"context"
	"flag"
	"fmt"
	"regexp"
	"slices"
	"sort"
	"strings"
	"sync"

	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-pgcommon/pkg/pgcommon"
	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-pgcommon/pkg/pgmetrics"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
)

// capturingRegisterer records every collector pgcommon registers, so the probe can read
// each one's declared variable-label order (a gathered sample's labels are sorted by name).
type capturingRegisterer struct {
	prometheus.Registerer
	mu         sync.Mutex
	collectors []prometheus.Collector
}

func (r *capturingRegisterer) Register(c prometheus.Collector) error {
	if err := r.Registerer.Register(c); err != nil {
		return err
	}
	r.mu.Lock()
	r.collectors = append(r.collectors, c)
	r.mu.Unlock()
	return nil
}

func (r *capturingRegisterer) MustRegister(cs ...prometheus.Collector) {
	for _, c := range cs {
		if err := r.Register(c); err != nil {
			panic(err)
		}
	}
}

// descRe parses prometheus.Desc.String() — the only access client_golang gives to a
// descriptor's label names: Desc{fqName: "…", help: "…", [unit: "…", ]constLabels:
// {k="v",…}, variableLabels: {a,b}}. Identity label values are [a-z0-9_-], so the const
// label list holds no brace.
var descRe = regexp.MustCompile(`^Desc\{fqName: "([^"]*)", .*, constLabels: \{([^}]*)\}, variableLabels: \{([^}]*)\}\}$`)

type declaredLabels struct {
	constNames []string
	variable   []string
}

// declared returns, per platform_db_* metric name, the const and variable label names its
// registered descriptor declares (variable labels in declaration order).
func (r *capturingRegisterer) declared() (map[string]declaredLabels, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := map[string]declaredLabels{}
	for _, c := range r.collectors {
		ch := make(chan *prometheus.Desc, 16)
		go func() { c.Describe(ch); close(ch) }()
		for d := range ch {
			m := descRe.FindStringSubmatch(d.String())
			if m == nil {
				return nil, fmt.Errorf("cannot parse descriptor %s", d.String())
			}
			if !strings.HasPrefix(m[1], "platform_") {
				continue
			}
			var dl declaredLabels
			for _, kv := range splitNonEmpty(m[2]) {
				dl.constNames = append(dl.constNames, strings.SplitN(kv, "=", 2)[0])
			}
			sort.Strings(dl.constNames)
			dl.variable = splitNonEmpty(m[3])
			out[m[1]] = dl
		}
	}
	return out, nil
}

func splitNonEmpty(s string) []string {
	if s == "" {
		return []string{}
	}
	return strings.Split(s, ",")
}

// runPGMetricsCheck reports platform-pgcommon's Tier 1 (platform_db_*) metric contract, for
// compare.py's pgmetrics check against python-probe's identical report from pgcommon-py:
//
//   - "registry": every pgmetrics.Registry() entry (name, type, tier, status, help = the
//     semantic definition, required/approved labels, allowed label values);
//   - "families": what pgmetrics.InitWithIdentity actually registers, observed live — after
//     Init, a real pool (--dsn) runs one query so every family exists with samples. Per
//     family: type and help as gathered, label names (identity labels, then the declared
//     variable labels in order), histogram buckets, and the values seen for the enumerated
//     labels state/outcome/reason.
func runPGMetricsCheck(args []string) error {
	fs := flag.NewFlagSet("pg-metrics-check", flag.ContinueOnError)
	dsn := fs.String("dsn", "", "postgres DSN (required: a live pool makes every family gatherable)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *dsn == "" {
		return fmt.Errorf("--dsn is required")
	}

	reg := prometheus.NewRegistry()
	capt := &capturingRegisterer{Registerer: reg}
	id := pgmetrics.Identity{Domain: "iam", Service: "interop-probe", Environment: "test"}
	warnings, err := pgmetrics.InitWithIdentity(id, capt)
	if err != nil {
		return fmt.Errorf("InitWithIdentity: %w", err)
	}
	warningNames := []string{}
	for _, w := range warnings {
		warningNames = append(warningNames, w.Metric)
	}

	ctx := context.Background()
	pool, err := pgcommon.NewPool(ctx, pgcommon.Config{DSN: *dsn, PoolName: "default", MaxConns: 2, MinConns: 1})
	if err != nil {
		return fmt.Errorf("connecting: %w", err)
	}
	err = pool.WithConn(ctx, func(ctx context.Context, conn *pgxpool.Conn) error {
		var one int
		return conn.QueryRow(ctx, "SELECT 1").Scan(&one)
	})
	if err != nil {
		pool.Close()
		return fmt.Errorf("query: %w", err)
	}

	declared, err := capt.declared()
	if err != nil {
		pool.Close()
		return err
	}
	mfs, err := reg.Gather()
	pool.Close()
	if err != nil {
		return fmt.Errorf("gathering metrics: %w", err)
	}

	required := []string{}
	registry := []map[string]any{}
	for _, e := range pgmetrics.Registry() {
		if string(e.Tier) != "platform" {
			continue
		}
		required = e.RequiredLabels
		labelValues := map[string][]string{}
		for k, v := range e.LabelValues {
			labelValues[k] = v
		}
		ruleLabels := []string{}
		for k := range e.LabelValueRules {
			ruleLabels = append(ruleLabels, k)
		}
		sort.Strings(ruleLabels)
		registry = append(registry, map[string]any{
			"name":              e.Name,
			"type":              string(e.Type),
			"tier":              string(e.Tier),
			"status":            string(e.Status),
			"help":              e.SemanticDefinition,
			"required_labels":   e.RequiredLabels,
			"approved_labels":   e.ApprovedLabels,
			"label_values":      labelValues,
			"label_rule_labels": ruleLabels,
		})
	}

	families := []map[string]any{}
	for _, mf := range mfs {
		name := mf.GetName()
		if !strings.HasPrefix(name, "platform_") {
			continue
		}
		dl, ok := declared[name]
		labels := []string{}
		constNames := []string{}
		if ok {
			constNames = dl.constNames
			// Whether a label is const or variable is a Go implementation detail (the
			// identity labels are const; the pool gauges also carry "pool" as a const
			// label); the exposed label names are the contract. List the identity labels
			// in the registry's RequiredLabels order, then any other const label (sorted),
			// then the variable labels as declared — compare.py checks the const set
			// covers RequiredLabels.
			labels = append(labels, required...)
			for _, c := range dl.constNames {
				if !slices.Contains(required, c) {
					labels = append(labels, c)
				}
			}
			labels = append(labels, dl.variable...)
		}
		buckets := []float64{}
		observed := map[string]map[string]bool{}
		for _, m := range mf.GetMetric() {
			for _, lp := range m.GetLabel() {
				switch lp.GetName() {
				case "state", "outcome", "reason":
					if observed[lp.GetName()] == nil {
						observed[lp.GetName()] = map[string]bool{}
					}
					observed[lp.GetName()][lp.GetValue()] = true
				}
			}
			if h := m.GetHistogram(); h != nil && len(buckets) == 0 {
				for _, b := range h.GetBucket() {
					buckets = append(buckets, b.GetUpperBound())
				}
			}
		}
		observedValues := map[string][]string{}
		for l, vs := range observed {
			list := []string{}
			for v := range vs {
				list = append(list, v)
			}
			sort.Strings(list)
			observedValues[l] = list
		}
		families = append(families, map[string]any{
			"name":                   name,
			"type":                   strings.ToLower(mf.GetType().String()),
			"help":                   mf.GetHelp(),
			"labels":                 labels,
			"const_labels":           constNames,
			"buckets":                buckets,
			"observed_label_values":  observedValues,
			"declared_by_collectors": ok,
		})
	}
	sort.Slice(families, func(i, j int) bool { return families[i]["name"].(string) < families[j]["name"].(string) })

	return printJSON(map[string]any{
		"language":              "go",
		"check":                 "pg-metrics-check",
		"registry":              registry,
		"families":              families,
		"registration_warnings": warningNames,
	})
}
