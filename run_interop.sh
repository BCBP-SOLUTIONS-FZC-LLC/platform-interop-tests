#!/usr/bin/env bash
# Orchestrates the full cross-language compatibility check: brings up shared infra (Postgres +
# LocalStack), builds/runs both probes, diffs their outputs against the fixtures and against
# each other, prints a pass/fail table, and tears everything down. Exits non-zero if any check
# fails. See README.md for what each check proves and docs/gaps.md (COMPATIBILITY.md at the
# workspace root) for the full audit this harness backs.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURES="$ROOT/fixtures"
GO_PROBE_DIR="$ROOT/go-probe"
PY_PROBE_DIR="$ROOT/python-probe"
GO_PROBE_BIN="$ROOT/.build/go-probe"
COMPOSE_STARTED=0
GO_SERVER_PID=""
PY_SERVER_PID=""

PASS=0
FAIL=0
declare -a FAILURES=()

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); FAILURES+=("$1"); echo "  FAIL: $1"; }

cleanup() {
  [ -n "$GO_SERVER_PID" ] && kill "$GO_SERVER_PID" 2>/dev/null
  [ -n "$PY_SERVER_PID" ] && kill "$PY_SERVER_PID" 2>/dev/null
  if [ "$COMPOSE_STARTED" = "1" ]; then
    (cd "$ROOT" && docker compose down -v) >/dev/null 2>&1
  fi
}
trap cleanup EXIT

echo "== Building go-probe =="
mkdir -p "$ROOT/.build"
(cd "$GO_PROBE_DIR" && GOFLAGS=-mod=mod GOSUMDB=off GOTOOLCHAIN=local go build -o "$GO_PROBE_BIN" .) \
  || { echo "go-probe build failed"; exit 1; }

echo "== Syncing python-probe =="
(cd "$PY_PROBE_DIR" && uv sync --quiet) || { echo "python-probe sync failed"; exit 1; }
PY_RUN=(uv run --project "$PY_PROBE_DIR" python -m probe)

echo "== Bringing up Postgres + LocalStack =="
(cd "$ROOT" && docker compose up -d) || { echo "docker compose up failed"; exit 1; }
COMPOSE_STARTED=1
echo -n "waiting for postgres..."
for _ in $(seq 1 60); do
  (cd "$ROOT" && docker compose exec -T postgres pg_isready -U postgres -d interop) >/dev/null 2>&1 && break
  echo -n "."
  sleep 1
done
echo " ready"

DSN="postgres://interop_app:interop_app@localhost:55432/interop"

# --- pair 1: gucset-check ---
echo
echo "== pair 1: gucset-check =="
"$GO_PROBE_BIN" gucset-check "$FIXTURES/gucset_cases.json" > "$ROOT/.build/go_gucset.json" 2>"$ROOT/.build/go_gucset.err" \
  && pass "go-probe gucset-check ran" || fail "go-probe gucset-check crashed: $(cat "$ROOT/.build/go_gucset.err")"
"${PY_RUN[@]}" gucset-check "$FIXTURES/gucset_cases.json" > "$ROOT/.build/py_gucset.json" 2>"$ROOT/.build/py_gucset.err" \
  && pass "python-probe gucset-check ran" || fail "python-probe gucset-check crashed: $(cat "$ROOT/.build/py_gucset.err")"
python3 "$ROOT/compare.py" gucset "$ROOT/.build/go_gucset.json" "$ROOT/.build/py_gucset.json" \
  && pass "gucset-check: Go and Python agree on every case" \
  || fail "gucset-check: Go/Python disagree (see output above)"

