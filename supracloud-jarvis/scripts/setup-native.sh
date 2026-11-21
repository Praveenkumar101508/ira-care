#!/usr/bin/env bash
# =============================================================================
# setup-native.sh — one-time IRA setup for Linux and macOS, no Docker, no GPU.
# The bash counterpart of scripts/setup-windows.ps1. Idempotent: safe to re-run.
#
#   1. Checks prerequisites (Python 3.11/3.12, Node 20+, PostgreSQL + pgvector,
#      Redis, Ollama) and prints the install command for this OS
#   2. Detects memory/GPU and picks a chat model this machine can run
#   3. Creates the database and its separated roles (the provisioning script
#      CI uses) and enables pgvector
#   4. Generates .env from .env.example with random secrets (never printed)
#   5. Creates .venv, installs the backend, pre-caches the embedding model
#   6. Applies the database migrations as ira_migrator (also on every re-run,
#      which is how an update gets its new migrations)
#   7. Pulls the chat model; installs and builds the frontend
#
# Then start everything with ./start-ira.sh.
#
# Usage (from supracloud-jarvis/):
#   bash scripts/setup-native.sh [--model qwen3:4b] [--skip-deps] [--skip-frontend]
#
# Postgres superuser access, in order of preference:
#   PG_SUPERUSER / PGPASSWORD env vars if you set them;
#   macOS (Homebrew): your own login role, which Homebrew makes a superuser;
#   Linux: `sudo -u postgres` (or running as root).
# Written for bash 3.2 — the version macOS ships.
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="$ROOT/scripts"
ENV_FILE="$ROOT/.env"
ENV_EXAMPLE="$ROOT/.env.example"
VENV="$ROOT/.venv"
FRONTEND_DIR="$ROOT/frontend"

MODEL=""
SKIP_DEPS=false
SKIP_FRONTEND=false
while [ $# -gt 0 ]; do
    case "$1" in
        --model) [ $# -ge 2 ] || { echo "--model needs a value" >&2; exit 2; }; MODEL="$2"; shift 2 ;;
        --skip-deps) SKIP_DEPS=true; shift ;;
        --skip-frontend) SKIP_FRONTEND=true; shift ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
step() { printf "\n${CYAN}=== %s ===${NC}\n" "$1"; }
ok()   { printf "  ${GREEN}[OK]${NC}   %s\n" "$1"; }
warn() { printf "  ${YELLOW}[WARN]${NC} %s\n" "$1"; }
die()  { printf "  ${RED}[FAIL]${NC} %s\n" "$1"; exit 1; }

OS="$(uname -s)"
case "$OS" in
    Darwin|Linux) ;;
    *) die "unsupported OS '$OS' — use scripts/setup-windows.ps1 on Windows" ;;
esac
hint() {  # what to install, per OS: hint <brew pkg> <apt pkg>
    if [ "$OS" = Darwin ]; then echo "brew install $1"; else echo "sudo apt-get install -y $2"; fi
}

# ── 1. Prerequisites ─────────────────────────────────────────────────────────
step "1. Prerequisites ($OS $(uname -m))"

# A Mac must be Apple Silicon, running natively, on macOS 14 or later. PyTorch
# publishes no Intel-Mac build of the release IRA pins, and av and onnxruntime
# publish Apple Silicon builds for macOS 14 and later only. On anything else pip
# fails near the end of step 5 with "No matching distribution found", which does
# not say the Mac is the reason.
if [ "$OS" = Darwin ]; then
    if [ "$(uname -m)" != arm64 ]; then
        if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = 1 ]; then
            die "this Terminal is running under Rosetta (Intel emulation) — quit it, open Terminal without 'Open using Rosetta', and use the Homebrew in /opt/homebrew"
        fi
        die "this Mac has an Intel processor ($(uname -m)) — IRA needs Apple Silicon (M1 or later): PyTorch publishes no Intel-Mac build of the release IRA pins"
    fi
    MACOS_VERSION="$(sw_vers -productVersion 2>/dev/null)"
    [ "${MACOS_VERSION%%.*}" -ge 14 ] 2>/dev/null \
        || die "macOS ${MACOS_VERSION:-unknown} is too old — IRA needs macOS 14 (Sonoma) or later: av and onnxruntime publish Apple Silicon builds for macOS 14+ only"
    ok "macOS $MACOS_VERSION on Apple Silicon"
