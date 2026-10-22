# platform-interop-tests

A shared cross-language regression harness for the three Go↔Python library pairs that stand
between the workspace's mixed Go/Python fleet and its shared Postgres/HTTP/SNS-SQS surfaces:

| # | Surface | Go | Python |
|---|---|---|---|
| 1 | Postgres access | `platform-pgcommon` | `platform-pgcommon-py` |
| 2 | HTTP/gRPC middleware | `platform-gincommon` | `platform-fastapicommon` |
| 3 | Event messaging | `platform-events` | `platform-eventcommon` |

Each Python port was built by translating Go behavior rather than sharing code, so a load-bearing
detail (a GUC key, a header casing, a JSON field order, a SQLSTATE list) can drift silently. This
repo exists so that drift fails a build instead of surfacing in production. See
`COMPATIBILITY.md` at the workspace root for the full audit this harness backs, including which
items are genuinely proven here vs. verified by code inspection only.

## Layout

```
platform-interop-tests/
  docker-compose.yml       # Postgres (RLS-protected test table; also the shared outbox) + floci (SNS/SQS emulator, as in platform-events)
  docker/init-rls.sql      # schema + seed rows for pg-rls-check
  fixtures/
    gucset_cases.json      # pair 1 — GUCSet.Validate() table-driven cases
    migrations/            # pair 1 — tiny up/down migrations (2 is slow: pg_sleep) for pg-migrate-check
    migrations-failing/    # pair 1 — a migration that always fails, overlaid on migrations/ for the dirty-state round trip
    envelope_golden.json   # pair 3 — a real Go-emitted Envelope.JSON() capture
    hmac_vector.json        # pair 3 — a real Go-computed HMAC signature over the envelope above
    header_cases.json      # pair 2 — header validation/parsing test vectors
    events_wire_cases.json # pair 3 — events published over SNS/SQS: every envelope field, payload
                           #          shapes written as raw JSON literals (Go sends them byte for byte)
    events_raw_bodies.json # pair 3 — hand-crafted SQS bodies for the consumer differential
  go-probe/                # one Go binary, subcommands below
  python-probe/            # one Python package (`python -m probe`), mirroring subcommands
  compare.py               # stdlib-only comparison logic, one function per check type
  run_interop.sh           # orchestrates: build both probes -> docker compose up -> run every
                            # check -> diff -> pass/fail report -> tear down
```

This repo is a **sibling** of the six libraries above, not nested inside any of them — it depends
on all six but none of them depend on it.

## Running locally

Requires this exact sibling layout on disk (matches the CI checkout in
`.github/workflows/interop.yml`):

```
XPertSharedLib/
  platform-events/
  platform-gincommon/
  platform-pgcommon/
  platform-interop-tests/   <- this repo
  python/
    platform-eventcommon/
    platform-fastapicommon/
    platform-pgcommon-py/
```

```bash
./run_interop.sh
```

Requires Go, `uv`, and Docker. Builds both probes, brings up Postgres + floci, runs every
check, prints a pass/fail table, tears down. Exits non-zero if anything fails.

Host ports default to Postgres 55432, floci 54566 and the two `http-serve` servers
18081 (Go) / 18082 (Python). Override them where another stack already holds one, and give the
compose project its own name to run beside another checkout:

```bash
COMPOSE_PROJECT_NAME=interop-mine INTEROP_POSTGRES_PORT=56432 INTEROP_FLOCI_PORT=56566 \
  INTEROP_GO_HTTP_PORT=18091 INTEROP_PY_HTTP_PORT=18092 ./run_interop.sh
```

`python-probe` resolves pgcommon-py and eventcommon in one environment, so eventcommon's own
`pgcommon` requirement must admit the pgcommon-py checkout: pgcommon-py 0.3.x needs an
eventcommon that allows `pgcommon<0.4` (eventcommon's main still pins `<0.3.0` until its
pgcommon-0.3 compatibility change merges; `uv sync` fails before any check runs otherwise).
pgcommon-py 0.3.0 itself requires `asyncpg>=0.30,<0.32`, mirrored by python-probe's floor.

## What each subcommand proves

