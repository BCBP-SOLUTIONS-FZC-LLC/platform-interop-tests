#!/usr/bin/env python3
"""Comparison logic used by run_interop.sh — one function per check type. Each takes two (or
more) file paths of JSON probe output and exits 0 if Go and Python agree, 1 (with a diagnostic
printed to stderr) if they don't. Kept as plain Python (no dependencies) so it runs under the
system interpreter without needing python-probe's venv.
"""

from __future__ import annotations

import json
import re
import sys
import zlib


def _load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def check_gucset(go_path: str, py_path: str) -> bool:
    go = _load(go_path)
    py = _load(py_path)
    go_results = {r["name"]: (r["valid"], r["error"]) for r in go["results"]}
    py_results = {r["name"]: (r["valid"], r["error"]) for r in py["results"]}
    names = go_results.keys() | py_results.keys()
    diffs = {name: (go_results.get(name), py_results.get(name)) for name in sorted(names) if go_results.get(name) != py_results.get(name)}
    unmapped = py.get("unmapped_identity_errors", [])
    if diffs or unmapped or not go["all_match"] or not py["all_match"]:
        misses = {lang: [r["name"] for r in doc["results"] if not r["matches_fixture"]] for lang, doc in (("go", go), ("py", py))}
        print(f"gucset mismatch: go_all_match={go['all_match']} py_all_match={py['all_match']} "
              f"(go,py) diffs={diffs} fixture misses={misses} "
              f"pgcommon.IDENTITY_ERRORS without a fixture code={unmapped}", file=sys.stderr)
        return False
    return True


def check_rls(go_path: str, py_path: str) -> bool:
    go = _load(go_path)
    py = _load(py_path)
    if sorted(go["visible_ids"]) != sorted(py["visible_ids"]):
        print(f"rls mismatch: go={go['visible_ids']} py={py['visible_ids']}", file=sys.stderr)
        return False
    return True


def check_envelope(go_path: str, py_path: str) -> bool:
    go = _load(go_path)["parsed"]
    py = _load(py_path)["parsed"]
    diffs = {k: (go[k], py.get(k)) for k in go if go[k] != py.get(k)}
    if diffs:
        print(f"envelope mismatch: {diffs}", file=sys.stderr)
        return False
    return True


def check_metrics(go_path: str, py_path: str) -> bool:
    """Tier 1 (platform_*) parity: Go's set must equal Python's plus the Go
    metrics the Python port declares it does not emit (GO_ONLY_METRICS)."""
    go_doc, py_doc = _load(go_path), _load(py_path)
    go = set(go_doc["metric_names"])
    py = set(py_doc["metric_names"])
    go_only = set(py_doc.get("go_only", []))
    if not py_doc.get("tier1_supported", True):
        print("metrics mismatch: this eventcommon has no Tier 1 metrics (pre-standard); "
              "it needs the Enterprise Platform Observability Standard port", file=sys.stderr)
        return False
    problems = []
    if go_only & py:
        problems.append(f"declared go_only but emitted by Python: {sorted(go_only & py)}")
    if go_only - go:
        problems.append(f"declared go_only but not in Go's registry: {sorted(go_only - go)}")
    if py - go:
        problems.append(f"py_only={sorted(py - go)}")
    if go - py - go_only:
        problems.append(f"go_only (undeclared)={sorted(go - py - go_only)}")
    # What a scrape of a freshly initialised registry exposes: both clients gather a
    # family only once it has a series, so the pre-created series must be the same.
    if "gathered_with_samples" in go_doc and "gathered_with_samples" in py_doc:
        go_g, py_g = set(go_doc["gathered_with_samples"]), set(py_doc["gathered_with_samples"])
        if go_g != py_g:
            problems.append(f"gathered families differ: go_only={sorted(go_g - py_g)} py_only={sorted(py_g - go_g)}")
    if problems:
        print("metrics mismatch: " + "; ".join(problems), file=sys.stderr)
        return False
    return True


