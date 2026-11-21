#!/usr/bin/env bash
# =============================================================================
# stop-ira.sh — stop the IRA processes started by start-ira.sh.
#
# Stops: ira-api (uvicorn), the background worker, frontend (Next), and ollama
# ONLY if start-ira.sh started it (PID file present). Postgres and Redis are
# system services and are left running — stop them with your service manager if
# you really want to.
#
# Only this checkout's processes. A PID file is acted on only when the process
# it names is still this checkout's: running from inside this folder (ollama:
# still named ollama). A PID file left by a crash or a reboot can name another
# program by now, and that program is left alone. The sweep for a stray API
# below has the same rule.
#
# Usage: ./stop-ira.sh
# =============================================================================

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The physical path, which is what /proc and lsof report (/tmp is
# /private/tmp on macOS).
ROOT_REAL="$(cd "$ROOT" && pwd -P)"
RUN_DIR="$ROOT/.ira-run"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
# Regular expressions live in variables: bash 3.2, macOS's, reads a pattern
# written inline after =~ differently from bash 4 and 5.
NUMBER='^[0-9]+$'

# ── Whose process is it? ─────────────────────────────────────────────────────
# A process's working directory: /proc on Linux, lsof on macOS (which has no
# /proc). Empty when it cannot be read, for example another user's process.
CWD_FROM=""
if [[ -L /proc/$$/cwd ]]; then CWD_FROM=proc
elif command -v lsof >/dev/null 2>&1; then CWD_FROM=lsof
fi
# Tests only: read working directories with lsof on Linux too.
[[ "${_IRA_STOP_CWD_FROM:-}" == lsof ]] && CWD_FROM=lsof

cwd_of() {  # pid
    case "$CWD_FROM" in
        proc) readlink "/proc/$1/cwd" 2>/dev/null ;;
        lsof) lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1 ;;
    esac
}