# --- pair 1: pg-rls-check (standard + pgbouncer, two tenants) ---
echo
echo "== pair 1: pg-rls-check =="
for mode in standard pgbouncer; do
  for tenant in acme globex; do
    "$GO_PROBE_BIN" pg-rls-check --dsn "$DSN" --mode "$mode" --tenant-id "$tenant" \
      > "$ROOT/.build/go_rls_${mode}_${tenant}.json" 2>"$ROOT/.build/go_rls_${mode}_${tenant}.err" \
      && pass "go-probe pg-rls-check mode=$mode tenant=$tenant ran" \
      || fail "go-probe pg-rls-check mode=$mode tenant=$tenant crashed: $(cat "$ROOT/.build/go_rls_${mode}_${tenant}.err")"
    "${PY_RUN[@]}" pg-rls-check --dsn "$DSN" --mode "$mode" --tenant-id "$tenant" \
      > "$ROOT/.build/py_rls_${mode}_${tenant}.json" 2>"$ROOT/.build/py_rls_${mode}_${tenant}.err" \
      && pass "python-probe pg-rls-check mode=$mode tenant=$tenant ran" \
      || fail "python-probe pg-rls-check mode=$mode tenant=$tenant crashed: $(cat "$ROOT/.build/py_rls_${mode}_${tenant}.err")"
    python3 "$ROOT/compare.py" rls "$ROOT/.build/go_rls_${mode}_${tenant}.json" "$ROOT/.build/py_rls_${mode}_${tenant}.json" \
      && pass "pg-rls-check mode=$mode tenant=$tenant: identical visible row sets" \
      || fail "pg-rls-check mode=$mode tenant=$tenant: row sets differ"
  done
done

# --- pair 3: envelope-check, hmac-check, hmac bidirectional, metrics-check ---
echo
echo "== pair 3: envelope-check =="
"$GO_PROBE_BIN" envelope-check "$FIXTURES/envelope_golden.json" > "$ROOT/.build/go_env.json" 2>&1 \
  && pass "go-probe envelope-check ran" || fail "go-probe envelope-check crashed"
"${PY_RUN[@]}" envelope-check "$FIXTURES/envelope_golden.json" > "$ROOT/.build/py_env.json" 2>&1 \
  && pass "python-probe envelope-check ran" || fail "python-probe envelope-check crashed"
python3 "$ROOT/compare.py" envelope "$ROOT/.build/go_env.json" "$ROOT/.build/py_env.json" \
  && pass "envelope-check: Go and Python parse identically" \
  || fail "envelope-check: Go/Python parse differently"

echo
echo "== pair 3: hmac-check (Go signs fixture, Python verifies + reproduces) =="
"$GO_PROBE_BIN" hmac-check "$FIXTURES/hmac_vector.json" > "$ROOT/.build/go_hmac.json" 2>&1
"${PY_RUN[@]}" hmac-check "$FIXTURES/hmac_vector.json" > "$ROOT/.build/py_hmac.json" 2>&1
python3 -c "
import json,sys
go = json.load(open('$ROOT/.build/go_hmac.json'))
py = json.load(open('$ROOT/.build/py_hmac.json'))
sys.exit(0 if go['all_match'] and py['all_match'] else 1)
" && pass "hmac-check: both languages confirm the fixture's Go-computed signature" \
  || fail "hmac-check: fixture signature check failed"

echo
echo "== pair 3: hmac bidirectional (Python signs, Go verifies) =="
"${PY_RUN[@]}" hmac-sign "$FIXTURES/envelope_golden.json" "$(python3 -c "import json;print(json.load(open('$FIXTURES/hmac_vector.json'))['key_hex'])")" \
  > "$ROOT/.build/py_signed.json" 2>&1
"$GO_PROBE_BIN" hmac-verify < "$ROOT/.build/py_signed.json" > "$ROOT/.build/go_verified_py_sig.json" 2>&1
python3 -c "
import json,sys
r = json.load(open('$ROOT/.build/go_verified_py_sig.json'))
sys.exit(0 if r.get('verified') else 1)
" && pass "hmac bidirectional: Go verifies a Python-computed signature" \
  || fail "hmac bidirectional: Go rejected Python's signature"

echo
echo "== pair 3: metrics-check =="
"$GO_PROBE_BIN" metrics-check > "$ROOT/.build/go_metrics.json" 2>&1 && pass "go-probe metrics-check ran" || fail "go-probe metrics-check crashed"
"${PY_RUN[@]}" metrics-check > "$ROOT/.build/py_metrics.json" 2>&1 && pass "python-probe metrics-check ran" || fail "python-probe metrics-check crashed"
python3 "$ROOT/compare.py" metrics "$ROOT/.build/go_metrics.json" "$ROOT/.build/py_metrics.json" \
  && pass "metrics-check: identical metric name sets" \
  || fail "metrics-check: metric name sets differ"

