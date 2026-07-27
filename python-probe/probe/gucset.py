from __future__ import annotations

import json
import sys

from pgcommon import EmptyTenantRoleError, GUCSet, InconsistentGUCSetError, InvalidTenantRoleError

from probe.util import print_json


def _error_code(exc: Exception | None) -> str | None:
    if exc is None:
        return None
    if isinstance(exc, InconsistentGUCSetError):
        return "inconsistent"
    if isinstance(exc, EmptyTenantRoleError):
        return "empty_role"
    if isinstance(exc, InvalidTenantRoleError):
        return "invalid_role"
    return f"unknown:{exc}"


def run(args: list[str]) -> int:
    if not args:
        print("gucset-check requires a path to gucset_cases.json", file=sys.stderr)
        return 2
    with open(args[0]) as f:
        fixture = json.load(f)

    results = []
    all_match = True
    for case in fixture["cases"]:
        guc = GUCSet(
            user_id=case["user_id"],
            tenant_id=case["tenant_id"],
            tenant_roles=case["tenant_roles"],
        )
        error: Exception | None = None
        try:
            guc.validate()
        except (InconsistentGUCSetError, EmptyTenantRoleError, InvalidTenantRoleError) as exc:
            error = exc

        code = _error_code(error)
        valid = error is None
        matches = valid == case["expected_valid"] and code == case["expected_error"]
        if not matches:
            all_match = False
        results.append(
            {"name": case["name"], "valid": valid, "error": code, "matches_fixture": matches}
        )

    print_json(
        {"language": "python", "check": "gucset-check", "all_match": all_match, "results": results}
    )
    return 0