| Subcommand | Pair | What it proves |
|---|---|---|
| `gucset-check` | 1 | Both languages' `GUCSet.Validate()` agree, for every case in `gucset_cases.json`, on accept/reject and on *which* error fired: user without tenant (`inconsistent`), tenant roles without tenant (`roles_without_tenant`), empty role, comma in a role (`invalid_role`), and a NUL in `user_id`/`tenant_id`/a role (`invalid_value`) — including each rule's precedence when several apply. Invalid values use NUL only: a lone surrogate or invalid UTF-8 decodes differently from JSON in Go and Python. python-probe maps every `pgcommon.IDENTITY_ERRORS` member and reports an unmapped one (or any unexpected exception) as `unknown:…` instead of crashing |
| `pg-rls-check` | 1 | A Go connection and a Python connection, each scoped to the same `tenant_id` under the *same* real Postgres RLS policy, see the *same* row set — in both standard (session-scoped `is_local=false`) and PgBouncer-simulated (transaction-scoped `is_local=true`) modes |
| `pg-migrate` | 1 | One `migrate.Runner` operation (`up`, `down N`, `steps N`, `version`, `force V`) in each language, reported as JSON — the building block `pg-migrate-check` drives |
| `pg-migrate-check` | 1 | **python-probe only, an orchestrator** (it runs both languages' `pg-migrate` and pgcommon-py's `pgcommon-migrate status` CLI as subprocesses, one fresh database per scenario, watching `pg_locks`/`pg_stat_activity` as the superuser); `compare.py migrate` holds the expectations. **roundtrip:** on one database and tracking table, Go applies 1–2 and Python's `version()`/CLI `status` read 2; Python applies 3 and Go reads 3; Python rolls back one and Go reads 2; a deliberately failing migration run by Go leaves the schema dirty at 4 for both, Python refuses (`dirty`) and recovers with `force 3`, then the same with the roles swapped; Go rolls everything back and Python reads "no version". **lock_keys:** for URL, `postgresql://`, keyword/value (plain, spaced and quoted, `PGDATABASE`-only), a percent-encoded path, a URL without a path (database from `?dbname=` or `PGDATABASE`), a custom table (Runner option, `x-migrations-table`, quoted `"schema"."table"`), the advisory-lock key each runner actually holds while migrating equals the other's and golang-migrate's key recomputed from (database name, schema, table). **serialisation:** a Go and a Python runner started together on one table (each language leading once) — the follower blocks on the leader's key, every migration is applied exactly once (each migration records who applied it), the final version is 3, clean. Go's runner takes the lock per migration and Python's for the whole run (pgcommon-py ARCHITECTURE.md § Divergences from Go), so a waiting Python runner applies the migrations a Go leader has not started yet; this is declared as `GO_LOCKS_PER_MIGRATION` in `compare.py`, and the check fails if it stops being true |
| `pg-metrics-check` | 1 | Tier 1 `platform_db_*` parity. Each probe calls `InitWithIdentity` / `init_with_identity` on a fresh registry, opens a real pool and runs one query (so every family is gatherable), then reports its registry (`pgmetrics.Registry()` / `pgcommon.metrics_registry.registry()`: name, type, tier, status, help, required/approved labels, allowed label values) and the registered families (type, help, label names in declared order — identity labels first; Go's const/variable split is not part of the contract — histogram buckets, and the values of `state`/`outcome`/`reason`). Everything must be equal except the Go label values pgcommon-py declares it cannot emit (`SKIPPED_IN_PYTHON`, e.g. `platform_db_pool_connections{state="constructing"}`); a declaration this check can observe must hold |
| `envelope-check` | 3 | Both languages parse a real Go-emitted envelope identically, field-for-field |
| `hmac-check` | 3 | Both languages reproduce and verify a real Go-computed HMAC signature over the same envelope |
| `hmac-sign` / `hmac-verify` | 3 | The *reverse* direction: python-probe signs, go-probe verifies — proving the check isn't one-directional |
| `metrics-check` | 3 | Tier 1 (`platform_*`) metric parity under the Enterprise Platform Observability Standard. Go reports every Tier 1 entry of `events.MetricsRegistry()` after a successful `events.InitMetrics`. Python reports the `platform_*` families `eventcommon.metrics.init_metrics` registers, plus `GO_ONLY_METRICS`, the Go metrics the port deliberately does not emit. Go's set must equal Python's plus `GO_ONLY_METRICS`. The removed pre-standard names (`events_*`, `outbox_*`, `sqs_*`) are not compared. A pre-standard eventcommon fails with an explicit "needs the port" message. |
| `events-publish` / `events-consume` | 3 | **Live SNS/SQS round trip, both directions** (`events-wire-check`). One language publishes every case of `events_wire_cases.json` through floci, the other consumes; `compare.py events_wire` checks every event arrived once with the same envelope fields (incl. Go's nanosecond `time`) and payload (Unicode, HTML characters, U+2028, existing escapes, integers beyond 64 bits, float literals, arrays, scalars, `null`, 150 KB), the routing attributes, the handler's trace ID / tenant GUC / baggage, and a span link to the producer's trace. Six scenarios per direction: single publish, HMAC signature attribute (signed by the producer, verified by the consumer), batch publish, Glue schema-registry codec, FIFO per-group order, FIFO batch + codec |
| `events-consume` on `events_raw_bodies.json` | 3 | **Consumer differential** (`events-raw-check`): 37 hand-crafted SQS bodies (unknown fields, missing / unusual `time`, mixed-case keys, SNS notification wrapper, `NaN`, NUL tenant, …) sent to a Go and a Python consumer: both handle or drop each body alike and hand the handler the same envelope |
| `events-outbox-enqueue` / `events-outbox-run` | 3 | **Shared outbox** (`events-outbox-check`): one language enqueues (half under one ordering key) into the outbox schema, the other language's runner publishes to SNS, the first consumes — every event handled, ordered-key events in enqueue order. Proves the two outbox schemas and migration tracking are one |
| `events-topology` / `events-send-raw` | — | Python-only helpers: create a topic + raw-delivery queue (FIFO with `--fifo`) and send the raw bodies |
| `header-check` | 2 | **Python-only.** `fastapicommon.auth.validation`'s functions against every case in `header_cases.json`. Go's equivalent logic (`internal/core/domain/validation.go`) is unexported and cannot be imported by this separate module — see "Known gaps" below |
| `http-serve` | 2 | A live minimal server in each language (`gincommon`/`fastapicommon`, real middleware, real OTel). `run_interop.sh` drives both with the same request and compares: `/whoami`'s parsed identity fields, both languages' 401 on the same error contract: the same RFC 9457 problem (`application/problem+json`; identical `type`, `title`, `status`, `detail`, `errorCode`, all required members, no `tenantId`; `instance`/`traceId`/`timestamp` may differ), both languages' 409 with RFC 9457 extension members (`/problem-extensions`, gincommon `v2.1.0`+ / fastapicommon `0.4.0`+): the same body byte for byte but the `timestamp`, with the extension members after the standard ones, sorted by name, and every standard-member or invalid name dropped, and end-to-end `traceparent` continuity in *both* call directions (`/call-other` propagates headers to the peer and echoes the peer's response) |

## Known gaps (see COMPATIBILITY.md for the full residual-risk list)

- **No Go `header-check` subcommand.** Go's header validation/parsing rules live in
  `platform-gincommon`'s unexported `internal/core/domain` package — Go's own module-privacy
  rules forbid a separate module (this one) from importing it. The contract is still verified,
  just only observably, through `http-serve`'s live HTTP surface, not as a fast pure-function
  check the way Python's `header-check` is.
- **Trace continuity is proven bidirectionally (Go→Python and Python→Go), not as a single 3-hop
  A→B→C chain in one request.** Each hop's propagation mechanism is proven symmetric and correct
  in both directions independently; a literal 3-hop chain would exercise the same mechanism
  twice in one request rather than prove anything new, but hasn't been built.

## CI

`.github/workflows/interop.yml` is a reusable `workflow_call` target — see the sibling `.github`
workflow snippet documented in COMPATIBILITY.md for how each of the six libraries' own CI calls
into it on every PR (checkout all seven repos as siblings using the layout above, then run
`./run_interop.sh`).