# What every pg-migrate-check roundtrip step must report (python-probe/probe/migrate_check.py
# ROUNDTRIP_STEPS runs them in order on one database). Runner steps: the listed keys of the
# probe result ("error_has" = substring of "error"). CLI steps: pgcommon-migrate status's exit
# code and stdout.
_DIV0 = "division by zero"
MIGRATE_ROUNDTRIP_EXPECTED: dict[str, dict] = {
    "go_steps_2": {"ok": True, "outcome": "changed"},
    "py_version_after_go_steps_2": {"ok": True, "version": 2, "dirty": False},
    "py_cli_status_after_go_steps_2": {"exit_code": 0, "stdout": "version: 2 dirty: false"},
    "py_steps_1": {"ok": True, "outcome": "changed"},
    "go_version_after_py_steps_1": {"ok": True, "version": 3, "dirty": False},
    "py_down_1": {"ok": True, "outcome": "changed"},
    "go_version_after_py_down_1": {"ok": True, "version": 2, "dirty": False},
    "go_up_fails_at_4": {"ok": False, "error_code": "error", "error_has": _DIV0},
    "go_version_dirty_4": {"ok": True, "version": 4, "dirty": True},
    "py_version_dirty_4_from_go": {"ok": True, "version": 4, "dirty": True},
    "py_cli_status_dirty_4_from_go": {"exit_code": 3, "stdout": "version: 4 dirty: true"},
    "py_up_refuses_dirty_from_go": {"ok": False, "error_code": "dirty"},
    "py_force_3": {"ok": True},
    "go_version_after_py_force_3": {"ok": True, "version": 3, "dirty": False},
    "py_up_fails_at_4": {"ok": False, "error_code": "error", "error_has": _DIV0},
    "py_version_dirty_4": {"ok": True, "version": 4, "dirty": True},
    "go_version_dirty_4_from_py": {"ok": True, "version": 4, "dirty": True},
    "go_up_refuses_dirty_from_py": {"ok": False, "error_code": "dirty"},
    "go_force_3": {"ok": True},
    "py_version_after_go_force_3": {"ok": True, "version": 3, "dirty": False},
    "go_down_3": {"ok": True, "outcome": "changed"},
    "py_version_none_after_go_down_3": {"ok": True, "version": None, "dirty": False},
    "py_cli_status_none_after_go_down_3": {"exit_code": 0, "stdout": "version: none (no migrations applied)"},
    "py_up_from_none_fails_at_4": {"ok": False, "error_code": "error", "error_has": _DIV0},
    "go_version_dirty_4_after_py_up": {"ok": True, "version": 4, "dirty": True},
}

# A DECLARED Go/Python divergence, not a bug (pgcommon-py ARCHITECTURE.md § Divergences from
# Go, "Migration advisory lock"): Go's Runner steps golang-migrate one migration at a time, and
# golang-migrate takes and releases the advisory lock per step, so a runner waiting on the lock
# takes over between two of Go's migrations; pgcommon-py holds the lock for its whole run. Both
# keep every migration exactly-once (each reads the version under the lock). Observable effect
# when Go leads and Python waits from inside Go's slow migration 2: Go applies 1-2, Python 3.
# If Go ever holds the lock for the whole run, this check fails and says so: delete this then.
GO_LOCKS_PER_MIGRATION = {"go_leader_applies": [1, 2], "python_follower_applies": [3]}

_ADVISORY_LOCK_SALT = 1486364155  # golang-migrate database.GenerateAdvisoryLockId


def _golang_migrate_lock_key(database_name: str, schema: str, table: str) -> int:
    return (zlib.crc32("\x00".join([schema, table, database_name]).encode()) * _ADVISORY_LOCK_SALT) & 0xFFFFFFFF


def _short(result: dict) -> dict:
    return {k: result.get(k) for k in ("ok", "error_code", "version", "dirty", "outcome", "error", "exit_code", "stdout") if k in result}


def _migrate_roundtrip(rt: dict, problems: list[str]) -> None:
    """(a) each runner reads, refuses and recovers what the other wrote."""
    steps = {s["step"]: s for s in rt["steps"]}
    for name in MIGRATE_ROUNDTRIP_EXPECTED.keys() - steps.keys():
        problems.append(f"roundtrip step {name} missing from the report")
    for name, step in steps.items():
        want = MIGRATE_ROUNDTRIP_EXPECTED.get(name)
        if want is None:
            problems.append(f"roundtrip step {name} has no expectation in compare.py")
            continue
        got = step["result"]
        bad = {}
        for k, v in want.items():
            if k == "error_has":
                if v not in (got.get("error") or ""):
                    bad[k] = (v, got.get("error"))
            elif got.get(k, "<absent>") != v:
                bad[k] = (v, got.get(k, "<absent>"))
        if bad:
            problems.append(f"roundtrip {name} ({step['runner']} {step['action']}): want/got {bad}; result={_short(got)}")
    if rt.get("failed_migration_left_objects"):
        problems.append("roundtrip: the failed migration 4 left interop_never_created behind (not rolled back)")


