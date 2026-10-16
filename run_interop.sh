#!/usr/bin/env bash
# Orchestrates the full cross-language compatibility check: brings up shared infra (Postgres +
# floci), builds/runs both probes, diffs their outputs against the fixtures and against
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

# Host ports are overridable, for a machine where another stack already holds a default.
PG_PORT="${INTEROP_POSTGRES_PORT:-55432}"
AWS_PORT="${INTEROP_FLOCI_PORT:-54566}"
export INTEROP_POSTGRES_PORT="$PG_PORT" INTEROP_FLOCI_PORT="$AWS_PORT"
GO_HTTP_PORT="${INTEROP_GO_HTTP_PORT:-18081}"
PY_HTTP_PORT="${INTEROP_PY_HTTP_PORT:-18082}"

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
# Build from a copy whose import paths and require/replace lines match the module path each
# sibling checkout declares (e.g. .../platform-pgcommon/v2 from v2.0.0 on). The probe then builds
# against any mix of major versions — one library's PR moving to /vN while the others' main is
# still v1 — so a cross-repo major bump cannot deadlock this check. The copy's replace targets
# are absolute (the copy sits at a different depth than go-probe/).
GO_PROBE_SRC="$ROOT/.build/go-probe-src"
rm -rf "$GO_PROBE_SRC" && mkdir -p "$GO_PROBE_SRC" && cp "$GO_PROBE_DIR"/*.go "$GO_PROBE_DIR"/go.mod "$GO_PROBE_SRC"/ \
  || { echo "go-probe build failed: cannot copy sources"; exit 1; }
for lib in platform-events platform-gincommon platform-pgcommon; do
  base="github.com/BCBP-SOLUTIONS-FZC-LLC/$lib"
  dir="$(cd "$ROOT/../$lib" 2>/dev/null && pwd)" \
    || { echo "go-probe build failed: sibling checkout ../$lib not found"; exit 1; }
  actual=$(awk '$1 == "module" { print $2; exit }' "$dir/go.mod")
  case "$actual" in
    "$base") ;;
    "$base"/v[2-9]|"$base"/v[1-9][0-9])
      echo "  $lib declares $actual"
      major=${actual##*/v}
      perl -pi -e 's#"\Q'"$base"'\E/#"'"$actual"'/#g' "$GO_PROBE_SRC"/*.go
      perl -pi -e 's#^(\s+)\Q'"$base"'\E\s+v\S+#${1}'"$actual"' v'"$major"'.0.0#' "$GO_PROBE_SRC/go.mod" ;;
    *) echo "go-probe build failed: ../$lib/go.mod declares '${actual}', want ${base}[/vN]"; exit 1 ;;
  esac
  perl -pi -e 's#^replace \Q'"$base"'\E => \S+#replace '"$actual"' => '"$dir"'#' "$GO_PROBE_SRC/go.mod"
done
(cd "$GO_PROBE_SRC" && GOFLAGS=-mod=mod GOSUMDB=off GOTOOLCHAIN=local go build -o "$GO_PROBE_BIN" .) \
  || { echo "go-probe build failed"; exit 1; }

echo "== Syncing python-probe =="
(cd "$PY_PROBE_DIR" && uv sync --quiet) || { echo "python-probe sync failed"; exit 1; }
PY_RUN=(uv run --project "$PY_PROBE_DIR" python -m probe)

echo "== Bringing up Postgres + floci =="
(cd "$ROOT" && docker compose up -d) || { echo "docker compose up failed"; exit 1; }
COMPOSE_STARTED=1
echo -n "waiting for postgres..."
for _ in $(seq 1 60); do
  (cd "$ROOT" && docker compose exec -T postgres pg_isready -U postgres -d interop) >/dev/null 2>&1 && break
  echo -n "."
  sleep 1
done
echo " ready"

DSN="postgres://interop_app:interop_app@localhost:${PG_PORT}/interop"

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

# --- pair 1: pg-migrate-check (Go and Python migration runners on shared databases) ---
# python-probe's pg-migrate-check orchestrates: it runs go-probe's and its own pg-migrate (one
# Runner operation each) and pgcommon-py's `pgcommon-migrate status` CLI against one fresh
# database per scenario, as the postgres superuser watches pg_locks / pg_stat_activity, and
# reports raw observations. compare.py holds the expectations, one pass/fail per scenario.
echo
echo "== pair 1: pg-migrate-check =="
"${PY_RUN[@]}" pg-migrate-check --go-probe "$GO_PROBE_BIN" --pg-port "$PG_PORT" \
  --migrations "$FIXTURES/migrations" --failing-dir "$FIXTURES/migrations-failing" --work-dir "$ROOT/.build" \
  > "$ROOT/.build/pg_migrate.json" 2>"$ROOT/.build/pg_migrate.err" \
  && pass "pg-migrate-check orchestrator ran" \
  || fail "pg-migrate-check orchestrator crashed: $(tail -20 "$ROOT/.build/pg_migrate.err")"
python3 "$ROOT/compare.py" migrate "$ROOT/.build/pg_migrate.json" roundtrip \
  && pass "pg-migrate-check roundtrip: each runner reads, refuses (dirty) and force-recovers what the other wrote" \
  || fail "pg-migrate-check roundtrip: Go/Python runners disagree on shared migration state"
python3 "$ROOT/compare.py" migrate "$ROOT/.build/pg_migrate.json" lock_keys \
  && pass "pg-migrate-check lock_keys: Go and Python hold the same advisory-lock key for every DSN/table variant" \
  || fail "pg-migrate-check lock_keys: Go and Python lock different keys"
python3 "$ROOT/compare.py" migrate "$ROOT/.build/pg_migrate.json" serialisation \
  && pass "pg-migrate-check serialisation: concurrent Go/Python runners serialise, every migration applied once" \
  || fail "pg-migrate-check serialisation: concurrent Go/Python runners did not serialise"

# --- pair 1: pg-metrics-check (Tier 1 platform_db_* registry + registered families) ---
echo
echo "== pair 1: pg-metrics-check =="
"$GO_PROBE_BIN" pg-metrics-check --dsn "$DSN" > "$ROOT/.build/go_pgmetrics.json" 2>"$ROOT/.build/go_pgmetrics.err" \
  && pass "go-probe pg-metrics-check ran" || fail "go-probe pg-metrics-check crashed: $(cat "$ROOT/.build/go_pgmetrics.err")"
"${PY_RUN[@]}" pg-metrics-check --dsn "$DSN" > "$ROOT/.build/py_pgmetrics.json" 2>"$ROOT/.build/py_pgmetrics.err" \
  && pass "python-probe pg-metrics-check ran" || fail "python-probe pg-metrics-check crashed: $(cat "$ROOT/.build/py_pgmetrics.err")"
python3 "$ROOT/compare.py" pgmetrics "$ROOT/.build/go_pgmetrics.json" "$ROOT/.build/py_pgmetrics.json" \
  && pass "pg-metrics-check: identical platform_db_* registry and registered families (names, types, help, labels, buckets)" \
  || fail "pg-metrics-check: platform_db_* metric contracts differ"

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

# --- pair 3: events-wire-check (live SNS/SQS through floci) ---
echo
echo "== pair 3: events-wire-check (live SNS/SQS: each language publishes, the other consumes) =="
AWS_ENDPOINT="http://localhost:$AWS_PORT"
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_REGION=us-east-1
EVENTS_CASES="$FIXTURES/events_wire_cases.json"
EVENTS_HMAC_KEY="interop-shared-hmac-key-0123456789abcdef"
OUTBOX_DSN="postgres://postgres:postgres@localhost:$PG_PORT/interop?sslmode=disable"
echo -n "waiting for floci..."
for _ in $(seq 1 60); do
  [ "$(docker inspect --format '{{.State.Health.Status}}' "$(cd "$ROOT" && docker compose ps -q floci)" 2>/dev/null)" = healthy ] && break
  echo -n "."
  sleep 1
done
echo " ready"

probe() { # probe <go|py> <subcommand> [args...]
  local lang=$1; shift
  if [ "$lang" = go ]; then "$GO_PROBE_BIN" "$@"; else "${PY_RUN[@]}" "$@"; fi
}
json_field() { python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])" "$1" "$2"; }
json_len() { python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))[sys.argv[2]]))" "$1" "$2"; }

# events_scenario <label> <producer> <consumer> <options: comma-separated batch,codec,hmac,fifo>
events_scenario() {
  local label=$1 producer=$2 consumer=$3 options=$4
  local tag="${producer}2${consumer}_$(printf '%s' "$label" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9' '_')"
  local topo="$ROOT/.build/ev_topo_$tag.json" sent="$ROOT/.build/ev_sent_$tag.json" got="$ROOT/.build/ev_got_$tag.json"
  local topo_args=() pub_args=() cons_args=()
  case ",$options," in *,fifo,*) topo_args+=(--fifo); pub_args+=(--fifo) ;; esac
  case ",$options," in *,batch,*) pub_args+=(--mode batch) ;; esac
  case ",$options," in *,codec,*) pub_args+=(--codec glue); cons_args+=(--codec glue) ;; esac
  case ",$options," in *,hmac,*) pub_args+=(--hmac "$EVENTS_HMAC_KEY"); cons_args+=(--hmac "$EVENTS_HMAC_KEY") ;; esac
  if ! probe py events-topology --endpoint "$AWS_ENDPOINT" --name "ev-$producer-$consumer" ${topo_args[@]+"${topo_args[@]}"} \
      > "$topo" 2>"$topo.err"; then
    fail "events-wire $label ($producer->$consumer): topology setup failed: $(tail -3 "$topo.err")"; return
  fi
  if ! probe "$producer" events-publish --endpoint "$AWS_ENDPOINT" --topic "$(json_field "$topo" topic_arn)" \
      --cases "$EVENTS_CASES" ${pub_args[@]+"${pub_args[@]}"} > "$sent" 2>"$sent.err"; then
    fail "events-wire $label ($producer->$consumer): $producer publish crashed: $(tail -3 "$sent.err")"; return
  fi
  if ! probe "$consumer" events-consume --endpoint "$AWS_ENDPOINT" --queue "$(json_field "$topo" queue_url)" \
      --n "$(json_len "$sent" sent)" --timeout 60s ${cons_args[@]+"${cons_args[@]}"} > "$got" 2>"$got.err"; then
    fail "events-wire $label ($producer->$consumer): $consumer consume crashed: $(tail -3 "$got.err")"; return
  fi
  python3 "$ROOT/compare.py" events_wire "$sent" "$got" "$options" \
    && pass "events-wire $label: $producer publishes, $consumer handles every event identically" \
    || fail "events-wire $label ($producer->$consumer): see output above"
}

for direction in "go py" "py go"; do
  set -- $direction
  events_scenario "single publish" "$1" "$2" ""
  events_scenario "HMAC signature attribute" "$1" "$2" "hmac"
  events_scenario "batch publish" "$1" "$2" "batch"
  events_scenario "Glue schema-registry codec" "$1" "$2" "codec"
  events_scenario "FIFO per-group order" "$1" "$2" "fifo"
  events_scenario "FIFO batch with Glue codec" "$1" "$2" "fifo,batch,codec"
done

echo
echo "== pair 3: events-raw-check (hand-crafted SQS bodies: same handle/drop decision) =="
for lang in go py; do
  probe py events-topology --endpoint "$AWS_ENDPOINT" --name "ev-raw-$lang" --queue-only > "$ROOT/.build/ev_raw_q_$lang.json" 2>&1 \
    && probe py events-send-raw --endpoint "$AWS_ENDPOINT" --queue "$(json_field "$ROOT/.build/ev_raw_q_$lang.json" queue_url)" \
         --bodies "$FIXTURES/events_raw_bodies.json" > /dev/null 2>&1 \
    && probe "$lang" events-consume --endpoint "$AWS_ENDPOINT" --queue "$(json_field "$ROOT/.build/ev_raw_q_$lang.json" queue_url)" \
         --n 999 --timeout 15s > "$ROOT/.build/ev_raw_$lang.json" 2>"$ROOT/.build/ev_raw_$lang.err" \
    && pass "events-raw: $lang consumer ran" \
    || fail "events-raw: $lang consumer crashed: $(tail -3 "$ROOT/.build/ev_raw_$lang.err" 2>/dev/null)"
done
python3 "$ROOT/compare.py" events_raw "$ROOT/.build/ev_raw_go.json" "$ROOT/.build/ev_raw_py.json" \
  && pass "events-raw: Go and Python handle or drop every hand-crafted body alike, with the same envelope" \
  || fail "events-raw: Go/Python consumers disagree (see output above)"

echo
echo "== pair 3: events-outbox-check (shared outbox: one language enqueues, the other's runner publishes) =="
for direction in "go py" "py go"; do
  set -- $direction
  enq_lang=$1 run_lang=$2
  topo="$ROOT/.build/ev_ob_topo_$enq_lang.json" enq="$ROOT/.build/ev_ob_enq_$enq_lang.json" got="$ROOT/.build/ev_ob_got_$enq_lang.json"
  if probe py events-topology --endpoint "$AWS_ENDPOINT" --name "ev-outbox-$enq_lang" > "$topo" 2>&1 \
     && probe "$enq_lang" events-outbox-enqueue --dsn "$OUTBOX_DSN" --count 5 > "$enq" 2>"$enq.err" \
     && probe "$run_lang" events-outbox-run --dsn "$OUTBOX_DSN" --endpoint "$AWS_ENDPOINT" --topic "$(json_field "$topo" topic_arn)" --for 5s \
          > /dev/null 2>"$got.err" \
     && probe "$enq_lang" events-consume --endpoint "$AWS_ENDPOINT" --queue "$(json_field "$topo" queue_url)" \
          --n "$(json_len "$enq" ids)" --timeout 30s > "$got" 2>>"$got.err"; then
    python3 "$ROOT/compare.py" events_outbox "$enq" "$got" \
      && pass "events-outbox: $enq_lang enqueues, $run_lang's runner publishes, every event handled (ordered key kept)" \
      || fail "events-outbox ($enq_lang enqueue, $run_lang runner): see output above"
  else
    fail "events-outbox ($enq_lang enqueue, $run_lang runner) crashed: $(tail -3 "$enq.err" "$got.err" 2>/dev/null)"
  fi
done

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
# gincommon v1.4.0 requires an observability identity at startup (domain +
# registry environment); v1.3 ignores these, so the probe runs against both.
OBSERVABILITY_DOMAIN=iam ENVIRONMENT=test \
  "$GO_PROBE_BIN" http-serve --port "$GO_HTTP_PORT" --peer "http://localhost:$PY_HTTP_PORT" > "$ROOT/.build/go_server.log" 2>&1 &
GO_SERVER_PID=$!
# fastapicommon likewise requires the observability domain for its platform metrics.
(cd "$PY_PROBE_DIR" && OBSERVABILITY_DOMAIN=iam ENVIRONMENT=test uv run python -m probe http-serve --port "$PY_HTTP_PORT" --peer "http://localhost:$GO_HTTP_PORT") > "$ROOT/.build/py_server.log" 2>&1 &
PY_SERVER_PID=$!

echo -n "waiting for both servers..."
for _ in $(seq 1 30); do
  curl -sf "http://localhost:${GO_HTTP_PORT}/health" >/dev/null 2>&1 && curl -sf "http://localhost:${PY_HTTP_PORT}/health" >/dev/null 2>&1 && break
  echo -n "."
  sleep 1
done
echo " ready"

curl -sf "http://localhost:${GO_HTTP_PORT}/health" >/dev/null 2>&1 && pass "go-probe http-serve /health" || fail "go-probe http-serve /health unreachable"
curl -sf "http://localhost:${PY_HTTP_PORT}/health" >/dev/null 2>&1 && pass "python-probe http-serve /health" || fail "python-probe http-serve /health unreachable"

HEADERS=(-H "x-user-id: u-1" -H "x-tenant-id: acme" -H "x-tenant-roles: admin,operator")
curl -s "${HEADERS[@]}" "http://localhost:${GO_HTTP_PORT}/whoami" > "$ROOT/.build/go_whoami.json" 2>&1
curl -s "${HEADERS[@]}" "http://localhost:${PY_HTTP_PORT}/whoami" > "$ROOT/.build/py_whoami.json" 2>&1
python3 "$ROOT/compare.py" whoami "$ROOT/.build/go_whoami.json" "$ROOT/.build/py_whoami.json" \
  && pass "whoami: identical tenant_id/user_id/roles for the same headers on both servers" \
  || fail "whoami: RequestContext values differ between the two servers"

GO_401=$(curl -s -o "$ROOT/.build/go_401.json" -w "%{http_code}" "http://localhost:${GO_HTTP_PORT}/whoami")
PY_401=$(curl -s -o "$ROOT/.build/py_401.json" -w "%{http_code}" "http://localhost:${PY_HTTP_PORT}/whoami-protected")
GO_401_CT=$(curl -s -o /dev/null -w "%{content_type}" "http://localhost:${GO_HTTP_PORT}/whoami")
PY_401_CT=$(curl -s -o /dev/null -w "%{content_type}" "http://localhost:${PY_HTTP_PORT}/whoami-protected")
python3 "$ROOT/compare.py" error_shape "$ROOT/.build/go_401.json" "$ROOT/.build/py_401.json" "$GO_401" "$PY_401" "$GO_401_CT" "$PY_401_CT" \
  && pass "error shape: both 401s are the same RFC 9457 problem (status=$GO_401/$PY_401)" \
  || fail "error shape: 401 bodies diverge (status=$GO_401/$PY_401)"

curl -s "${HEADERS[@]}" "http://localhost:${GO_HTTP_PORT}/call-other" > "$ROOT/.build/go_call_other.json" 2>&1
curl -s "${HEADERS[@]}" "http://localhost:${PY_HTTP_PORT}/call-other" > "$ROOT/.build/py_call_other.json" 2>&1
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