in_checkout() {  # pid — started from inside this folder
    local cwd; cwd="$(cwd_of "$1")"
    [[ -n "$cwd" && ( "$cwd" == "$ROOT_REAL" || "$cwd" == "$ROOT_REAL"/* ) ]]
}

is_ours() {  # name pid
    if [[ "$1" == "ollama" ]]; then
        # start-ira.sh starts ollama from wherever it was run, so its folder
        # says nothing; its name does.
        ps -p "$2" -o comm= 2>/dev/null | grep -q 'ollama'
    elif [[ -z "$CWD_FROM" ]]; then
        return 0   # cannot tell; the PID file is all there is (said below)
    else
        in_checkout "$2"
    fi
}

stop_pidfile() {  # name pidfile
    local name="$1" pidfile="$2"
    if [[ ! -f "$pidfile" ]]; then
        printf "  ${YELLOW}[SKIP]${NC} %-10s not started by start-ira.sh\n" "$name"
        return
    fi
    local pid; pid="$(cat "$pidfile" 2>/dev/null)"
    if [[ ! "$pid" =~ $NUMBER ]] || ! kill -0 "$pid" 2>/dev/null; then
        printf "  ${YELLOW}[SKIP]${NC} %-10s not running\n" "$name"
    elif ! is_ours "$name" "$pid"; then
        printf "  ${YELLOW}[SKIP]${NC} %-10s pid %s is not this checkout's %s (stale PID file); left running\n" \
            "$name" "$pid" "$name"
    else
        # The whole process group: start-ira.sh runs each server as a group
        # leader, and `npm run start` -> sh -> next-server is three processes.
        # Killing only the recorded PID left next-server holding the port.
        # A PID file from an older start-ira.sh is not a group leader, so fall
        # back to the single PID.
        kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 -- "-$pid" 2>/dev/null || kill -0 "$pid" 2>/dev/null || break; sleep 1
        done
        kill -9 -- "-$pid" 2>/dev/null; kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
        local how=""
        [[ -z "$CWD_FROM" && "$name" != "ollama" ]] && how=" (not checked against this folder: no /proc, no lsof)"
        printf "  ${GREEN}[OK]${NC}   %-10s stopped (pid %s)%s\n" "$name" "$pid" "$how"
    fi
    rm -f "$pidfile"
}

# ── The API's port, as start-ira.sh chose it ─────────────────────────────────
# start-ira.sh loads .env over the environment, then defaults to 8000. Only
# IRA_API_PORT is read here, by start-ira.sh's rules: a quoted value is taken
# verbatim, and in an unquoted one ` #` starts a comment. The last line wins.
env_file_value() {  # KEY — prints .env's value; status 1 when .env does not set KEY
    local key="$1" line val found=1
    local assignment='^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*(.*)$'
    [[ -f "$ROOT/.env" ]] || return 1
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" =~ $assignment ]] && [[ "${BASH_REMATCH[1]}" == "$key" ]]; then
            val="${BASH_REMATCH[2]}"
            if [[ "$val" == \"* ]]; then
                val="${val#\"}"; val="${val%%\"*}"
            elif [[ "$val" == \'* ]]; then
                val="${val#\'}"; val="${val%%\'*}"
            elif [[ "$val" == \#* ]]; then
                val=""
            else
                val="${val%%[[:space:]]#*}"
                val="${val%"${val##*[![:space:]]}"}"
            fi
            found=0
        fi
    done < "$ROOT/.env"
    [[ $found -eq 0 ]] && printf '%s\n' "$val"
    return $found
}

API_PORT="$(env_file_value IRA_API_PORT)" || API_PORT="${IRA_API_PORT:-}"
[[ -n "$API_PORT" ]] || API_PORT=8000

echo "Stopping IRA..."
stop_pidfile "frontend" "$RUN_DIR/frontend.pid"
stop_pidfile "worker"   "$RUN_DIR/worker.pid"
stop_pidfile "ira-api"  "$RUN_DIR/ira-api.pid"
stop_pidfile "ollama"   "$RUN_DIR/ollama.pid"

# ── A stray API ──────────────────────────────────────────────────────────────
# An API whose PID file is missing or stale: one started by hand, or by a
# start-ira.sh from before the PID file named the server. Stopped only when
# its command line runs `uvicorn main:app` on exactly this port and it runs
# from inside this folder. This used to be `pkill -f "uvicorn main:app.*--port
# 8000"`, which matched any user's IRA from any folder, and port 80000 too.
if [[ ! "$API_PORT" =~ $NUMBER ]]; then
    printf "  ${YELLOW}[SKIP]${NC} %-10s IRA_API_PORT is not a number (%s); no sweep for a stray API\n" \
        "ira-api" "$API_PORT"
elif [[ -z "$CWD_FROM" ]]; then
    printf "  ${YELLOW}[SKIP]${NC} %-10s cannot see which folder a process runs from (no /proc, no lsof); no sweep for a stray API\n" \
        "ira-api"
else
    # `python -m uvicorn main:app` or `.venv/bin/uvicorn main:app`, and the
    # port exactly: `--port 8000` or `--port=8000`, not `--port 80000`.
    runs_api='(^|[[:space:]/])uvicorn[[:space:]]+main:app([[:space:]]|$)'
    on_port="(^|[[:space:]])--port([[:space:]]+|=)${API_PORT}([[:space:]]|\$)"
    swept=""
    while read -r pid args; do
        [[ "$pid" =~ $NUMBER && "$pid" != "$$" ]] || continue
        [[ "$args" =~ $runs_api && "$args" =~ $on_port ]] || continue
        in_checkout "$pid" || continue
        kill "$pid" 2>/dev/null && swept="$swept $pid"
    done < <(ps -A -o pid= -o args= 2>/dev/null)
    if [[ -n "$swept" ]]; then
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            alive=""
            for pid in $swept; do kill -0 "$pid" 2>/dev/null && alive="$alive $pid"; done
            [[ -z "$alive" ]] && break; sleep 1
        done
        for pid in $swept; do kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null; done
        printf "  ${GREEN}[OK]${NC}   %-10s swept stray uvicorn on port %s (pid%s)\n" "ira-api" "$API_PORT" "$swept"
    fi
fi

echo -e "${GREEN}Done. Postgres/Redis left running (system services).${NC}"