# --- pair 2: header-check (Python pure-function; no Go equivalent, see README) ---
echo
echo "== pair 2: header-check (python-only pure-function check) =="
"${PY_RUN[@]}" header-check "$FIXTURES/header_cases.json" > "$ROOT/.build/py_header.json" 2>&1
python3 -c "
import json,sys
r = json.load(open('$ROOT/.build/py_header.json'))
sys.exit(0 if r['all_match'] else 1)
" && pass "header-check: fastapicommon.auth.validation matches every fixture case" \
  || fail "header-check: fastapicommon.auth.validation diverges from a fixture case"

# --- pair 2: live cross-service HTTP check ---
echo
echo "== pair 2: live cross-service HTTP check (gincommon server <-> fastapicommon server) =="
"$GO_PROBE_BIN" http-serve --port 18081 --peer http://localhost:18082 > "$ROOT/.build/go_server.log" 2>&1 &
GO_SERVER_PID=$!
(cd "$PY_PROBE_DIR" && uv run python -m probe http-serve --port 18082 --peer http://localhost:18081) > "$ROOT/.build/py_server.log" 2>&1 &
PY_SERVER_PID=$!

echo -n "waiting for both servers..."
for _ in $(seq 1 30); do
  curl -sf http://localhost:18081/health >/dev/null 2>&1 && curl -sf http://localhost:18082/health >/dev/null 2>&1 && break
  echo -n "."
  sleep 1
done
echo " ready"

curl -sf http://localhost:18081/health >/dev/null 2>&1 && pass "go-probe http-serve /health" || fail "go-probe http-serve /health unreachable"
curl -sf http://localhost:18082/health >/dev/null 2>&1 && pass "python-probe http-serve /health" || fail "python-probe http-serve /health unreachable"

HEADERS=(-H "x-user-id: u-1" -H "x-tenant-id: acme" -H "x-tenant-roles: admin,operator")
curl -s "${HEADERS[@]}" http://localhost:18081/whoami > "$ROOT/.build/go_whoami.json" 2>&1
curl -s "${HEADERS[@]}" http://localhost:18082/whoami > "$ROOT/.build/py_whoami.json" 2>&1
python3 "$ROOT/compare.py" whoami "$ROOT/.build/go_whoami.json" "$ROOT/.build/py_whoami.json" \
  && pass "whoami: identical tenant_id/user_id/roles for the same headers on both servers" \
  || fail "whoami: RequestContext values differ between the two servers"

GO_401=$(curl -s -o "$ROOT/.build/go_401.json" -w "%{http_code}" http://localhost:18081/whoami)
PY_401=$(curl -s -o "$ROOT/.build/py_401.json" -w "%{http_code}" http://localhost:18082/whoami-protected)
python3 "$ROOT/compare.py" error_shape "$ROOT/.build/go_401.json" "$ROOT/.build/py_401.json" "$GO_401" "$PY_401" \
  && pass "error shape: both 401s share {error,status,trace_id,request_id} (status=$GO_401/$PY_401)" \
  || fail "error shape: 401 bodies diverge (status=$GO_401/$PY_401)"

curl -s "${HEADERS[@]}" http://localhost:18081/call-other > "$ROOT/.build/go_call_other.json" 2>&1
curl -s "${HEADERS[@]}" http://localhost:18082/call-other > "$ROOT/.build/py_call_other.json" 2>&1
python3 "$ROOT/compare.py" trace_continuity "$ROOT/.build/go_call_other.json" "$ROOT/.build/py_call_other.json" \
  && pass "trace continuity: own_trace_id == peer's reported trace_id, both directions" \
  || fail "trace continuity: trace ID did not propagate correctly in one direction"

kill "$GO_SERVER_PID" "$PY_SERVER_PID" 2>/dev/null
GO_SERVER_PID=""
PY_SERVER_PID=""

echo
echo "================================================================"
echo "  $PASS passed, $FAIL failed"
echo "================================================================"
if [ "$FAIL" -gt 0 ]; then
  echo "Failures:"
  printf '  - %s\n' "${FAILURES[@]}"
  exit 1
fi
exit 0