fi

# Python 3.11 or 3.12. Newer interpreters fail partway through the install:
# numpy 1.26.4 has no 3.13 wheel and psycopg-binary 3.2.3 has none for 3.14,
# and Homebrew's default python3 is newer than both.
PY=""
for cand in "$VENV/bin/python" python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1 \
        && "$cand" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 12) else 1)' 2>/dev/null; then
        PY="$(command -v "$cand")"; break
    fi
done
[ -n "$PY" ] || die "Python 3.11 or 3.12 not found — $(hint python@3.12 python3.12-venv) (Python 3.13+ cannot install IRA's pinned dependencies)"
# An Intel Python on an Apple Silicon Mac (Intel Homebrew in /usr/local, run
# under Rosetta) asks pip for Intel-Mac wheels, which do not exist for PyTorch.
if [ "$OS" = Darwin ] && [ "$("$PY" -c 'import platform; print(platform.machine())' 2>/dev/null)" != arm64 ]; then
    die "$PY is an Intel (x86_64) Python — install the Apple Silicon one: /opt/homebrew/bin/brew install python@3.12"
fi
ok "python $("$PY" --version 2>&1 | cut -d' ' -f2) ($PY)"

if command -v node >/dev/null 2>&1; then
    NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
    if [ "$NODE_MAJOR" -ge 20 ] 2>/dev/null; then ok "node $(node --version)"
    else $SKIP_FRONTEND || die "node $(node --version) is too old — need 20+: $(hint node@22 nodejs)"; fi
else
    $SKIP_FRONTEND || die "node not found — $(hint node@22 nodejs) (or pass --skip-frontend)"
fi

# Homebrew's PostgreSQL formulae are keg-only: psql is not on PATH after
# `brew install postgresql@17`. Find the newest installed one.
if ! command -v psql >/dev/null 2>&1 && command -v brew >/dev/null 2>&1; then
    for _pg in $(brew list --formula 2>/dev/null | grep -E '^postgresql@[0-9]+$' | sort -r); do
        _pg_bin="$(brew --prefix "$_pg" 2>/dev/null)/bin"
        if [ -x "$_pg_bin/psql" ]; then PATH="$_pg_bin:$PATH"; export PATH; break; fi
    done
fi
command -v psql >/dev/null 2>&1 && ok "psql $(psql --version | awk '{print $3}')" \
    || die "PostgreSQL client not found — $(hint 'postgresql@17 pgvector' 'postgresql postgresql-16-pgvector')"
command -v redis-server >/dev/null 2>&1 || command -v redis-cli >/dev/null 2>&1 \
    && ok "redis" || warn "redis not found — $(hint redis redis-server) (IRA needs Redis on localhost:6379)"
command -v ollama >/dev/null 2>&1 && ok "ollama $(ollama --version 2>/dev/null | awk '{print $NF}' | tail -1)" \
    || warn "ollama not found — $( [ "$OS" = Darwin ] && echo 'brew install ollama' || echo 'curl -fsSL https://ollama.com/install.sh | sh')"

# ── 2. Hardware → model ──────────────────────────────────────────────────────
step "2. Hardware"
"$PY" "$SCRIPTS/detect_hardware.py" || true
if [ -z "$MODEL" ]; then
    MODEL="$("$PY" "$SCRIPTS/detect_hardware.py" --model 2>/dev/null || echo qwen3:4b)"
fi
ACCEL="$("$PY" "$SCRIPTS/detect_hardware.py" --json 2>/dev/null \
    | "$PY" -c 'import json, sys; print(json.load(sys.stdin)["accelerator"])' 2>/dev/null || echo cpu)"
ok "chat model: $MODEL  (change later with OLLAMA_MODEL_* and IRA_LOCAL_*_MODEL in .env)"

# ── 3. PostgreSQL: running, database, roles, pgvector ────────────────────────
step "3. PostgreSQL (database, separated roles, pgvector)"

env_value() {  # env_value KEY — read KEY from an existing .env without sourcing it
    # `tr -d '\r'`: a .env written on Windows (or restored from a Windows
    # backup) ends its lines with CR LF; the CR would stay in the value.
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -1 | tr -d '\r' | sed 's/[[:space:]]*#.*$//; s/^"//; s/"$//'
}
PG_HOST="$(env_value POSTGRES_HOST)"; PG_HOST="${PG_HOST:-localhost}"
PG_PORT="$(env_value POSTGRES_PORT)"; PG_PORT="${PG_PORT:-5432}"

