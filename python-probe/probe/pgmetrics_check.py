from __future__ import annotations

import argparse
import asyncio
import math
from typing import Any

from prometheus_client import CollectorRegistry

from pgcommon import Config, Pool, metrics_registry
from pgcommon.metrics import Identity, init_with_identity

from probe.util import print_json

_ENUMERATED_LABELS = ("state", "outcome", "reason")


def _registry_report() -> list[dict[str, Any]]:
    return [
        {
            "name": e.name,
            "type": str(e.type),
            "tier": str(e.tier),
            "status": str(e.status),
            "help": e.semantic_definition,
            "required_labels": list(e.required_labels),
            "approved_labels": list(e.approved_labels),
            "label_values": {k: list(v) for k, v in e.label_values.items()},
            "label_rule_labels": sorted(e.label_value_rules),
            # Python-only field (Go's RegistryEntry has none): compared with the buckets Go
            # actually registers, see compare.py.
            "buckets": list(e.buckets),
        }
        for e in metrics_registry.registry()
        if str(e.tier) == "platform"
    ]


def _families_report(registry: CollectorRegistry) -> list[dict[str, Any]]:
    families = []
    for mf in registry.collect():
        if not mf.name.startswith("platform_"):
            continue
        # prometheus_client strips "_total" from a counter family's name (the exposed sample
        # keeps it); restore it so names are the literal ones a scrape and Go use.
        name = mf.name if mf.type != "counter" or mf.name.endswith("_total") else f"{mf.name}_total"
        labels: list[str] = []
        buckets: list[float] = []
        observed: dict[str, set[str]] = {}
        for sample in mf.samples:
            # A sample's labels are in the collector's declared order (le appended last).
            names = [k for k in sample.labels if k != "le"]
            if len(names) > len(labels):
                labels = names
            for k in _ENUMERATED_LABELS:
                if k in sample.labels:
                    observed.setdefault(k, set()).add(sample.labels[k])
            if sample.name.endswith("_bucket") and "le" in sample.labels:
                le = float(sample.labels["le"])
                if not math.isinf(le) and le not in buckets:
                    buckets.append(le)
        families.append(
            {
                "name": name,
                "type": mf.type,
                "help": mf.documentation,
                "labels": labels,
                "buckets": sorted(buckets),
                "observed_label_values": {k: sorted(v) for k, v in observed.items()},
            }
        )
    return sorted(families, key=lambda f: f["name"])


async def _check(dsn: str) -> dict[str, Any]:
    registry = CollectorRegistry()
    identity = Identity(domain="iam", service="interop-probe", environment="test")
    warnings = init_with_identity(identity, registry)

    # A live pool and one query make every family gatherable with samples (a labelled
    # histogram exposes its buckets only once a series exists), exactly as go-probe does.
    pool = await Pool.create(Config(dsn=dsn, pool_name="default", max_conns=2, min_conns=1))
    try:

        async def _op(conn: Any) -> object:
            return await conn.fetchval("SELECT 1")

        await pool.with_conn(_op)
        families = _families_report(registry)
    finally:
        await pool.drain_and_close()

    return {
        "language": "python",
        "check": "pg-metrics-check",
        "registry": _registry_report(),
        "families": families,
        "registration_warnings": [w.metric for w in warnings],
        # Go Tier 1 behaviour the port declares it cannot reproduce, keyed by metric or
        # metric{label="value"} (pgcommon.metrics_registry.SKIPPED_IN_PYTHON). compare.py
        # accepts exactly these differences and no others.
        "skipped_in_python": sorted(metrics_registry.SKIPPED_IN_PYTHON),
    }


def run(args: list[str]) -> int:
    """Report pgcommon-py's Tier 1 (platform_db_*) metric contract: the registry entries and
    the families init_with_identity (plus a live pool) actually registers — the same report
    go-probe's pg-metrics-check produces from platform-pgcommon."""
    parser = argparse.ArgumentParser(prog="pg-metrics-check")
    parser.add_argument("--dsn", required=True)
    parsed = parser.parse_args(args)
    print_json(asyncio.run(_check(parsed.dsn)))
    return 0
