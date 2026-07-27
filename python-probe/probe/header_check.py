from __future__ import annotations

import json
import sys

from fastapicommon.auth.validation import normalize_header_value, normalize_request_id, parse_roles

from probe.util import print_json


def run(args: list[str]) -> int:
    """Pure-function check against fastapicommon.auth.validation — Python-only (Go's equivalent
    logic lives in gincommon's unexported internal/core/domain package and can only be exercised
    via the real HTTP surface; see go-probe's http-serve and COMPATIBILITY.md's residual-risk
    note for pair 2). Still valuable as a fast, no-server-required regression check on its own.
    """
    if not args:
        print("header-check requires a path to header_cases.json", file=sys.stderr)
        return 2
    with open(args[0]) as f:
        fixture = json.load(f)

    all_match = True
    identity_results = []
    for case in fixture["identity_header_cases"]:
        got = normalize_header_value(case["raw"])
        matches = got == case["expected"]
        all_match = all_match and matches
        identity_results.append({"name": case["name"], "got": got, "matches_fixture": matches})

    for case in fixture["identity_header_length_cases"]:
        raw = "a" * case["length"]
        got = normalize_header_value(raw) is not None
        matches = got == case["valid"]
        all_match = all_match and matches
        identity_results.append({"name": case["name"], "got": got, "matches_fixture": matches})

    request_id_results = []
    for case in fixture["request_id_cases"]:
        got = normalize_request_id(case["raw"])
        matches = got == case["expected"]
        all_match = all_match and matches
        request_id_results.append({"name": case["name"], "got": got, "matches_fixture": matches})

    for case in fixture["request_id_length_cases"]:
        raw = "a" * case["length"]
        got = normalize_request_id(raw) is not None
        matches = got == case["valid"]
        all_match = all_match and matches
        request_id_results.append({"name": case["name"], "got": got, "matches_fixture": matches})

    roles_results = []
    for case in fixture["roles_cases"]:
        got = list(parse_roles(case["raw"]))
        matches = got == case["expected"]
        all_match = all_match and matches
        roles_results.append({"name": case["name"], "got": got, "matches_fixture": matches})

    for case in fixture["roles_length_cases"]:
        raw = "a" * case["length"]
        got = len(parse_roles(raw)) == 1  # kept (len 1) vs dropped (len 0)
        matches = got == case["valid"]
        all_match = all_match and matches
        roles_results.append({"name": case["name"], "got": got, "matches_fixture": matches})

    print_json(
        {
            "language": "python",
            "check": "header-check",
            "all_match": all_match,
            "identity_header_results": identity_results,
            "request_id_results": request_id_results,
            "roles_results": roles_results,
        }
    )
    return 0