def _migrate_lock_keys(lk: dict, problems: list[str]) -> None:
    """(b) the key each runner holds, observed in pg_locks, is the same — and is
    golang-migrate's key for the expected (databaseName, schema, table)."""
    for v in lk["variants"]:
        name = v["variant"]
        inputs = v["lock_inputs"]
        expected = _golang_migrate_lock_key(inputs["database_name"], inputs["schema"], inputs["table"])
        for lang in ("go", "python"):
            r = v[lang]["result"]
            if not r.get("ok") or r.get("outcome") != "changed":
                problems.append(f"lock_keys {name}: {lang} run did not apply the migrations: {_short(r)}")
            if len(v[lang]["held_keys"]) != 1:
                problems.append(f"lock_keys {name}: {lang} held {v[lang]['held_keys']} (want exactly one advisory key)")
        if v["go"]["held_keys"] != v["python"]["held_keys"]:
            problems.append(f"lock_keys {name}: Go locked {v['go']['held_keys']}, Python locked {v['python']['held_keys']} — "
                            "a Go and a Python runner would migrate this table concurrently")
        elif v["go"]["held_keys"] != [expected]:
            problems.append(f"lock_keys {name}: both locked {v['go']['held_keys']}, golang-migrate's key for {inputs} is {expected}")


def _migrate_serialisation(rounds: list, problems: list[str]) -> None:
    """(c) started together, they serialise on the lock; every migration applied once."""
    for rnd in rounds:
        tag = f"serialisation {rnd['leader']}-first"
        lead, follow = rnd["leader_result"], rnd["follower_result"]
        if not rnd["leader_was_in_slow_migration_when_follower_started"]:
            problems.append(f"{tag}: the leader was never seen holding the lock inside the slow migration")
        if not lead.get("ok") or not follow.get("ok"):
            problems.append(f"{tag}: a runner failed: leader={_short(lead)} follower={_short(follow)}")
        if len(rnd["leader_held_keys"]) != 1 or rnd["follower_waited_keys"] != rnd["leader_held_keys"]:
            problems.append(f"{tag}: follower waited on {rnd['follower_waited_keys']}, leader held {rnd['leader_held_keys']} "
                            "(want: the follower blocked on the leader's one key)")
        if "Lock:advisory" not in rnd["lock_wait_events"]:
            problems.append(f"{tag}: no session was seen waiting on an advisory lock ({rnd['lock_wait_events']})")
        applied: dict[int, list[str]] = {}
        for e in rnd["effects"]:
            applied.setdefault(e["version"], []).extend([e["applied_by"]] * e["count"])
        if sorted(applied) != [1, 2, 3] or any(len(by) != 1 for by in applied.values()):
            problems.append(f"{tag}: migrations not applied exactly once each: {applied}")
        if rnd["final_version"] != {"version": 3, "dirty": False}:
            problems.append(f"{tag}: final tracking row {rnd['final_version']}, want version 3 clean")
        by = {v: who[0] for v, who in applied.items() if len(who) == 1}
        leader_app, follower_app = f"interop-{rnd['leader']}", f"interop-{rnd['follower']}"
        if rnd["leader"] == "python":
            if follow.get("outcome") != "no_change" or any(w != leader_app for w in by.values()):
                problems.append(f"{tag}: the Go follower should wait out the whole Python run and report no change; "
                                f"follower={_short(follow)} applied_by={by}")
        else:
            want_by = {v: leader_app for v in GO_LOCKS_PER_MIGRATION["go_leader_applies"]}
            want_by |= {v: follower_app for v in GO_LOCKS_PER_MIGRATION["python_follower_applies"]}
            if by != want_by or follow.get("outcome") != "changed":
                if follow.get("outcome") == "no_change" and all(w == leader_app for w in by.values()):
                    problems.append(f"{tag}: Go now holds the lock for its whole run — the declared divergence "
                                    "GO_LOCKS_PER_MIGRATION no longer holds; delete it and expect no_change here")
                else:
                    problems.append(f"{tag}: want applied_by={want_by} and a 'changed' Python follower "
                                    f"(GO_LOCKS_PER_MIGRATION); got applied_by={by} follower={_short(follow)}")


_MIGRATE_SCENARIOS = {
    "roundtrip": _migrate_roundtrip,
    "lock_keys": _migrate_lock_keys,
    "serialisation": _migrate_serialisation,
}


