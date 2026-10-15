"""pg-migrate / pg-migrate-check: are the Go and Python migration runners interchangeable?

`pg-migrate` runs ONE pgcommon-py `migrate.Runner` operation and prints its result as JSON —
the same interface and output as go-probe's `pg-migrate` over platform-pgcommon's Runner.

`pg-migrate-check` is the orchestrator run_interop.sh calls. It drives both languages'
`pg-migrate` (and pgcommon-py's `pgcommon-migrate status` CLI) as subprocesses against shared
databases — one fresh database per scenario — while watching pg_locks / pg_stat_activity from
an admin connection, and writes one JSON report of raw observations. compare.py's `migrate`
check holds the expectations; this module only observes.

Scenarios:

- roundtrip: Go and Python alternate on one database and tracking table — each reads the
  version the other wrote; a deliberately failing migration run by one leaves the schema
  dirty at that version, the other refuses to run, then recovers with force.
- lock_keys: for several DSN spellings and tracking tables, the advisory lock each runner
  actually holds during a slow migration, read from pg_locks — Go's and Python's must be equal,
  or a Go and a Python runner would not serialise.
- serialisation: a Go and a Python runner started together on one table — the leader applies
  every migration, the follower waits on the advisory lock, then reports no change.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import asyncpg

from pgcommon.errors import MigrationDirtyError, ShortLimitError, VersionNotInSourceError
from pgcommon.migrate import Runner

from probe.util import print_json

# ── pg-migrate: one Runner operation ─────────────────────────────────────────────────────────


class _CaptureLogger:
    """Records the Runner's messages: "migrations: no changes to apply" vs "migrations:
    applied up to version N" is how a run reports whether it changed anything (go-probe reads
    the same messages from the Go Runner)."""

    def __init__(self) -> None:
        self.msgs: list[str] = []

    def debug(self, msg: str, /, **fields: object) -> None:
        pass

    def info(self, msg: str, /, **fields: object) -> None:
        self.msgs.append(msg)

    def warn(self, msg: str, /, **fields: object) -> None:
        self.msgs.append(f"warning: {msg}")

    def error(self, msg: str, /, **fields: object) -> None:
        self.msgs.append(f"error: {msg}")


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, MigrationDirtyError):
        return "dirty"
    if isinstance(exc, VersionNotInSourceError):
        return "not_in_source"
    if isinstance(exc, ShortLimitError):
        return "short_limit"
    return "error"


def _outcome(action: str, failed: bool, msgs: list[str]) -> str | None:
    if failed or action not in ("up", "down", "steps"):
        return None
    no_change = ("migrations: no changes", "migrations: nothing to roll back")
    return "no_change" if any(m.startswith(no_change) for m in msgs) else "changed"


async def _migrate_once(
    dsn: str, directory: str, table: str | None, lock_timeout: float | None, action: list[str]
) -> dict[str, Any]:
    logger = _CaptureLogger()
    runner = Runner(
        dsn,
        directory,
        logger=logger,
        table=table or None,
        lock_timeout=None if lock_timeout is None else timedelta(seconds=lock_timeout),
    )
    out: dict[str, Any] = {"language": "python", "action": " ".join(action), "ok": True}
    verb = action[0]
    try:
        if verb == "up":
            await runner.up()
        elif verb == "down":
            await runner.down(int(action[1]))
        elif verb == "steps":
            await runner.steps(int(action[1]))
        elif verb == "force":
            await runner.force(int(action[1]))
        elif verb == "version":
            version, dirty = await runner.version()
            out["version"], out["dirty"] = version, dirty
        else:
            raise SystemExit(f"pg-migrate: unknown action {verb!r}")
    except VersionNotInSourceError as exc:
        out.update(ok=False, error_code="not_in_source", error=str(exc))
        if verb == "version":
            out["version"], out["dirty"] = exc.version, exc.dirty
    except Exception as exc:  # noqa: BLE001 — a failed operation is data, not a crash
        out.update(ok=False, error_code=_error_code(exc), error=f"{type(exc).__name__}: {exc}")
    out["log"] = logger.msgs
    out["outcome"] = _outcome(verb, not out["ok"], logger.msgs)
    return out


def run_migrate(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pg-migrate")
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--table", default="")
    parser.add_argument("--lock-timeout", type=float, default=None, help="seconds")
    parser.add_argument("action", nargs="+", help="up | down N | steps N | version | force V")
    parsed = parser.parse_args(args)
    print_json(
        asyncio.run(
            _migrate_once(parsed.dsn, parsed.dir, parsed.table, parsed.lock_timeout, parsed.action)
        )
    )
    return 0


# ── pg-migrate-check: the orchestrator ───────────────────────────────────────────────────────

_POLL = 0.02  # seconds between pg_locks samples
_PROC_TIMEOUT = 120.0


class _Env:
    def __init__(self, ns: argparse.Namespace) -> None:
        self.go_probe: str = ns.go_probe
        self.host: str = ns.pg_host
        self.port: int = ns.pg_port
        self.admin_user: str = ns.admin_user
        self.admin_password: str = ns.admin_password
        self.app_user: str = ns.app_user
        self.app_password: str = ns.app_password
        self.admin_db: str = ns.admin_db
        self.migrations: str = ns.migrations
        self.failing_dir: str = ns.failing_dir

    def url(self, db: str, *, user: str | None = None, password: str | None = None) -> str:
        u, p = user or self.app_user, password or self.app_password
        return f"postgres://{quote(u, safe='')}:{quote(p, safe='')}@{self.host}:{self.port}/{db}?sslmode=disable"

    def admin_url(self, db: str) -> str:
        return self.url(db, user=self.admin_user, password=self.admin_password)


async def _recreate_db(env: _Env, db: str, setup_sql: str = "") -> int:
    """Drop and create `db` owned by the app role; return its oid (pg_locks.database)."""
    admin = await asyncpg.connect(env.admin_url(env.admin_db))
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{db}" OWNER "{env.app_user}"')
        oid = await admin.fetchval("SELECT oid FROM pg_database WHERE datname = $1", db)
    finally:
        await admin.close()
    if setup_sql:
        conn = await asyncpg.connect(env.url(db))
        try:
            await conn.execute(setup_sql)
        finally:
            await conn.close()
    return int(oid)


def _lock_key(classid: int, objid: int, objsubid: int) -> int | str:
    """The pg_advisory_lock key a pg_locks advisory row stands for: a bigint key is split
    into classid (high 32 bits) and objid (low 32 bits) with objsubid 1."""
    if objsubid == 1:
        key = (classid << 32) | objid
        return key - (1 << 64) if key >= 1 << 63 else key
    return f"int4pair:{classid},{objid}"


class _Observer:
    """Samples one database's advisory locks and lock waits from an admin connection."""

    def __init__(self, conn: asyncpg.Connection, db_oid: int, db: str) -> None:
        self.conn, self.db_oid, self.db = conn, db_oid, db

    async def sample(self) -> dict[str, Any]:
        locks = await self.conn.fetch(
            "SELECT pid, classid::bigint AS classid, objid::bigint AS objid, objsubid, granted "
            "FROM pg_locks WHERE locktype = 'advisory' AND database = $1",
            self.db_oid,
        )
        activity = await self.conn.fetch(
            "SELECT pid, wait_event_type, wait_event, query FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            self.db,
        )
        return {
            "locks": [
                {
                    "pid": r["pid"],
                    "key": _lock_key(r["classid"], r["objid"], r["objsubid"]),
                    "granted": r["granted"],
                }
                for r in locks
            ],
            "activity": [dict(r) for r in activity],
        }


