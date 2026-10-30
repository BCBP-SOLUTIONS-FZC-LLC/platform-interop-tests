from __future__ import annotations

import argparse
import os

from fastapi import Depends, FastAPI, Request

from fastapicommon.auth import CurrentUser, require_authenticated_user
from fastapicommon.bootstrap import create_platform_app
from fastapicommon.context import get_request_context
from fastapicommon.exceptions import ConflictException
from fastapicommon.propagation import create_platform_http_client


def build_app() -> FastAPI:
    app = create_platform_app(app_name="python-probe")

    @app.get("/whoami")
    async def whoami() -> dict[str, object]:
        ctx = get_request_context()
        return {
            "language": "python",
            "tenant_id": ctx.tenant_id,
            "user_id": ctx.user_id,
            "roles": list(ctx.roles),
            "trace_id": ctx.trace_id,
            "client_ip": ctx.client_ip,
        }

    @app.get("/whoami-protected")
    async def whoami_protected(
        user: CurrentUser = Depends(require_authenticated_user),
    ) -> dict[str, object]:
        # Go enforces RequireAuth as blanket route-group middleware (see go-probe/httpserve.go);
        # Python's idiomatic equivalent is a per-route dependency (see COMPATIBILITY.md § pair 2,
        # item 8 — a documented, accepted divergence in *mechanism*, not in the validation rule
        # or error shape once either mechanism actually triggers). This route exists solely so
        # run_interop.sh can compare the two languages' 401 error bodies directly.
        ctx = get_request_context()
        return {
            "language": "python",
            "tenant_id": ctx.tenant_id,
            "user_id": user.user_id,
            "roles": list(ctx.roles),
        }

    @app.get("/problem-extensions")
    async def problem_extensions() -> None:
        # The same problem go-probe's /problem-extensions sends (fastapicommon 0.4+): extension
        # members after the standard ones, sorted by name, colliding/invalid names dropped.
        raise (
            ConflictException("The tenant was modified concurrently.", code="VERSION_CONFLICT")
            .with_extension("record_version", 7)
            .with_extensions(
                {
                    "active_workflows": ["wf-1", "wf-2"],
                    "Zeta": True,
                    "status": 200,
                    "traceId": "forged",
                    "TenantID": "other-tenant",
                    "bad-name": 1,
                }
            )
        )

    @app.get("/call-other")
    async def call_other() -> dict[str, object]:
        peer = os.environ.get("PROBE_PEER_URL", "")
        if not peer:
            return {"error": "PROBE_PEER_URL not configured"}
        ctx = get_request_context()
        async with create_platform_http_client() as client:
            resp = await client.get(f"{peer}/whoami")
        return {
            "language": "python",
            "own_trace_id": ctx.trace_id,
            "peer_response": resp.json(),
            "peer_status": resp.status_code,
        }

    # For the frontend SDK's ts-probe (pair 4): the problem scenarios of problem-bodies under
    # /errors, and an unprotected /echo reporting the correlation and identity headers received.
    from probe import problem_bodies

    app.mount("/errors", problem_bodies.build_app())

    @app.get("/echo")
    async def echo(request: Request) -> dict[str, object]:
        traceparent = request.headers.get("traceparent", "")
        parts = traceparent.split("-")
        trace_id = parts[1] if len(parts) == 4 and len(parts[1]) == 32 and parts[1] != "0" * 32 else ""
        return {
            "language": "python",
            "traceparent": traceparent,
            "received_trace_id": trace_id,
            "x_request_id": request.headers.get("x-request-id", ""),
            "x_session_id": request.headers.get("x-session-id", ""),
            "authorization": "authorization" in request.headers,
            "identity_headers": [h for h in IDENTITY_HEADERS if h in request.headers],
        }

    return app


# Headers only the gateway may set (gincommon's three plus IAM's ingress strip-list).
IDENTITY_HEADERS = ("x-user-id", "x-tenant-id", "x-tenant-roles", "x-departments", "x-plan", "x-feature-flags", "x-residency-mode")


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="http-serve")
    parser.add_argument("--port", default="8082")
    parser.add_argument("--peer", default="")
    parsed = parser.parse_args(args)

    if parsed.peer:
        os.environ["PROBE_PEER_URL"] = parsed.peer

    import uvicorn

    uvicorn.run(build_app(), host="0.0.0.0", port=int(parsed.port), log_level="warning")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(run(sys.argv[1:]))
