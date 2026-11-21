#!/usr/bin/env bash
# =============================================================================
# start-ira.sh — one-command IRA native stack for Linux / macOS / WSL2.
# Bash counterpart of start-ira.ps1 (Windows). Replaces the retired
# docker-compose path (scripts/dev-start.sh) — everything runs native.
#
# Order: checks (python/node/.env) -> Postgres -> Redis -> Ollama (pull + warm)
#        -> web-research backends (optional) -> IRA backend (uvicorn)
#        -> IRA frontend (Next).
# Each service is started only if it isn't already up, then probed until
# healthy. Optional services (SearXNG / Crawl4AI) are WARN, not fatal.
#
# Usage:
#   ./start-ira.sh                 # bring everything up
#   ./start-ira.sh --skip-frontend # backend only
#   ./stop-ira.sh                  # stop the processes this script started
# =============================================================================

set -uo pipefail

if [ -z "${BASH_VERSION:-}" ]; then
    echo "ERROR: run with bash: bash start-ira.sh" >&2
    exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IRA_DIR="$ROOT/ira"
FRONTEND_DIR="$ROOT/frontend"
RUN_DIR="$ROOT/.ira-run"
mkdir -p "$RUN_DIR"

SKIP_FRONTEND=false
[[ "${1:-}" == "--skip-frontend" ]] && SKIP_FRONTEND=true

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
FAILED=""

ok()   { printf "  ${GREEN}[OK]${NC}   %-12s %s\n" "$1" "${2:-}"; }
warn() { printf "  ${YELLOW}[WARN]${NC} %-12s %s\n" "$1" "${2:-}"; }
fail() { printf "  ${RED}[FAIL]${NC} %-12s %s\n" "$1" "${2:-}"; FAILED="${FAILED} $1"; }

