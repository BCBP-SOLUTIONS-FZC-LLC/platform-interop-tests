from __future__ import annotations

import argparse
import os

from fastapi import Depends, FastAPI

from fastapicommon.auth import CurrentUser, require_authenticated_user
from fastapicommon.bootstrap import create_platform_app
from fastapicommon.context import get_request_context
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

    return app


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