async def _spawn(cmd: list[str], env: dict[str, str] | None = None) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )


async def _finish(proc: asyncio.subprocess.Process, cmd: list[str]) -> dict[str, Any]:
    stdout, stderr = await asyncio.wait_for(proc.communicate(), _PROC_TIMEOUT)
    try:
        result: dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError:
        result = {
            "ok": False,
            "error_code": "probe_crashed",
            "error": f"exit {proc.returncode}: {stderr.decode(errors='replace')[-2000:]}",
        }
    return result


def _probe_cmd(env: _Env, lang: str, dsn: str, directory: str, table: str, action: list[str]) -> list[str]:
    base = [env.go_probe] if lang == "go" else [sys.executable, "-m", "probe"]
    cmd = [*base, "pg-migrate", "--dsn", dsn, "--dir", directory]
    if table:
        cmd += ["--table", table]
    return [*cmd, *action]


async def _run_probe(
    env: _Env, lang: str, dsn: str, directory: str, action: list[str], table: str = ""
) -> dict[str, Any]:
    cmd = _probe_cmd(env, lang, dsn, directory, table, action)
    return await _finish(await _spawn(cmd), cmd)


async def _run_python_cli_status(env: _Env, dsn: str, directory: str) -> dict[str, Any]:
    """pgcommon-py's `pgcommon-migrate status` (python -m pgcommon.migrate), configured
    through its environment variables only."""
    child_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PG", "MIGRATION", "DATABASE_URL"))
    }
    child_env.update(MIGRATION_DATABASE_URL=dsn, MIGRATIONS_DIR=directory)
    proc = await _spawn([sys.executable, "-m", "pgcommon.migrate", "status"], env=child_env)
    stdout, stderr = await asyncio.wait_for(proc.communicate(), _PROC_TIMEOUT)
    return {
        "exit_code": proc.returncode,
        "stdout": stdout.decode().strip(),
        "stderr": stderr.decode().strip()[-2000:],
    }


