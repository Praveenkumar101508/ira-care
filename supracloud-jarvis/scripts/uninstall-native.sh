#!/usr/bin/env bash
# =============================================================================
# uninstall-native.sh — remove a native IRA install (Linux and macOS), keeping
# a backup you can restore from. Written for bash 3.2 (macOS).
#
# Usage (from supracloud-jarvis/):
#   bash scripts/uninstall-native.sh              # dry run: shows what would go; changes nothing
#   bash scripts/uninstall-native.sh --yes        # back up, then remove
#
# Options (only with --yes do they change anything):
#   --remove-roles   also drop IRA's database roles (ira_app, ira_worker,
#                    ira_migrator, ira_schema_owner). A role that another
#                    database still uses is kept, and the script says so.
#   --remove-model   also remove the chat model setup pulled (OLLAMA_MODEL_DEEP
#                    in .env). Off by default: other programs may use it.
#
# What --yes removes, in this order:
#   1. stops IRA (stop-ira.sh)
#   2. takes a backup with scripts/ira-backup.sh and checks the archive holds
#      both the database dump and .env. If the backup fails, NOTHING is removed.
#   3. drops IRA's database (POSTGRES_DB in .env)
#   4. removes .venv, .ira-run, frontend/node_modules, frontend/.next and .env
#
# Never removed: the backups folder, this source folder, .env.backup-* files
# that an earlier restore kept, PostgreSQL, Redis, Ollama, Node, Python, and the
# shared pip, npm and Hugging Face caches.
#
# To undo:  bash scripts/setup-native.sh
#           bash scripts/ira-backup.sh restore <the archive printed below> --yes
#           ./start-ira.sh   and sign in with your old password.
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="$ROOT/scripts"
ENV_FILE="$ROOT/.env"
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { printf "  ${GREEN}[OK]${NC}   %s\n" "$1"; }
plan() { printf "  ${CYAN}[PLAN]${NC} %s\n" "$1"; }
warn() { printf "  ${YELLOW}[WARN]${NC} %s\n" "$1"; }
die()  { printf "  ${RED}[FAIL]${NC} %s\n" "$1"; exit 1; }

YES=false; REMOVE_ROLES=false; REMOVE_MODEL=false
while [ $# -gt 0 ]; do
    case "$1" in
        --yes) YES=true; shift ;;
        --remove-roles) REMOVE_ROLES=true; shift ;;
        --remove-model) REMOVE_MODEL=true; shift ;;
        -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
done

env_value() {  # read KEY from .env without sourcing it
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -1 | sed 's/[[:space:]]*#.*$//; s/^"//; s/"$//'
}

# Homebrew's PostgreSQL is keg-only: its tools are not on PATH by default.
if ! command -v psql >/dev/null 2>&1 && command -v brew >/dev/null 2>&1; then
    for _pg in $(brew list --formula 2>/dev/null | grep -E '^postgresql@[0-9]+$' | sort -r); do
        _pg_bin="$(brew --prefix "$_pg" 2>/dev/null)/bin"
        if [ -x "$_pg_bin/psql" ]; then PATH="$_pg_bin:$PATH"; export PATH; break; fi
    done
fi

PG_PORT="$(env_value POSTGRES_PORT)"; PG_PORT="${PG_PORT:-5432}"
DB_NAME="$(env_value POSTGRES_DB)"; DB_NAME="${DB_NAME:-jarvis_db}"
BACKUP_DIR="$(env_value BACKUP_DIR)"; BACKUP_DIR="${BACKUP_DIR:-$HOME/.ira/backups}"
MODEL="$(env_value OLLAMA_MODEL_DEEP)"
ROLES="ira_app ira_worker ira_migrator ira_schema_owner"

