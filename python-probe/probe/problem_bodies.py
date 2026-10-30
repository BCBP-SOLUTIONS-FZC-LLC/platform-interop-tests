"""``problem-bodies``: the Python side of fixtures/problem_bodies.json.

Drives a real fastapicommon app in-process through the same scenarios as go-probe's
``problem-bodies`` (same names, same paths, same fixed traceparent and x-request-id) and prints
each raw response as JSON. go-probe adds these captures to its own and decodes all of them with
gincommon's ``problem.FromResponse``.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from fastapicommon.auth import CurrentUser, require_authenticated_user
from fastapicommon.bootstrap import create_platform_app
from fastapicommon.config.settings import PlatformSettings
from fastapicommon.exceptions import (
    ConflictException,
    PlatformException,
    ResourceNotFoundException,
    ValidationException,
)
from fastapicommon.problem import FieldError, type_for_status

SCENARIOS = [
    ("validation_failed", "/validation"),
    ("not_found", "/tenders/123"),
    ("conflict_with_extensions", "/conflict"),
    ("internal_error", "/internal"),
    ("unauthorized", "/protected"),
    ("route_not_found", "/no-such-route"),
    ("about_blank", "/gone"),
    ("server_timeout", "/slow"),
]


def request_headers(name: str) -> dict[str, str]:
    return {
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "x-request-id": "fixture-" + name.replace("_", "-"),
    }


def build_app() -> FastAPI:
    settings = PlatformSettings(request_timeout_seconds=0.05, enable_metrics=False, enable_tracing=True,
                                otlp_endpoint="none")
    app = create_platform_app(app_name="python-probe", settings=settings)

    @app.get("/validation")
    async def validation() -> None:
        raise ValidationException(
            validation_errors=[
                FieldError(field="deadline", message="must be in the future"),
                FieldError(field="items[0].sku", message="is required"),
            ]
        )

    @app.get("/tenders/{tender_id}")
    async def tender(tender_id: str) -> None:
        raise ResourceNotFoundException("Tender 123 was not found.", code="TENDER_NOT_FOUND")

    @app.get("/conflict")
    async def conflict() -> None:
        raise (
            ConflictException("The tenant was modified concurrently.", code="VERSION_CONFLICT")
            .with_extension("record_version", 7)
            .with_extensions({"active_workflows": ["wf-1", "wf-2"], "Zeta": True})
        )

    @app.get("/internal")
    async def internal() -> None:
        raise RuntimeError("asyncpg: password authentication failed for user app")

    @app.get("/gone")
    async def gone() -> None:
        raise PlatformException("The tender was withdrawn.", problem_type=type_for_status(410))

    @app.get("/slow")
    async def slow() -> str:
        await asyncio.sleep(2)
        return "too late"

    @app.get("/protected")
    async def protected(user: CurrentUser = Depends(require_authenticated_user)) -> str:
        return "ok"

    return app


def run(args: list[str]) -> int:
    client = TestClient(build_app(), raise_server_exceptions=False)
    out = []
    for name, path in SCENARIOS:
        res = client.get(path, headers=request_headers(name))
        out.append(
            {
                "name": name,
                "source": "python",
                "path": path,
                "status": res.status_code,
                "headers": {
                    "content-type": res.headers.get("content-type", ""),
                    "x-request-id": res.headers.get("x-request-id", ""),
                    "x-trace-id": res.headers.get("x-trace-id", ""),
                },
                "body": res.text,
            }
        )
    print(json.dumps(out, indent=2))
    return 0