async def _observe_while(
    observer: _Observer, procs: list[asyncio.subprocess.Process]
) -> list[dict[str, Any]]:
    samples = []
    while any(p.returncode is None for p in procs):
        samples.append(await observer.sample())
        await asyncio.sleep(_POLL)
    return samples


# ── scenario: roundtrip ──────────────────────────────────────────────────────────────────────

# (step name, runner, action). compare.py's MIGRATE_ROUNDTRIP_EXPECTED holds what each must
# report. Source: fixtures/migrations (1..3) + fixtures/migrations-failing (4: always fails).
ROUNDTRIP_STEPS: list[tuple[str, str, list[str]]] = [
    ("go_steps_2", "go", ["steps", "2"]),
    ("py_version_after_go_steps_2", "python", ["version"]),
    ("py_cli_status_after_go_steps_2", "python-cli", ["status"]),
    ("py_steps_1", "python", ["steps", "1"]),
    ("go_version_after_py_steps_1", "go", ["version"]),
    ("py_down_1", "python", ["down", "1"]),
    ("go_version_after_py_down_1", "go", ["version"]),
    ("go_up_fails_at_4", "go", ["up"]),
    ("go_version_dirty_4", "go", ["version"]),
    ("py_version_dirty_4_from_go", "python", ["version"]),
    ("py_cli_status_dirty_4_from_go", "python-cli", ["status"]),
    ("py_up_refuses_dirty_from_go", "python", ["up"]),
    ("py_force_3", "python", ["force", "3"]),
    ("go_version_after_py_force_3", "go", ["version"]),
    ("py_up_fails_at_4", "python", ["up"]),
    ("py_version_dirty_4", "python", ["version"]),
    ("go_version_dirty_4_from_py", "go", ["version"]),
    ("go_up_refuses_dirty_from_py", "go", ["up"]),
    ("go_force_3", "go", ["force", "3"]),
    ("py_version_after_go_force_3", "python", ["version"]),
    ("go_down_3", "go", ["down", "3"]),
    ("py_version_none_after_go_down_3", "python", ["version"]),
    ("py_cli_status_none_after_go_down_3", "python-cli", ["status"]),
    ("py_up_from_none_fails_at_4", "python", ["up"]),
    ("go_version_dirty_4_after_py_up", "go", ["version"]),
]


async def _roundtrip(env: _Env, work_dir: Path) -> dict[str, Any]:
    db = "interop_migrate_roundtrip"
    source = work_dir / "migrations-roundtrip"
    shutil.rmtree(source, ignore_errors=True)
    source.mkdir(parents=True)
    for d in (env.migrations, env.failing_dir):
        for f in sorted(Path(d).iterdir()):
            if f.is_file() and f.suffix == ".sql":
                shutil.copy(f, source / f.name)
    await _recreate_db(env, db)
    dsn = env.url(db)
    steps = []
    for name, runner, action in ROUNDTRIP_STEPS:
        if runner == "python-cli":
            result = await _run_python_cli_status(env, dsn, str(source))
        else:
            result = await _run_probe(env, runner, dsn, str(source), action)
        steps.append({"step": name, "runner": runner, "action": " ".join(action), "result": result})
    conn = await asyncpg.connect(env.url(db))
    try:
        never_created = await conn.fetchval("SELECT to_regclass('interop_never_created') IS NOT NULL")
    finally:
        await conn.close()
    return {"database": db, "steps": steps, "failed_migration_left_objects": never_created}


# ── scenario: lock_keys ──────────────────────────────────────────────────────────────────────