# The same superuser access setup-native.sh and ira-backup.sh use.
as_su() {  # as_su <pg command> [args...]
    local cmd="$1"; shift
    if [ -n "${PG_SUPERUSER:-}" ]; then "$cmd" -h localhost -p "$PG_PORT" -U "$PG_SUPERUSER" "$@"
    elif [ "$(uname -s)" = Darwin ]; then "$cmd" -h localhost -p "$PG_PORT" "$@"
    elif [ "$(id -u)" = 0 ]; then runuser -u postgres -- "$cmd" -p "$PG_PORT" "$@"
    else sudo -n -u postgres "$cmd" -p "$PG_PORT" "$@"; fi
}
db_exists() { [ "$(as_su psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'" 2>/dev/null)" = 1 ]; }
# Reachable at all. "Not found" and "could not ask" were one state, so on Linux
# without cached sudo (`sudo -n` fails) the database was never dropped and the
# run still ended "IRA is uninstalled", with .env, and its password, gone.
db_reachable() { [ "$(as_su psql -d postgres -tAc 'SELECT 1' 2>/dev/null)" = 1 ]; }

LOCAL_PATHS=".venv .ira-run frontend/node_modules frontend/.next .env"

# ── What is here ───────────────────────────────────────────────────────────────
printf "${CYAN}IRA uninstall — %s${NC}\n" "$($YES && echo 'removing' || echo 'dry run, nothing will change')"
if db_exists; then DB_STATE="present"
elif ! command -v psql >/dev/null 2>&1; then DB_STATE="unknown (psql not found)"
elif db_reachable; then DB_STATE="absent"
else DB_STATE="not reachable"; fi
plan "back up to $BACKUP_DIR (database + .env), and stop if that fails"
plan "drop database $DB_NAME on port $PG_PORT ($DB_STATE)"
if [ "$DB_STATE" = "not reachable" ]; then
    warn "PostgreSQL cannot be asked about $DB_NAME as its administrator, so the real run will stop before removing anything. On Linux run \`sudo -v\` first, or set PG_SUPERUSER."
fi
for p in $LOCAL_PATHS; do
    if [ -e "$ROOT/$p" ]; then plan "remove $p"; fi
done
$REMOVE_ROLES && plan "drop roles $ROLES (each kept if another database still uses it)"
if $REMOVE_MODEL; then
    if [ -n "$MODEL" ]; then plan "remove chat model $MODEL from Ollama"; else warn "no OLLAMA_MODEL_DEEP in .env; no model to remove"; fi
fi
for f in "$ROOT"/.env.backup-*; do [ -e "$f" ] && warn "keeping $(basename "$f") (a .env an earlier restore replaced)"; done
ok "kept: backups in $BACKUP_DIR, this folder, PostgreSQL, Redis, Ollama and shared caches"

if ! $YES; then
    echo ""
    echo "Dry run: nothing was changed. Run again with --yes to back up and remove."
    exit 0
fi

# Refuse while the database's state is unknowable: removing .env and reporting
# success would leave the data behind, with nothing left that opens it.
if [ "$DB_STATE" = "not reachable" ]; then
    die "cannot tell whether database $DB_NAME exists: PostgreSQL is not reachable as its administrator — nothing was removed. On Linux run \`sudo -v\` first, or set PG_SUPERUSER, then run again."
fi

# ── 1. Stop ────────────────────────────────────────────────────────────────────
echo ""
if [ -f "$ROOT/stop-ira.sh" ]; then
    bash "$ROOT/stop-ira.sh" >/dev/null 2>&1 && ok "IRA stopped" || warn "stop-ira.sh reported a problem; continuing"
fi

# ── 2. Back up, and refuse to go further without a usable archive ─────────────
if [ -f "$ENV_FILE" ] || [ "$DB_STATE" = present ]; then
    marker="$(mktemp)" || die "mktemp failed"
    bash "$SCRIPTS/ira-backup.sh" backup || { rm -f "$marker"; die "backup failed — nothing was removed"; }
    archive="$(find "$BACKUP_DIR" -maxdepth 1 -name 'ira-backup-*.tar.gz' -newer "$marker" 2>/dev/null | sort | tail -1)"
    rm -f "$marker"
    [ -n "$archive" ] || die "backup reported success but no new archive is in $BACKUP_DIR — nothing was removed"
    listing="$(tar -tzf "$archive" 2>/dev/null)" || die "cannot read $archive — nothing was removed"
    for member in database.dump env; do
        printf '%s\n' "$listing" | grep -qx "$member" || die "$archive has no $member — nothing was removed"
    done
    ok "backup checked: $archive"
else
    archive=""
    warn "no .env and no database: nothing to back up"
fi

# ── 3. Database ───────────────────────────────────────────────────────────────
if [ "$DB_STATE" = present ]; then
    as_su dropdb --if-exists "$DB_NAME" && ok "database $DB_NAME dropped" || die "could not drop $DB_NAME (backup kept: $archive)"
fi
if $REMOVE_ROLES; then
    for role in $ROLES; do
        if as_su psql -d postgres -v ON_ERROR_STOP=1 -qc "DROP ROLE IF EXISTS $role" >/dev/null 2>&1; then
            ok "role $role dropped"
        else
            warn "role $role kept: another database still uses it"
        fi
    done
fi

# ── 4. Files in this folder ──────────────────────────────────────────────────
for p in $LOCAL_PATHS; do
    if [ -e "$ROOT/$p" ]; then rm -rf "${ROOT:?}/$p" && ok "removed $p"; fi
done

# ── 5. Model (opt-in) ─────────────────────────────────────────────────────────
if $REMOVE_MODEL && [ -n "$MODEL" ]; then
    ollama rm "$MODEL" >/dev/null 2>&1 && ok "removed model $MODEL" || warn "could not remove model $MODEL"
fi

echo ""
printf "${GREEN}IRA is uninstalled.${NC}\n"
if [ -n "$archive" ]; then
    echo "  Your backup: $archive  (it contains your .env secrets; keep it safe)"
    echo "  To undo:     bash scripts/setup-native.sh"
    echo "               bash scripts/ira-backup.sh restore \"$archive\" --yes"
fi
echo "  To remove IRA completely, delete this folder: $ROOT"