pg_up() { (exec 3<>"/dev/tcp/127.0.0.1/$PG_PORT") 2>/dev/null && { exec 3>&- 3<&-; return 0; }; return 1; }
if ! pg_up; then
    if [ "$OS" = Darwin ] && command -v brew >/dev/null 2>&1; then
        # Homebrew's formulae are versioned (postgresql@17); the bare name is gone.
        PG_FORMULA="$(brew list --formula 2>/dev/null | grep -E '^postgresql(@[0-9]+)?$' | sort | tail -1)"
        [ -n "$PG_FORMULA" ] && brew services start "$PG_FORMULA" >/dev/null 2>&1
    elif [ "$OS" = Linux ]; then
        sudo -n service postgresql start >/dev/null 2>&1 || service postgresql start >/dev/null 2>&1 || true
    fi
    for _ in 1 2 3 4 5 6 7 8 9 10; do pg_up && break; sleep 1; done
fi
pg_up && ok "postgres listening on $PG_PORT" || die "PostgreSQL is not running on port $PG_PORT — start it, then re-run"

# How to run SQL as a superuser on this machine.
if [ -n "${PG_SUPERUSER:-}" ]; then
    psql_su() { psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_SUPERUSER" -v ON_ERROR_STOP=1 -q "$@"; }
elif [ "$OS" = Darwin ]; then
    psql_su() { psql -h "$PG_HOST" -p "$PG_PORT" -v ON_ERROR_STOP=1 -q "$@"; }
elif [ "$(id -u)" = 0 ]; then
    psql_su() { runuser -u postgres -- psql -p "$PG_PORT" -v ON_ERROR_STOP=1 -q "$@"; }
else
    psql_su() { sudo -u postgres psql -p "$PG_PORT" -v ON_ERROR_STOP=1 -q "$@"; }
fi
psql_su -d postgres -c 'SELECT 1' >/dev/null 2>&1 \
    || die "cannot connect to PostgreSQL as a superuser — set PG_SUPERUSER (and PGPASSWORD) and re-run"

DB_NAME="$(env_value POSTGRES_DB)"; DB_NAME="${DB_NAME:-jarvis_db}"

# Every value reaches psql on stdin as a variable — never on a command line,
# where `ps` would show it. psql reads a backslash inside a single-quoted
# argument as an escape, so the backslash is doubled as well as the quote. sed
# rather than ${//}: bash 3.2 and 4.3+ treat quotes and backslashes in a
# replacement differently. The value reaches sed on stdin.
psql_quote() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e "s/'/''/g"; }
sql_su() {  # sql_su <database> <var> <value> <sql using :'var'>
    _q="$(psql_quote "$3")"
    printf '%s\n' "\\set $2 '$_q'" "$4" | psql_su -d "$1" -f - >/dev/null
}
set_role_password() {  # set_role_password <role> <password>
    sql_su postgres pw "$2" "SELECT format('ALTER ROLE %I PASSWORD %L', '$1', :'pw')\\gexec"
}

[ -n "$(psql_su -d postgres -tAc "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")" ] || {
    PG_MAJOR="$(psql_su -d postgres -tAc 'SHOW server_version_num' | cut -c1-2)"
    die "pgvector is not installed for PostgreSQL $PG_MAJOR — $(hint pgvector "postgresql-$PG_MAJOR-pgvector") (Homebrew's pgvector is built for PostgreSQL 17 and 18 only)"
}
sql_su postgres db "$DB_NAME" \
    "SELECT format('CREATE DATABASE %I', :'db') WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db')\\gexec" \
    || die "could not create database $DB_NAME"

# The roles the schema is built around, from the same provisioning script CI
# runs (postgres/provisioning/001_database_roles.sql): ira_schema_owner owns
# every object, ira_migrator applies migrations, ira_app serves requests and
# owns nothing — so the running API cannot switch off row-level security. The
# script also creates the extensions, which needs a superuser. Piped on stdin
# so the postgres OS user never needs to read this checkout.
psql_su -d "$DB_NAME" -f - < "$ROOT/postgres/provisioning/001_database_roles.sql" >/dev/null \
    || die "could not provision the database roles (postgres/provisioning/001_database_roles.sql)"