def _lock_variants(env: _Env, db: str) -> list[dict[str, Any]]:
    """DSN spellings and tracking tables whose lock keys must agree. The key is golang-migrate's
    GenerateAdvisoryLockId(databaseName, schema, table), where databaseName is the URL path
    ("/db"), "" for a URL without one (then CURRENT_DATABASE(), no slash), or "/" + the
    database a keyword/value DSN or an unparseable URL resolves to.

    Not a variant: a URL with leading whitespace. Both runners' lock-key code treats it as a
    URL, but neither driver connects with it as one — pgx and asyncpg both parse it as a
    keyword/value string, so the run goes to the libpq defaults (another server entirely)."""
    u, p, h, port = env.app_user, env.app_password, env.host, env.port
    url = env.url(db)
    # lock_inputs: golang-migrate's (databaseName, schema, table) the key must derive from —
    # compare.py recomputes the key from them independently of either library.
    slash_db = {"database_name": f"/{db}", "schema": "public", "table": "pgcommon_migrations"}
    bare_db = {**slash_db, "database_name": db}
    return [
        {"variant": "url", "dsn": url, "lock_inputs": slash_db},
        {
            "variant": "url_postgresql_scheme",
            "dsn": url.replace("postgres://", "postgresql://", 1),
            "lock_inputs": slash_db,
        },
        {
            "variant": "keyword_value",
            "dsn": f"host={h} port={port} user={u} password={p} dbname={db} sslmode=disable",
            "lock_inputs": slash_db,
        },
        {
            "variant": "keyword_value_spaced_quoted",
            "dsn": f"host={h} port = {port} user='{u}' password='{p}' dbname = '{db}' sslmode=disable",
            "lock_inputs": slash_db,
        },
        {
            "variant": "keyword_value_pgdatabase_env",
            "dsn": f"host={h} port={port} user={u} password={p} sslmode=disable",
            "env": {"PGDATABASE": db},
            "lock_inputs": slash_db,
        },
        {
            "variant": "url_percent_encoded_path",
            "dsn": f"postgres://{u}:{p}@{h}:{port}/{db.replace('_', '%5F')}?sslmode=disable",
            "lock_inputs": slash_db,
        },
        {
            "variant": "url_no_path_dbname_param",
            "dsn": f"postgres://{u}:{p}@{h}:{port}?sslmode=disable&dbname={db}",
            "lock_inputs": bare_db,
        },
        {
            "variant": "url_no_path_pgdatabase_env",
            "dsn": f"postgres://{u}:{p}@{h}:{port}?sslmode=disable",
            "env": {"PGDATABASE": db},
            "lock_inputs": bare_db,
        },
        {
            "variant": "url_custom_table_option",
            "dsn": url,
            "table": "interop_custom_migrations",
            "lock_inputs": {**slash_db, "table": "interop_custom_migrations"},
        },
        {
            "variant": "url_x_migrations_table",
            "dsn": url + "&x-migrations-table=interop_x_migrations",
            "lock_inputs": {**slash_db, "table": "interop_x_migrations"},
        },
        {
            "variant": "url_x_migrations_table_quoted_schema",
            "dsn": url
            + "&x-migrations-table=%22interop_mig%22.%22tracking%22&x-migrations-table-quoted=1",
            "setup_sql": "CREATE SCHEMA interop_mig",
            "lock_inputs": {**slash_db, "schema": "interop_mig", "table": "tracking"},
        },
    ]


async def _held_keys_during(
    env: _Env, lang: str, db: str, db_oid: int, variant: dict[str, Any]
) -> dict[str, Any]:
    cmd = _probe_cmd(env, lang, variant["dsn"], env.migrations, variant.get("table", ""), ["up"])
    child_env = {**os.environ, **variant["env"]} if "env" in variant else None
    admin = await asyncpg.connect(env.admin_url(db))
    try:
        proc = await _spawn(cmd, env=child_env)
        samples = await _observe_while(_Observer(admin, db_oid, db), [proc])
        result = await _finish(proc, cmd)
    finally:
        await admin.close()
    held = sorted({lk["key"] for s in samples for lk in s["locks"] if lk["granted"]}, key=str)
    return {"result": result, "held_keys": held, "samples": len(samples)}


async def _lock_keys(env: _Env) -> dict[str, Any]:
    db = "interop_migrate_lock"
    out = []
    for variant in _lock_variants(env, db):
        entry: dict[str, Any] = {
            "variant": variant["variant"],
            "table": variant.get("table", ""),
            "env": variant.get("env", {}),
            "lock_inputs": variant["lock_inputs"],
        }
        for lang in ("go", "python"):
            oid = await _recreate_db(env, db, variant.get("setup_sql", ""))
            entry[lang] = await _held_keys_during(env, lang, db, oid, variant)
        out.append(entry)
    return {"database": db, "variants": out}