def check_migrate(report_path: str, scenario: str = "all") -> bool:
    """pg-migrate-check: the Go and Python migration runners are interchangeable on one
    database. `scenario` limits the check to one section of the report."""
    report = _load(report_path)
    problems: list[str] = []
    for name, check in _MIGRATE_SCENARIOS.items():
        if scenario in ("all", name):
            check(report[name], problems)
    if scenario != "all" and scenario not in _MIGRATE_SCENARIOS:
        problems.append(f"unknown scenario {scenario!r} (want one of {sorted(_MIGRATE_SCENARIOS)})")
    if problems:
        print(f"pg-migrate mismatch ({scenario}):\n  " + "\n  ".join(problems), file=sys.stderr)
        return False
    return True


def _parse_skip(key: str) -> tuple[str, str, str] | None:
    """'metric{label="value"}' -> (metric, label, value); None for any other shape."""
    m = re.fullmatch(r'(\w+)\{(\w+)="([^"]*)"\}', key)
    return (m.group(1), m.group(2), m.group(3)) if m else None


def check_pgmetrics(go_path: str, py_path: str) -> bool:
    """Tier 1 platform_db_* parity: the registry entries and the live registered families.

    Every field must be equal, except the Go label values the Python port declares it cannot
    emit (pgcommon.metrics_registry.SKIPPED_IN_PYTHON, reported as "skipped_in_python"); a
    declaration this check can observe must hold (Go emits the value, Python does not)."""
    go, py = _load(go_path), _load(py_path)
    problems: list[str] = []
    for lang, doc in (("go", go), ("python", py)):
        if doc["registration_warnings"]:
            problems.append(f"{lang}: registration warnings for {doc['registration_warnings']}")
        reg_names = {e["name"] for e in doc["registry"]}
        fam_names = {f["name"] for f in doc["families"]}
        if reg_names != fam_names:
            problems.append(f"{lang}: registry and registered families differ: registry-only={sorted(reg_names - fam_names)} "
                            f"registered-only={sorted(fam_names - reg_names)}")

    go_reg = {e["name"]: e for e in go["registry"]}
    py_reg = {e["name"]: e for e in py["registry"]}
    go_fam = {f["name"]: f for f in go["families"]}
    py_fam = {f["name"]: f for f in py["families"]}
    if go_reg.keys() != py_reg.keys():
        problems.append(f"registry names differ: go-only={sorted(go_reg.keys() - py_reg.keys())} "
                        f"py-only={sorted(py_reg.keys() - go_reg.keys())}")
    if go_fam.keys() != py_fam.keys():
        problems.append(f"registered families differ: go-only={sorted(go_fam.keys() - py_fam.keys())} "
                        f"py-only={sorted(py_fam.keys() - go_fam.keys())}")

    reg_fields = ("type", "tier", "status", "help", "required_labels", "approved_labels", "label_values", "label_rule_labels")
    for name in sorted(go_reg.keys() & py_reg.keys()):
        g, p = go_reg[name], py_reg[name]
        for f in reg_fields:
            if g[f] != p[f]:
                problems.append(f"registry {name}.{f}: go={g[f]!r} py={p[f]!r}")
        # Go's RegistryEntry has no buckets; Python's must equal what Go registers.
        if name in go_fam and p.get("buckets", []) != go_fam[name]["buckets"]:
            problems.append(f"registry {name}.buckets: py registry={p.get('buckets')} go registered={go_fam[name]['buckets']}")

    skips: dict[tuple[str, str], set[str]] = {}
    for key in py.get("skipped_in_python", []):
        parsed = _parse_skip(key)
        if parsed:
            skips.setdefault(parsed[:2], set()).add(parsed[2])

    for name in sorted(go_fam.keys() & py_fam.keys()):
        g, p = go_fam[name], py_fam[name]
        for f in ("type", "help", "labels", "buckets"):
            if g[f] != p[f]:
                problems.append(f"family {name}.{f}: go={g[f]!r} py={p[f]!r}")
        if not set(go_reg.get(name, {}).get("required_labels", [])) <= set(g.get("const_labels", [])):
            problems.append(f"family {name}: Go const labels {g.get('const_labels')} do not cover the identity labels")
        go_vals, py_vals = g["observed_label_values"], p["observed_label_values"]
        for label in sorted(go_vals.keys() | py_vals.keys()):
            gv, pv = set(go_vals.get(label, [])), set(py_vals.get(label, []))
            declared = skips.get((name, label), set())
            for value in sorted(declared):
                if value not in gv or value in pv:
                    problems.append(f"family {name}: SKIPPED_IN_PYTHON declares {label}=\"{value}\" Go-only, "
                                    f"but go emits={sorted(gv)} py emits={sorted(pv)}")
            if gv - declared != pv:
                problems.append(f"family {name}: {label} values go={sorted(gv)} py={sorted(pv)} "
                                f"(declared Go-only: {sorted(declared)})")

    if problems:
        print("pg-metrics mismatch:\n  " + "\n  ".join(problems), file=sys.stderr)
        return False
    return True


