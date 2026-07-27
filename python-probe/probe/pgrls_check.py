from __future__ import annotations

import argparse
import asyncio

from pgcommon import Config, GUCSet, Pool, guc_set_from_context, with_guc_set

from probe.util import print_json


async def _check(dsn: str, mode: str, tenant_id: str) -> dict[str, object]:
    config = Config(
        dsn=dsn,
        guc_provider=guc_set_from_context,
        pgbouncer_mode=(mode == "pgbouncer"),
        max_conns=2,
        min_conns=1,
    )
    pool = await Pool.create(config)
    try:
        with with_guc_set(GUCSet(tenant_id=tenant_id)):

            async def _op(conn: object) -> list[str]:
                rows = await conn.fetch("SELECT id FROM interop_rls_test ORDER BY id")  # type: ignore[attr-defined]
                return [row["id"] for row in rows]

            visible_ids = await pool.with_conn(_op)
    finally:
        await pool.drain_and_close()

    return {
        "language": "python",
        "check": "pg-rls-check",
        "mode": mode,
        "tenant_id": tenant_id,
        "visible_ids": visible_ids,
    }


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pg-rls-check")
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--mode", choices=["standard", "pgbouncer"], default="standard")
    parser.add_argument("--tenant-id", default="")
    parsed = parser.parse_args(args)

    result = asyncio.run(_check(parsed.dsn, parsed.mode, parsed.tenant_id))
    print_json(result)
    return 0