# ── scenario: serialisation ──────────────────────────────────────────────────────────────────


async def _wait_for(
    observer: _Observer, cond: Any, procs: list[asyncio.subprocess.Process], timeout: float = 30.0
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(p.returncode is None for p in procs):
        s = await observer.sample()
        if cond(s):
            return s
        await asyncio.sleep(_POLL)
    return None


async def _serial_round(env: _Env, leader: str, follower: str) -> dict[str, Any]:
    db = f"interop_migrate_serial_{leader}_first"
    oid = await _recreate_db(env, db)
    admin = await asyncpg.connect(env.admin_url(db))
    try:
        observer = _Observer(admin, oid, db)
        # application_name only labels each runner's rows in interop_migration_effects; the
        # DSN shape is not under test here (lock_keys covers it).
        cmds = {
            lang: _probe_cmd(
                env, lang, env.url(db) + f"&application_name=interop-{lang}", env.migrations, "", ["up"]
            )
            for lang in (leader, follower)
        }
        lead = await _spawn(cmds[leader])
        # Start the follower only once the leader holds the advisory lock AND is inside the
        # slow migration — so the follower must wait for the lock, not race the leader to it.
        in_slow = await _wait_for(
            observer,
            lambda s: any(lk["granted"] for lk in s["locks"])
            and any("pg_sleep" in (a["query"] or "") and a["wait_event"] == "PgSleep" for a in s["activity"]),
            [lead],
        )
        follow = await _spawn(cmds[follower])
        samples = await _observe_while(observer, [lead, follow])
        lead_result = await _finish(lead, cmds[leader])
        follow_result = await _finish(follow, cmds[follower])
        effects = await admin.fetch(
            "SELECT version, applied_by, count(*) AS n FROM interop_migration_effects "
            "GROUP BY version, applied_by ORDER BY version"
        ) if await admin.fetchval("SELECT to_regclass('interop_migration_effects') IS NOT NULL") else []
        final = await admin.fetchrow("SELECT version, dirty FROM pgcommon_migrations")
    finally:
        await admin.close()

    leader_keys = sorted(
        {lk["key"] for lk in (in_slow or {"locks": []})["locks"] if lk["granted"]}, key=str
    )
    waits = [lk for s in samples for lk in s["locks"] if not lk["granted"]]
    wait_events = sorted(
        {
            f"{a['wait_event_type']}:{a['wait_event']}"
            for s in samples
            for a in s["activity"]
            if a["wait_event_type"] == "Lock"
        }
    )
    return {
        "database": db,
        "leader": leader,
        "follower": follower,
        "leader_was_in_slow_migration_when_follower_started": in_slow is not None,
        "leader_held_keys": leader_keys,
        "follower_waited_keys": sorted({lk["key"] for lk in waits}, key=str),
        "lock_wait_events": wait_events,
        "leader_result": lead_result,
        "follower_result": follow_result,
        "effects": [
            {"version": r["version"], "applied_by": r["applied_by"], "count": r["n"]} for r in effects
        ],
        "final_version": dict(final) if final else None,
    }


async def _check(env: _Env, work_dir: Path) -> dict[str, Any]:
    return {
        "check": "pg-migrate-check",
        "roundtrip": await _roundtrip(env, work_dir),
        "lock_keys": await _lock_keys(env),
        "serialisation": [
            await _serial_round(env, "go", "python"),
            await _serial_round(env, "python", "go"),
        ],
    }


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pg-migrate-check")
    parser.add_argument("--go-probe", required=True, help="path to the go-probe binary")
    parser.add_argument("--pg-host", default="localhost")
    parser.add_argument("--pg-port", type=int, required=True)
    parser.add_argument("--admin-user", default="postgres")
    parser.add_argument("--admin-password", default="postgres")
    parser.add_argument("--admin-db", default="interop", help="database the admin connects to")
    parser.add_argument("--app-user", default="interop_app")
    parser.add_argument("--app-password", default="interop_app")
    parser.add_argument("--migrations", required=True, help="fixtures/migrations")
    parser.add_argument("--failing-dir", required=True, help="fixtures/migrations-failing")
    parser.add_argument("--work-dir", required=True, help="scratch dir (e.g. .build/)")
    ns = parser.parse_args(args)
    report = asyncio.run(_check(_Env(ns), Path(ns.work_dir)))
    print_json(report)
    return 0