# ── helpers ───────────────────────────────────────────────────────────────────
tcp_up() {  # host port
    (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null && { exec 3>&- 3<&-; return 0; } || return 1
}

http_up() { curl -sf --max-time 5 "$1" > /dev/null 2>&1; }

wait_until() {  # "command string" timeout_sec
    local deadline=$(( $(date +%s) + $2 ))
    while (( $(date +%s) < deadline )); do
        eval "$1" && return 0
        sleep 1
    done
    return 1
}

env_or() {  # VAR default
    local v="${!1:-}"
    [[ -n "$v" ]] && echo "$v" || echo "$2"
}

# ── .env ──────────────────────────────────────────────────────────────────────
echo -e "${CYAN}Starting IRA native stack...${NC}"

if [[ -f "$ROOT/.env" ]]; then
    # Parse KEY=VALUE lines instead of sourcing: values with spaces or shell
    # metacharacters (e.g. OWNER_NAME=Praveen Kumar) must not be executed.
    # Mirrors the .env loader in start-ira.ps1.
    #
    # Same rules as the backend's own reader (python-dotenv): a quoted value is
    # taken verbatim, and in an unquoted one ` #` starts a comment. .env.example
    # documents most settings with inline comments, and exporting them as part
    # of the value made the backend refuse to start ("false   # true ..." is
    # not a boolean) — and these exports override the .env file it reads.
    while IFS= read -r _line || [[ -n "$_line" ]]; do
        if [[ "$_line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
            _key="${BASH_REMATCH[1]}"; _val="${BASH_REMATCH[2]}"
            if [[ "$_val" == \"* ]]; then
                _val="${_val#\"}"; _val="${_val%%\"*}"
            elif [[ "$_val" == \'* ]]; then
                _val="${_val#\'}"; _val="${_val%%\'*}"
            elif [[ "$_val" == \#* ]]; then
                _val=""
            else
                _val="${_val%%[[:space:]]#*}"
                _val="${_val%"${_val##*[![:space:]]}"}"
            fi
            export "${_key}=${_val}"
        fi
    done < "$ROOT/.env"
    ok ".env" "loaded"
else
    fail ".env" "missing — run setup first; it writes .env with new, random secrets:"
    echo "         bash scripts/setup-native.sh"
    exit 1
fi

# ONNX Runtime (voice transcription, the wake word) reports usage to Microsoft
# as soon as it is imported unless this is set before it loads. Exported here,
# after .env, so the backend and the worker inherit it; a value set in .env is
# kept, a blank one is not (ira/core/runtime/vendor_telemetry.py).
export ORT_DISABLE_TELEMETRY="${ORT_DISABLE_TELEMETRY:-1}"

# ── prerequisite binaries ─────────────────────────────────────────────────────
# Homebrew's PostgreSQL formulae are keg-only, so psql and pg_dump are not on
# PATH after `brew install postgresql@17`. The backend shells out to pg_dump for
# backups, and it inherits this PATH.
if ! command -v pg_dump >/dev/null 2>&1 && command -v brew >/dev/null 2>&1; then
    for _pg in $(brew list --formula 2>/dev/null | grep -E '^postgresql@[0-9]+$' | sort -r); do
        _pg_bin="$(brew --prefix "$_pg" 2>/dev/null)/bin"
        if [[ -x "$_pg_bin/pg_dump" ]]; then export PATH="$_pg_bin:$PATH"; break; fi
    done
fi
# The backend runs on the project's .venv (created by scripts/setup-native.sh),
# so the double-click launcher works without an activated shell. Otherwise the
# first 3.11/3.12 on PATH: 3.13+ cannot install the pinned dependencies.
PY=""
for _cand in "$ROOT/.venv/bin/python" python3.12 python3.11 python3; do
    if command -v "$_cand" >/dev/null 2>&1 \
        && "$_cand" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 12) else 1)' 2>/dev/null; then
        PY="$(command -v "$_cand")"; break
    fi
done
if [[ -z "$PY" ]]; then
    fail "python" "Python 3.11 or 3.12 not found — run: bash scripts/setup-native.sh"
elif ! "$PY" -c 'import uvicorn, fastapi' >/dev/null 2>&1; then
    fail "python" "$PY has no backend dependencies — run: bash scripts/setup-native.sh"
else
    ok "python" "$("$PY" --version 2>&1) ($PY)"
fi
command -v node >/dev/null 2>&1 && ok "node" "$(node --version 2>&1)" \
    || { $SKIP_FRONTEND || fail "node" "node not found — install Node 20+ (or use --skip-frontend)"; }
command -v curl >/dev/null 2>&1 || fail "curl" "curl not found — install curl"

[[ -n "$FAILED" ]] && { echo -e "${RED}Prerequisites missing:${FAILED}${NC}"; exit 1; }

# ── 1. Postgres ───────────────────────────────────────────────────────────────
PG_HOST=$(env_or POSTGRES_HOST localhost)
PG_PORT=$(env_or POSTGRES_PORT 5432)
if ! tcp_up "$PG_HOST" "$PG_PORT"; then
    # try to start a local service (best effort, no sudo prompt)
    command -v pg_ctlcluster >/dev/null 2>&1 && sudo -n service postgresql start >/dev/null 2>&1 || true
    # Homebrew's formulae are versioned (postgresql@17); the bare name is gone.
    if command -v brew >/dev/null 2>&1; then
        _pg_formula="$(brew list --formula 2>/dev/null | grep -E '^postgresql(@[0-9]+)?$' | sort | tail -1)"
        [[ -n "$_pg_formula" ]] && brew services start "$_pg_formula" >/dev/null 2>&1 || true
    fi
fi
if wait_until "tcp_up $PG_HOST $PG_PORT" 15; then ok "postgres" "port $PG_PORT"
else fail "postgres" "not reachable on $PG_HOST:$PG_PORT — start it (e.g. sudo service postgresql start)"; fi

# ── 2. Redis ──────────────────────────────────────────────────────────────────
REDIS_HOST_V=$(env_or REDIS_HOST localhost)
REDIS_PORT_V=$(env_or REDIS_PORT 6379)
if ! tcp_up "$REDIS_HOST_V" "$REDIS_PORT_V"; then
    # Persistence as resilience only (migration 096): per-token revocations and
    # rate limits stay warm across a restart. Nothing reads Redis to decide
    # whether a token is current; that is PostgreSQL. The files live in a
    # private directory (0700, files 0600), not the launcher's working
    # directory, where Redis wrote them before.
    if command -v redis-server >/dev/null 2>&1; then
        REDIS_DIR="$RUN_DIR/redis"
        mkdir -p "$REDIS_DIR" && chmod 700 "$REDIS_DIR"
        # This machine only, like the API and the frontend. With no --bind, a
        # redis-server started without a configuration file listens on every
        # interface: 0.0.0.0:6379 on a clean Ubuntu 24.04 install (#190), where
        # only protected mode, which applies only while no password is set,
        # kept other machines out. -::1 adds the IPv6 loopback where the
        # machine has one; the "-" makes it optional, which only Redis 7 and
        # later understand, so an older Redis gets 127.0.0.1 alone. --port:
        # otherwise Redis takes 6379 whatever REDIS_PORT says, and the wait
        # below is on REDIS_PORT.
        REDIS_BIND=(127.0.0.1)
        _redis_major="$(redis-server --version 2>/dev/null | sed -n 's/.* v=\([0-9][0-9]*\)\..*/\1/p')"
        if [[ "${_redis_major:-0}" -ge 7 ]]; then REDIS_BIND+=(-::1); fi
        ( umask 077
          redis-server --daemonize yes --bind "${REDIS_BIND[@]}" --protected-mode yes \
              --port "$REDIS_PORT_V" --appendonly yes --appendfsync everysec \
              --dir "$REDIS_DIR" >/dev/null 2>&1 ) || true
    fi
fi
if wait_until "tcp_up $REDIS_HOST_V $REDIS_PORT_V" 15; then ok "redis" "port $REDIS_PORT_V"
else fail "redis" "not reachable on $REDIS_HOST_V:$REDIS_PORT_V — start it (e.g. redis-server --daemonize yes)"; fi

# ── 3. Ollama (serve, pull + warm the fast/deep tiers) ────────────────────────
OLLAMA_BASE="${OLLAMA_BASE_URL:-http://localhost:11434/v1}"; OLLAMA_BASE="${OLLAMA_BASE%/v1}"
KEEP_ALIVE=$(env_or OLLAMA_KEEP_ALIVE 30m)
FAST_MODEL=$(env_or OLLAMA_MODEL_FAST qwen3:8b)
DEEP_MODEL=$(env_or OLLAMA_MODEL_DEEP qwen3:14b)
# Each model once: setup gives both tiers the same model.
WARM_MODELS=("$FAST_MODEL")
[[ "$DEEP_MODEL" != "$FAST_MODEL" ]] && WARM_MODELS+=("$DEEP_MODEL")
if ! http_up "$OLLAMA_BASE/api/tags"; then
    if command -v ollama >/dev/null 2>&1; then
        OLLAMA_KEEP_ALIVE="$KEEP_ALIVE" nohup ollama serve > "$RUN_DIR/ollama.log" 2>&1 &
        echo $! > "$RUN_DIR/ollama.pid"
    fi
fi
if wait_until "http_up $OLLAMA_BASE/api/tags" 30; then
    for m in "${WARM_MODELS[@]}"; do
        if ! curl -sf "$OLLAMA_BASE/api/tags" | grep -q "\"$m\""; then
            echo "         ollama pull $m ... (first time only, this can take a while)"
            ollama pull "$m" 2>/dev/null || warn "ollama" "could not pull $m — pull it manually"
        fi
    done
    # Warm the models so the first chat isn't cold. After a reboot, a first
    # load can take minutes without a GPU: more than 120 s for qwen3:4b
    # (2.5 GB) on 4 vCPUs. Ollama aborts a load when its client hangs up, so
    # the old `curl --max-time 120 ... || true` cancelled that load and said
    # nothing. The load now runs in the background, logged, with a cap far
    # above a cold load. The start waits for it no longer than it could
    # before (two calls of up to 120 s), then goes on and says so; it stops
    # waiting, not loading. The job ignores SIGHUP and SIGINT, so closing the
    # terminal or pressing Ctrl-C during the wait does not cancel the load
    # either. (A background job ignores SIGINT itself, but the commands it
    # runs, curl here, do not, unless it says so.)
    # The request has no prompt: Ollama then loads the model and answers
    # `"done":true,"done_reason":"load"` without writing anything (its API
    # docs, "Load a model"). With a prompt the model also wrote a reply, which
    # held the CPU after the load, even when the model was already loaded.
    WARM_MAX_TIME=1800
    WARM_WAIT=240
    WARM_LOG="$RUN_DIR/ollama-warm.log"
    warm_models() {  # model... — one line each, "warm: m" or "not warm: m (...)", then "done"
        local m code why
        for m in "$@"; do
            echo "loading: $m"
            rm -f "$WARM_LOG.reply" "$WARM_LOG.err"
            code="$(curl -sS --max-time "$WARM_MAX_TIME" -o "$WARM_LOG.reply" -w '%{http_code}' \
                -X POST "$OLLAMA_BASE/api/generate" -H "Content-Type: application/json" \
                -d "{\"model\":\"$m\",\"stream\":false,\"keep_alive\":\"$KEEP_ALIVE\"}" \
                2>"$WARM_LOG.err")"
            if [[ "$code" == "200" ]]; then
                echo "warm: $m"
            else
                why="$(head -c 300 "$WARM_LOG.reply" 2>/dev/null | tr '\n' ' ')"
                [[ -n "$why" ]] || why="$(head -c 300 "$WARM_LOG.err" 2>/dev/null | tr '\n' ' ')"
                why="${why%"${why##*[![:space:]]}"}"
                echo "not warm: $m (HTTP ${code:-000})${why:+ $why}"
            fi
        done
        rm -f "$WARM_LOG.reply" "$WARM_LOG.err"
        echo "done"
    }
    ( trap '' HUP INT; warm_models "${WARM_MODELS[@]}" ) > "$WARM_LOG" 2>&1 &
    WARM_PID=$!
    wait_until "grep -qx done '$WARM_LOG' 2>/dev/null || ! kill -0 $WARM_PID 2>/dev/null" "$WARM_WAIT"
    ok "ollama" "$OLLAMA_BASE ($FAST_MODEL + $DEEP_MODEL, keep_alive=$KEEP_ALIVE)"
    for m in "${WARM_MODELS[@]}"; do
        grep -qxF "warm: $m" "$WARM_LOG" 2>/dev/null && continue
        _why="$(grep -F "not warm: $m (" "$WARM_LOG" 2>/dev/null | head -n 1)"
        if [[ -n "$_why" ]]; then
            warn "ollama" "could not warm ${_why#not warm: } — see $WARM_LOG"
        elif kill -0 "$WARM_PID" 2>/dev/null; then
            warn "ollama" "$m still loading after ${WARM_WAIT}s; it goes on in the background, and the first chat waits for it — see $WARM_LOG"
        else
            warn "ollama" "$m was not warmed — see $WARM_LOG"
        fi
    done
    # A model bigger than this machine's memory loads slowly or not at all.
    for m in "$FAST_MODEL" "$DEEP_MODEL"; do
        _fit="$("$PY" "$ROOT/scripts/detect_hardware.py" --check "$m" 2>/dev/null)" || warn "ollama" "$_fit"
    done
else
    fail "ollama" "no response at $OLLAMA_BASE — install from https://ollama.com and run: ollama serve"
fi

# ── 4. Web-research backends (optional — research fails soft) ─────────────────
SEARXNG="${SEARXNG_URL:-http://localhost:8888}"; SEARXNG="${SEARXNG%/}"
http_up "$SEARXNG/" && ok "searxng" "$SEARXNG" \
    || warn "searxng" "down ($SEARXNG) — web search disabled, research fails soft"
CRAWL4AI="${CRAWL4AI_URL:-http://localhost:11235}"; CRAWL4AI="${CRAWL4AI%/}"
http_up "$CRAWL4AI/health" && ok "crawl4ai" "$CRAWL4AI" \
    || warn "crawl4ai" "down ($CRAWL4AI) — web reader disabled, research fails soft"

# ── 5. IRA backend (uvicorn) ──────────────────────────────────────────────────
API_PORT=$(env_or IRA_API_PORT 8000)
if ! http_up "http://127.0.0.1:$API_PORT/health"; then
    # `set -m` gives the job its own process group and `{ …; }` keeps $! on the
    # server itself. Written as `( cd … && nohup … & echo $! )`, the `&` took
    # the whole `cd && nohup` list, so $! was a throwaway subshell: stop-ira.sh
    # killed that, and the server kept running — and a "restart" after an
    # update went on serving the old code. stop-ira.sh stops the whole group.
    ( cd "$IRA_DIR" && set -m && { nohup "$PY" -m uvicorn main:app --host 127.0.0.1 --port "$API_PORT" \
        > "$RUN_DIR/ira-api.log" 2>&1 & echo $! > "$RUN_DIR/ira-api.pid"; } )
fi
if wait_until "http_up http://127.0.0.1:$API_PORT/health" 90; then ok "ira-api" "http://127.0.0.1:$API_PORT"
else fail "ira-api" "no /health on $API_PORT — see $RUN_DIR/ira-api.log"; fi

# ── 5b. Background worker ─────────────────────────────────────────────────────
# Reminders, retention purges and any other job IRA_WORKER_JOBS names. This
# script used to start no worker, so a reminder never fired on Linux or macOS.
# Which jobs run is ira/worker/jobs.py: unset means a default that calls no
# model; IRA_WORKER_JOBS=none starts no worker at all.
WORKER_JOBS="$(env_or IRA_WORKER_JOBS "")"
if [[ "$(echo "$WORKER_JOBS" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')" == "none" ]]; then
    warn "worker" "not started (IRA_WORKER_JOBS=none): reminders will not fire"
else
    worker_up() { [[ -f "$RUN_DIR/worker.pid" ]] && kill -0 "$(cat "$RUN_DIR/worker.pid")" 2>/dev/null; }
    # A PID file left by a crash or a reboot can name another process by now:
    # count it as the worker only if that process is running worker.main.
    worker_is_ours() {
        worker_up && { ! command -v ps >/dev/null 2>&1 \
            || ps -p "$(cat "$RUN_DIR/worker.pid")" -o args= 2>/dev/null | grep -q 'worker[.]main'; }
    }
    if ! worker_is_ours; then
        ( cd "$IRA_DIR" && set -m && { nohup "$PY" -m worker.main > "$RUN_DIR/worker.log" 2>&1 & echo $! > "$RUN_DIR/worker.pid"; } )
    fi
    # Up once it has logged its job list; down if it exits first.
    if wait_until "grep -q 'Worker jobs on:' '$RUN_DIR/worker.log' 2>/dev/null || ! worker_up" 30 && worker_up; then
        ok "worker" "$(grep -o 'Worker jobs on: [^.]*' "$RUN_DIR/worker.log" | tail -1 | sed 's/Worker jobs on: /jobs: /')"
    else
        warn "worker" "not running: background jobs are off. See $RUN_DIR/worker.log"
    fi
fi

# ── 6. IRA frontend (Next) ────────────────────────────────────────────────────
if $SKIP_FRONTEND; then
    warn "frontend" "skipped (--skip-frontend)"
else
    FE_PORT=$(env_or FRONTEND_PORT 3000)
    # Loopback only, like the API. Plain `next start` listens on every
    # interface, and its /api and /auth rewrites would then hand the API to
    # anyone on the network over plain HTTP. Phones and tablets reach IRA
    # through `tailscale serve` (HTTPS), which proxies to 127.0.0.1 — see
    # TAILSCALE_SETUP.md.
    if ! tcp_up 127.0.0.1 "$FE_PORT"; then
        if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
            echo "         npm install ... (first time only)"
            ( cd "$FRONTEND_DIR" && npm install --no-audit --no-fund > "$RUN_DIR/npm-install.log" 2>&1 ) \
                || warn "frontend" "npm install failed — see $RUN_DIR/npm-install.log"
        fi
        # Server-side rendering calls the API directly; natively that is here.
        export IRA_API_INTERNAL_URL="${IRA_API_INTERNAL_URL:-http://127.0.0.1:$API_PORT}"
        if [[ -f "$FRONTEND_DIR/.next/BUILD_ID" ]]; then
            ( cd "$FRONTEND_DIR" && set -m && { nohup npm run start -- -H 127.0.0.1 -p "$FE_PORT" > "$RUN_DIR/frontend.log" 2>&1 & echo $! > "$RUN_DIR/frontend.pid"; } )
        else
            warn "frontend" "no production build (.next) — starting dev server; run 'npm run build' for production"
            ( cd "$FRONTEND_DIR" && set -m && { nohup npm run dev -- -H 127.0.0.1 -p "$FE_PORT" > "$RUN_DIR/frontend.log" 2>&1 & echo $! > "$RUN_DIR/frontend.pid"; } )
        fi
    fi
    if wait_until "tcp_up 127.0.0.1 $FE_PORT" 90; then ok "frontend" "http://localhost:$FE_PORT (this machine only)"
    else fail "frontend" "not reachable on $FE_PORT — see $RUN_DIR/frontend.log"; fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
if [[ -n "$FAILED" ]]; then
    echo -e "${RED}STACK NOT FULLY UP — failed:${FAILED}${NC}"
    exit 1
else
    UI_URL="http://localhost:$(env_or FRONTEND_PORT 3000)"
    echo -e "${GREEN}ALL UP. IRA is ready, boss.${NC}"
    echo -e "  UI:  ${CYAN}$UI_URL${NC}"
    echo -e "  API: ${CYAN}http://127.0.0.1:$(env_or IRA_API_PORT 8000)/docs${NC}"
    echo -e "  Stop with: ${CYAN}./stop-ira.sh${NC}"
    # Open the dashboard in the default browser (best effort, never fatal).
    if ! $SKIP_FRONTEND; then
        if command -v xdg-open >/dev/null 2>&1; then xdg-open "$UI_URL" >/dev/null 2>&1 || true
        elif command -v open >/dev/null 2>&1; then open "$UI_URL" >/dev/null 2>&1 || true
        fi
    fi
fi