def check_whoami(go_path: str, py_path: str) -> bool:
    go = _load(go_path)
    py = _load(py_path)
    fields = ("tenant_id", "user_id", "roles")
    diffs = {f: (go.get(f), py.get(f)) for f in fields if go.get(f) != py.get(f)}
    if diffs:
        print(f"whoami mismatch: {diffs}", file=sys.stderr)
        return False
    return True


def check_error_shape(go_path: str, py_path: str, go_status: str, py_status: str) -> bool:
    go = _load(go_path)
    py = _load(py_path)
    expected_keys = {"error", "status", "trace_id", "request_id"}
    if set(go) != expected_keys or set(py) != expected_keys:
        print(f"error_shape key mismatch: go_keys={set(go)} py_keys={set(py)}", file=sys.stderr)
        return False
    if go_status != "401" or py_status != "401":
        print(f"error_shape: expected both 401, got go={go_status} py={py_status}", file=sys.stderr)
        return False
    return True


def check_trace_continuity(go_path: str, py_path: str) -> bool:
    go = _load(go_path)
    py = _load(py_path)
    ok = True
    if go.get("own_trace_id") != go.get("peer_response", {}).get("trace_id"):
        print(f"go->python trace mismatch: {go.get('own_trace_id')} != {go.get('peer_response', {}).get('trace_id')}", file=sys.stderr)
        ok = False
    if py.get("own_trace_id") != py.get("peer_response", {}).get("trace_id"):
        print(f"python->go trace mismatch: {py.get('own_trace_id')} != {py.get('peer_response', {}).get('trace_id')}", file=sys.stderr)
        ok = False
    return ok


_ENVELOPE_FIELDS = ("id", "type", "source", "specversion", "tenant_id", "trace_id",
                    "correlation_id", "subject", "actor", "ip_address", "user_agent", "time")


def check_events_wire(sent_path: str, got_path: str, options: str = "") -> bool:
    """One SNS/SQS direction: every event the producer probe sent was handled once by the
    consumer probe, with the same envelope fields and payload, routing attributes,
    handler context (trace ID, tenant GUC, baggage), a span link to the producer's trace
    and — per `options` (comma-separated: codec, hmac, fifo) — a decoded codec payload, a
    verifying HMAC signature, and FIFO per-group order."""
    flags = set(filter(None, options.split(",")))
    sent_doc, got_doc = _load(sent_path), _load(got_path)
    sent, got = sent_doc["sent"], got_doc["received"]
    producer = sent_doc["language"]
    problems: list[str] = []
    if len(got) != len(sent):
        problems.append(f"received {len(got)} of {len(sent)}")
    by_id = {g["envelope"]["id"]: g for g in got}
    for s in sent:
        env, key = s["envelope"], s["key"]
        g = by_id.get(env["id"])
        if g is None:
            problems.append(f"{key}: not received")
            continue
        r = g["envelope"]
        for field in _ENVELOPE_FIELDS:
            if env.get(field, "") != r.get(field, ""):
                problems.append(f"{key}: {field} sent {env.get(field)!r}, handler saw {r.get(field)!r}")
        if "codec" in flags:
            if not r.get("dataschema"):
                problems.append(f"{key}: dataschema missing after codec decode")
        elif env.get("dataschema", "") != r.get("dataschema", ""):
            problems.append(f"{key}: dataschema differs")
        if r.get("data") != env.get("data"):
            problems.append(f"{key}: data differs")
        if json.loads(g["payload_raw"] or "null") != env.get("data"):
            problems.append(f"{key}: handler payload differs: {g['payload_raw'][:120]}")
        if g["trace_id_ctx"] != env.get("trace_id", ""):
            problems.append(f"{key}: handler trace ID {g['trace_id_ctx']!r}")
        if not g["has_tenant_guc"] or g["tenant_guc"] != env.get("tenant_id", ""):
            problems.append(f"{key}: handler tenant GUC {g['tenant_guc']!r}")
        want_baggage = {"probe.lang": "go" if producer == "go" else "py"}
        if g.get("baggage") != want_baggage:
            problems.append(f"{key}: handler baggage {g.get('baggage')!r}, want {want_baggage!r}")
        attrs = g.get("attributes") or {}
        want = {"EventType": env["type"], "Source": env["source"], "EventID": env["id"]}
        if env.get("tenant_id"):
            want["TenantID"] = env["tenant_id"]
        if env.get("subject"):
            want["Subject"] = env["subject"]
        for k, v in want.items():
            if attrs.get(k) != v:
                problems.append(f"{key}: attribute {k}={attrs.get(k)!r}, want {v!r}")
        if "traceparent" not in attrs:
            problems.append(f"{key}: no traceparent attribute")
        if s["producer_trace_id"] not in (g.get("linked_trace_ids") or []):
            problems.append(f"{key}: consumer span not linked to the producer's trace")
        if "hmac" in flags and g.get("hmac_ok") is not True:
            problems.append(f"{key}: the producer's HMAC signature does not verify")
    if "fifo" in flags:
        group_of = {s["envelope"]["id"]: s["group"] for s in sent}
        order = [g["envelope"]["id"] for g in got]
        for grp in sorted(set(group_of.values())):
            want_order = [s["envelope"]["id"] for s in sent if s["group"] == grp]
            got_order = [i for i in order if group_of.get(i) == grp]
            if got_order != want_order:
                problems.append(f"FIFO group {grp}: handled out of order")
    for p in problems:
        print(f"events-wire ({producer}->{got_doc['language']}): {p}", file=sys.stderr)
    return not problems