# Re-running must not change the runtime password under an existing .env.
DB_PASSWORD="$(env_value POSTGRES_PASSWORD)"
[ -n "$DB_PASSWORD" ] || DB_PASSWORD="$("$PY" -c 'import secrets; print(secrets.token_hex(18))')"
set_role_password ira_app "$DB_PASSWORD" || die "could not set the ira_app password"
ok "database $DB_NAME ready: roles provisioned, pgvector $(psql_su -d "$DB_NAME" -tAc "SELECT extversion FROM pg_extension WHERE extname='vector'")"
if [ -f "$ENV_FILE" ] && [ "$(env_value POSTGRES_USER)" != "ira_app" ]; then
    warn ".env runs the API as '$(env_value POSTGRES_USER)'; IRA expects the runtime role ira_app — set POSTGRES_USER=ira_app in .env"
fi

# ── 4. .env ──────────────────────────────────────────────────────────────────
step "4. .env"
if [ -f "$ENV_FILE" ]; then
    warn ".env exists — left untouched (delete it and re-run to regenerate)"
else
    [ -f "$ENV_EXAMPLE" ] || die ".env.example not found"
    # Written by Python so the substitution is the same on GNU and BSD systems.
    # Secrets are generated here and only ever written to the file.
    IRA_ENV_FILE="$ENV_FILE" IRA_ENV_EXAMPLE="$ENV_EXAMPLE" IRA_DB_PASSWORD="$DB_PASSWORD" \
    IRA_MODEL="$MODEL" IRA_BACKUP_DIR="$HOME/.ira/backups" IRA_ACCEL="$ACCEL" "$PY" - <<'PYEOF' || die "could not write .env"
import os, re, secrets
text = open(os.environ["IRA_ENV_EXAMPLE"], encoding="utf-8").read()
model = os.environ["IRA_MODEL"]
values = {
    "IRA_SECRET_KEY": secrets.token_hex(32),
    "IRA_ADMIN_PASSWORD": secrets.token_urlsafe(12),
    "POSTGRES_USER": "ira_app",      # the runtime role: owns nothing, no DDL
    "POSTGRES_PASSWORD": os.environ["IRA_DB_PASSWORD"],
    "REDIS_PASSWORD": "",            # a local redis-server has no password by default
    "VLLM_API_KEY": "ollama",        # unused with LLM_BACKEND=ollama
    "LLM_BACKEND": "ollama",
    "DEV_MODE": "false",
    "OLLAMA_MODEL_FAST": model, "OLLAMA_MODEL_DEEP": model, "OLLAMA_MODEL_REASONING": model,
    # The model router (on by default) reads these; without them it asks for
    # the profile's qwen3:8b / qwen3:14b, which this machine may never pull.
    "IRA_LOCAL_FAST_MODEL": model, "IRA_LOCAL_MAIN_MODEL": model,
    "IRA_LOCAL_REASONING_MODEL": model, "IRA_FALLBACK_TINY_MODEL": model,
    # .env.example's /backups is the Docker volume, unwritable natively (and
    # read-only on macOS). Keep dumps in the user's home, outside the
    # repository, so a backup of personal data can never be committed.
    "BACKUP_DIR": os.environ["IRA_BACKUP_DIR"],
    # Without a GPU a memory-augmented answer can outlast the 120 s default.
    "OLLAMA_READ_TIMEOUT_S": "600" if os.environ["IRA_ACCEL"] == "cpu" else "120",
}
for key, val in values.items():
    line = f"{key}={val}"
    pattern = re.compile(rf"^{key}=.*$", re.M)
    text, n = pattern.subn(lambda _m: line, text)
    if not n:
        text = text.rstrip("\n") + "\n" + line + "\n"
for key in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD"):
    if not re.search(rf"^{key}=\S+", text, re.M):
        raise SystemExit(f"{key} is empty in the generated .env — refusing to write it")
