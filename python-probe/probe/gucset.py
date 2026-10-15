from __future__ import annotations

import json
import sys

from pgcommon import (
    IDENTITY_ERRORS,
    EmptyTenantRoleError,
    GUCSet,
    InconsistentGUCSetError,
    InvalidGUCValueError,
    InvalidTenantRoleError,
    TenantRolesWithoutTenantError,
)

from probe.util import print_json

# The fixture's language-agnostic error codes (see the _comment in fixtures/gucset_cases.json),
# one per pgcommon.IDENTITY_ERRORS member — go-probe/gucset.go maps Go's sentinels to the same.
_CODES: tuple[tuple[type[Exception], str], ...] = (
    (InconsistentGUCSetError, "inconsistent"),
    (TenantRolesWithoutTenantError, "roles_without_tenant"),
    (EmptyTenantRoleError, "empty_role"),
    (InvalidTenantRoleError, "invalid_role"),
    (InvalidGUCValueError, "invalid_value"),
)


def _error_code(exc: BaseException | None) -> str | None:
    if exc is None:
        return None
    for cls, code in _CODES:
        if isinstance(exc, cls):
            return code
    # An IDENTITY_ERRORS member added after this probe, or an unexpected exception: reported,
    # never matching a fixture case, instead of crashing the whole check.
    return f"unknown:{type(exc).__name__}: {exc}"


def run(args: list[str]) -> int:
    if not args:
        print("gucset-check requires a path to gucset_cases.json", file=sys.stderr)
        return 2
    with open(args[0]) as f:
        fixture = json.load(f)

    unmapped = [cls.__name__ for cls in IDENTITY_ERRORS if cls not in dict(_CODES)]
    results = []
    all_match = not unmapped
    for case in fixture["cases"]:
        error: Exception | None = None
        try:
            GUCSet(
                user_id=case["user_id"],
                tenant_id=case["tenant_id"],
                tenant_roles=case["tenant_roles"],
            ).validate()
        except IDENTITY_ERRORS as exc:
            error = exc
        except Exception as exc:  # noqa: BLE001 — reported as "unknown:…", see _error_code
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
        {
            "language": "python",
            "check": "gucset-check",
            "all_match": all_match,
            # IDENTITY_ERRORS members this probe has no fixture code for: a new identity error
            # in pgcommon must get a code (here and in go-probe/gucset.go) and fixture cases.
            "unmapped_identity_errors": unmapped,
            "results": results,
        }
    )
    return 0
