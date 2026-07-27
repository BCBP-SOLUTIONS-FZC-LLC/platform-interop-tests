from __future__ import annotations

from prometheus_client import CollectorRegistry

from eventcommon import metrics as metrics_module

from probe.util import print_json


def run(args: list[str]) -> int:
    registry = CollectorRegistry()
    metrics_module.init_with_registry("interop-probe", "0.0.0-test", registry)

    # Unlike Go's client_golang, prometheus_client lists every registered family name up front —
    # no need to force samples first (see go-probe/metrics.go's comment on this asymmetry). But
    # prometheus_client strips the "_total" suffix from a Counter's *family* name (it's re-added
    # on the actual exposed sample/metric name) while leaving Gauge/Histogram names alone — add
    # it back so these are the literal names Go and a Prometheus scrape both actually use.
    names = sorted(
        {
            mf.name if mf.type != "counter" or mf.name.endswith("_total") else f"{mf.name}_total"
            for mf in registry.collect()
        }
    )

    print_json({"language": "python", "check": "metrics-check", "metric_names": names})
    return 0
