#!/usr/bin/env python3
"""Comparison logic used by run_interop.sh — one function per check type. Each takes two (or
more) file paths of JSON probe output and exits 0 if Go and Python agree, 1 (with a diagnostic
printed to stderr) if they don't. Kept as plain Python (no dependencies) so it runs under the
system interpreter without needing python-probe's venv.
"""

from __future__ import annotations

import json
import sys


def _load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def check_gucset(go_path: str, py_path: str) -> bool:
    go = _load(go_path)
    py = _load(py_path)
    go_results = {r["name"]: (r["valid"], r["error"]) for r in go["results"]}
    py_results = {r["name"]: (r["valid"], r["error"]) for r in py["results"]}
    diffs = {name: (go_results[name], py_results.get(name)) for name in go_results if go_results[name] != py_results.get(name)}
    if diffs or not go["all_match"] or not py["all_match"]:
        print(f"gucset mismatch: go_all_match={go['all_match']} py_all_match={py['all_match']} diffs={diffs}", file=sys.stderr)
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
    if problems:
        print("metrics mismatch: " + "; ".join(problems), file=sys.stderr)
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


CHECKS = {
    "gucset": check_gucset,
    "rls": check_rls,
    "envelope": check_envelope,
    "metrics": check_metrics,
    "whoami": check_whoami,
    "error_shape": check_error_shape,
    "trace_continuity": check_trace_continuity,
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
