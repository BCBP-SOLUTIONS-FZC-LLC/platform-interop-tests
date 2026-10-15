from __future__ import annotations

from prometheus_client import CollectorRegistry

from eventcommon import metrics as metrics_module

from probe.util import print_json


def run(args: list[str]) -> int:
    """Report eventcommon's Tier 1 (platform_*) metric contract.

    ``metric_names`` are the platform_* families eventcommon registers;
    ``go_only`` are the Go Tier 1 metrics the Python port deliberately does not
    emit (eventcommon.metrics_registry.GO_ONLY_METRICS: features the port
    lacks). compare.py requires Go's set == metric_names | go_only.

    A pre-standard eventcommon (no ``init_metrics``) has no Tier 1 metrics:
    it reports an empty set, so the comparison fails with a clear message
    instead of crashing.
    """
    registry = CollectorRegistry()
    go_only: list[str] = []
    names: list[str] = []
    if hasattr(metrics_module, "init_metrics"):
        identity = metrics_module.MetricsIdentity(domain="iam", service="interop-probe", environment="test")
        warnings = metrics_module.init_metrics(identity, registry)
        if warnings:
            raise SystemExit(f"init_metrics refused metrics: {[str(w) for w in warnings]}")
        from eventcommon import metrics_registry

        go_only = sorted(getattr(metrics_registry, "GO_ONLY_METRICS", {}))
        # As go-probe does: the Tier 1 entries of the library's own registry, once they
        # registered without a warning. A scrape is only a cross-check — like Go's
        # client, eventcommon gathers a family only once it has a series.
        names = sorted(e.name for e in metrics_registry.registry() if str(e.tier) == "platform")

    # prometheus_client strips "_total" from a Counter's family name (it is re-added on
    # the exposed sample); add it back so these are the literal names a scrape uses.
    gathered = sorted(
        {
            mf.name if mf.type != "counter" or mf.name.endswith("_total") else f"{mf.name}_total"
            for mf in registry.collect()
            if mf.name.startswith("platform_")
        }
    )

    print_json(
        {
            "language": "python",
            "check": "metrics-check",
            "metric_names": names,
            "gathered_with_samples": gathered,
            "go_only": go_only,
            "tier1_supported": hasattr(metrics_module, "init_metrics"),
        }
    )
    return 0