def check_events_raw(go_path: str, py_path: str) -> bool:
    """Hand-crafted SQS bodies (events_raw_bodies.json): both consumers make the same
    decision — handle, or drop as malformed — and hand the handler the same envelope."""
    go = {g["attributes"]["Case"]: g for g in _load(go_path)["received"]}
    py = {g["attributes"]["Case"]: g for g in _load(py_path)["received"]}
    problems = []
    for case in sorted(set(go) | set(py)):
        g, p = go.get(case), py.get(case)
        if (g is None) != (p is None):
            problems.append(f"{case}: Go {'handled' if g else 'dropped'}, Python {'handled' if p else 'dropped'}")
            continue
        for k in sorted(set(g["envelope"]) | set(p["envelope"])):
            if g["envelope"].get(k) != p["envelope"].get(k):
                problems.append(f"{case}: {k} Go={g['envelope'].get(k)!r} Python={p['envelope'].get(k)!r}")
        if json.loads(g["payload_raw"] or "null") != json.loads(p["payload_raw"] or "null"):
            problems.append(f"{case}: handler payload differs")
        for k in ("trace_id_ctx", "tenant_guc"):
            if g[k] != p[k]:
                problems.append(f"{case}: {k} Go={g[k]!r} Python={p[k]!r}")
    for p in problems:
        print(f"events-raw: {p}", file=sys.stderr)
    return not problems


def check_events_outbox(enqueued_path: str, got_path: str) -> bool:
    """Shared outbox: every event one language enqueued was published by the other
    language's runner and handled by the consumer, ordered-key events in enqueue order."""
    enq, got = _load(enqueued_path), _load(got_path)["received"]
    seen = [g["envelope"]["id"] for g in got]
    problems = []
    if sorted(seen) != sorted(enq["ids"]):
        problems.append(f"handled {len(seen)} of {len(enq['ids'])} enqueued events")
    if [i for i in seen if i in enq["ordered_ids"]] != enq["ordered_ids"]:
        problems.append("ordered-key events handled out of enqueue order")
    if any(g["tenant_guc"] != "acme" for g in got):
        problems.append("handler tenant GUC is not the enqueued tenant")
    for p in problems:
        print(f"events-outbox: {p}", file=sys.stderr)
    return not problems


CHECKS = {
    "gucset": check_gucset,
    "rls": check_rls,
    "envelope": check_envelope,
    "metrics": check_metrics,
    "migrate": check_migrate,
    "pgmetrics": check_pgmetrics,
    "whoami": check_whoami,
    "error_shape": check_error_shape,
    "trace_continuity": check_trace_continuity,
    "events_wire": check_events_wire,
    "events_raw": check_events_raw,
    "events_outbox": check_events_outbox,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in CHECKS:
        print(f"usage: compare.py <{'|'.join(CHECKS)}> <args...>", file=sys.stderr)
        return 2
    name = sys.argv[1]
    ok = CHECKS[name](*sys.argv[2:])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