fd = os.open(os.environ["IRA_ENV_FILE"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write(text)
PYEOF
    ok ".env written (mode 600) with generated secrets and $MODEL"
    warn "your login: username 'admin', password = IRA_ADMIN_PASSWORD in .env"
fi

# ── 5. Python backend ────────────────────────────────────────────────────────
if $SKIP_DEPS; then
    warn "skipping dependency install (--skip-deps)"
else
    step "5. Python backend (.venv)"
    [ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV" || die "could not create $VENV — $(hint python@3.12 python3.12-venv)"
    # The repository's .gitignore does not list .venv; keep it out of git the
    # way Python 3.13's venv module does by default.
    [ -f "$VENV/.gitignore" ] || printf '*\n' > "$VENV/.gitignore"
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
    "$VENV/bin/python" -m pip install --quiet -r "$ROOT/ira/requirements.txt" \
        || die "pip install failed — see the error above"
    ok "backend dependencies installed into .venv"
    # The embedder is used on the first chat; caching it now avoids a silent
    # 1.3 GB download at that moment. Fails soft: it downloads on first use.
    # At its pinned revision (ira/core/models/artefacts.py), the same bytes the
    # app loads; the name is passed as an argument, never pasted into code.
    EMB="$(env_value EMBEDDING_MODEL)"; EMB="${EMB:-BAAI/bge-large-en-v1.5}"
    "$VENV/bin/python" "$ROOT/ira/scripts/prefetch_models.py" --embedder "$EMB" >/dev/null 2>&1 \
        && ok "embedding model cached ($EMB)" || warn "embedding model not cached — it downloads on first use"
fi

# ── 6. Schema: migrations, applied as ira_migrator ───────────────────────────
# The API deliberately never runs DDL (ira/scripts/migrate.py); a fresh
# database without this step has no tables, and login refuses with 503. Re-run
# setup after updating IRA and the new migrations apply here.
step "6. Database schema"
if [ -x "$VENV/bin/python" ]; then
    # A single-use migrator password: set for this run, cleared afterwards, so
    # no stored credential anywhere can change the schema.
    mkdir -p "$ROOT/.ira-run"
    MIG_PW="$("$PY" -c 'import secrets; print(secrets.token_hex(24))')"
    set_role_password ira_migrator "$MIG_PW" || die "could not enable ira_migrator"
    ( cd "$ROOT/ira" && PGPASSWORD="$MIG_PW" "$VENV/bin/python" -m scripts.migrate apply \
        --dsn "postgresql://ira_migrator@127.0.0.1:$PG_PORT/$DB_NAME" ) > "$ROOT/.ira-run/migrate.log" 2>&1
    MIG_STATUS=$?
    printf '%s\n' "ALTER ROLE ira_migrator PASSWORD NULL;" | psql_su -d postgres -f - >/dev/null
    [ "$MIG_STATUS" -eq 0 ] || die "migrations failed — see .ira-run/migrate.log"
    ok "migrations applied ($(grep -c . "$ROOT/.ira-run/migrate.log") lines in .ira-run/migrate.log)"
else
    warn "no .venv yet — re-run without --skip-deps to apply the database migrations"
fi

# ── 7. Model + frontend ──────────────────────────────────────────────────────
step "7. Chat model and frontend"
if command -v ollama >/dev/null 2>&1 && curl -sf --max-time 5 http://localhost:11434/api/tags >/dev/null 2>&1; then
    ollama pull "$MODEL" >/dev/null 2>&1 && ok "pulled $MODEL" || warn "could not pull $MODEL — start-ira.sh will retry"
else
    warn "Ollama is not running — start-ira.sh starts it and pulls $MODEL"
fi
if $SKIP_FRONTEND || $SKIP_DEPS; then
    warn "skipping frontend install"
else
    # The API proxy rules are fixed at build time (frontend/next.config.js).
    API_PORT="$(env_value IRA_API_PORT)"; API_PORT="${API_PORT:-8000}"
    ( cd "$FRONTEND_DIR" && npm ci --no-audit --no-fund >/dev/null 2>&1 \
        && IRA_API_INTERNAL_URL="http://127.0.0.1:$API_PORT" npm run build >/dev/null 2>&1 ) \
        && ok "frontend installed and built" \
        || warn "frontend build failed — run 'npm ci && npm run build' in frontend/ to see why"
fi

step "Done"
echo "  Start IRA:   ./start-ira.sh        (stop with ./stop-ira.sh)"
echo "  Then open:   http://localhost:3000 and sign in as 'admin'"
echo "  Phones and tablets: see TAILSCALE_SETUP.md (HTTPS; IRA itself listens on localhost only)"
