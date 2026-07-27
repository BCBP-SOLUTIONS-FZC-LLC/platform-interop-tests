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
  docker-compose.yml       # Postgres (with an RLS-protected test table) + LocalStack (SNS/SQS, provisioned but not yet wired into a check)
  docker/init-rls.sql      # schema + seed rows for pg-rls-check
  fixtures/
    gucset_cases.json      # pair 1 — GUCSet.Validate() table-driven cases
    envelope_golden.json   # pair 3 — a real Go-emitted Envelope.JSON() capture
    hmac_vector.json        # pair 3 — a real Go-computed HMAC signature over the envelope above
    header_cases.json      # pair 2 — header validation/parsing test vectors
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

Requires Go, `uv`, and Docker. Builds both probes, brings up Postgres + LocalStack, runs every
check, prints a pass/fail table, tears down. Exits non-zero if anything fails.

## What each subcommand proves

| Subcommand | Pair | What it proves |
|---|---|---|
| `gucset-check` | 1 | Both languages' `GUCSet.Validate()` agree on accept/reject for every case in `gucset_cases.json` |
| `pg-rls-check` | 1 | A Go connection and a Python connection, each scoped to the same `tenant_id` under the *same* real Postgres RLS policy, see the *same* row set — in both standard (session-scoped `is_local=false`) and PgBouncer-simulated (transaction-scoped `is_local=true`) modes |
| `envelope-check` | 3 | Both languages parse a real Go-emitted envelope identically, field-for-field |
| `hmac-check` | 3 | Both languages reproduce and verify a real Go-computed HMAC signature over the same envelope |
| `hmac-sign` / `hmac-verify` | 3 | The *reverse* direction: python-probe signs, go-probe verifies — proving the check isn't one-directional |
| `metrics-check` | 3 | Both languages register the exact same Prometheus metric name set (accounting for a real asymmetry: Go's `client_golang` omits a Counter/Gauge/Histogram's metric family from `Gather()` until it has a sample, while Python's `prometheus_client` lists family names up front but strips the `_total` suffix from Counter family names specifically — both probes correct for their own library's quirk so the *compared* name lists are the literal strings a Prometheus scrape actually exposes) |
| `header-check` | 2 | **Python-only.** `fastapicommon.auth.validation`'s functions against every case in `header_cases.json`. Go's equivalent logic (`internal/core/domain/validation.go`) is unexported and cannot be imported by this separate module — see "Known gaps" below |
| `http-serve` | 2 | A live minimal server in each language (`gincommon`/`fastapicommon`, real middleware, real OTel). `run_interop.sh` drives both with the same request and compares: `/whoami`'s parsed identity fields, both languages' 401 error JSON shape (`{error,status,trace_id,request_id}`, message text allowed to differ), and end-to-end `traceparent` continuity in *both* call directions (`/call-other` propagates headers to the peer and echoes the peer's response) |

## Known gaps (see COMPATIBILITY.md for the full residual-risk list)

- **No Go `header-check` subcommand.** Go's header validation/parsing rules live in
  `platform-gincommon`'s unexported `internal/core/domain` package — Go's own module-privacy
  rules forbid a separate module (this one) from importing it. The contract is still verified,
  just only observably, through `http-serve`'s live HTTP surface, not as a fast pure-function
  check the way Python's `header-check` is.
- **LocalStack is provisioned but not yet wired into a check.** No SNS/SQS interop check exists
  yet in this harness (the envelope/HMAC checks already cover wire-format compatibility without
  needing a live broker). A live publish-from-Go/consume-from-Python round trip would be a
  natural extension.
- **Trace continuity is proven bidirectionally (Go→Python and Python→Go), not as a single 3-hop
  A→B→C chain in one request.** Each hop's propagation mechanism is proven symmetric and correct
  in both directions independently; a literal 3-hop chain would exercise the same mechanism
  twice in one request rather than prove anything new, but hasn't been built.

## CI

`.github/workflows/interop.yml` is a reusable `workflow_call` target — see the sibling `.github`
workflow snippet documented in COMPATIBILITY.md for how each of the six libraries' own CI calls
into it on every PR (checkout all seven repos as siblings using the layout above, then run
`./run_interop.sh`).
