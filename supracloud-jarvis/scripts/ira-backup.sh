#!/usr/bin/env bash
# =============================================================================
# ira-backup.sh — back up IRA, and restore it on this or another computer.
# Linux and macOS. Written for bash 3.2 (macOS).
#
#   bash scripts/ira-backup.sh backup              # -> ~/.ira/backups/ira-backup-<time>.tar.gz
#   bash scripts/ira-backup.sh restore <archive> --yes
#
# Why this is not the in-app backup (POST /api/v1/backup/create): the API runs
# as ira_app, which owns nothing and is held by forced row-level security. That
# is deliberate (postgres/042_role_ownership_separation.sql), and it means
# pg_dump as ira_app is refused ("query would be affected by row-level security
# policy"). Turning RLS on for the dump would be worse: with no tenant bound,
# every table dumps EMPTY. A complete backup needs a role that sees every row,
# so it runs here, as the local database superuser, outside the app.
#
# What an archive holds:
#   database.dump    complete pg_dump (custom format) of IRA's database
#   env              the .env — its keys decrypt TOTP secrets, voiceprints and
#                    connector credentials in the dump. Without it a restored
#                    database cannot open its own encrypted data.
#   data-<name>.tar  each folder IRA keeps outside the database (DATA_LABELS
#                    below): notes, project folders and their .env files, the
#                    sealed vault, uploads, coding-agent tasks, "about me" files
#   MANIFEST.txt     when, which commit, which PostgreSQL, where each folder
#                    was, checksums
# The archive therefore contains secrets: it is written mode 600. Keep it as
# carefully as the computer it came from.
#
# Moving to another computer:
#   1. old computer:  bash scripts/ira-backup.sh backup      (IRA may be running)
#   2. new computer:  install IRA with scripts/setup-native.sh, then ./stop-ira.sh
#   3. new computer:  bash scripts/ira-backup.sh restore <archive> --yes
#   4. new computer:  ./start-ira.sh — and sign in with the OLD password.
# Restore replaces this computer's IRA database, .env and data folders. It
# takes a safety backup of all three first, and moves each folder it replaces
# into the backups folder rather than deleting it.
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
VENV="$ROOT/.venv"
GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
ok()  { printf "  ${GREEN}[OK]${NC}   %s\n" "$1"; }
die() { printf "  ${RED}[FAIL]${NC} %s\n" "$1"; exit 1; }

env_value() {  # read KEY from a .env file without sourcing it
    # `tr -d '\r'`: a .env written on Windows ends its lines with CR LF, and the
    # CR would otherwise stay in the value (macOS sed cannot match \r itself).
    [ -f "${2:-$ENV_FILE}" ] || return 0
    sed -n "s/^$1=//p" "${2:-$ENV_FILE}" | tail -1 | tr -d '\r' | sed 's/[[:space:]]*#.*$//; s/^"//; s/"$//' \
        | sed "s/^'\\(.*\\)'\$/\\1/"   # a single-quoted value, as python-dotenv reads it
}

# A value for a single-quoted psql argument. psql reads a backslash there as an
# escape, so the backslash is doubled as well as the quote: doubling only the
# quote turned `p\w'q` into `pw'q`, and the restored app could not log in. sed
# rather than ${//}: bash 3.2 (macOS) and 4.3+ treat backslashes in a
# replacement differently. The value reaches sed on stdin, never on a command line.
psql_quote() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e "s/'/''/g"; }

# Homebrew's PostgreSQL is keg-only: pg_dump is not on PATH by default.
if ! command -v pg_dump >/dev/null 2>&1 && command -v brew >/dev/null 2>&1; then
    for _pg in $(brew list --formula 2>/dev/null | grep -E '^postgresql@[0-9]+$' | sort -r); do
        _pg_bin="$(brew --prefix "$_pg" 2>/dev/null)/bin"
        if [ -x "$_pg_bin/pg_dump" ]; then PATH="$_pg_bin:$PATH"; export PATH; break; fi
    done
fi
command -v pg_dump >/dev/null 2>&1 || die "pg_dump not found — install the PostgreSQL client"

PG_PORT="$(env_value POSTGRES_PORT)"; PG_PORT="${PG_PORT:-5432}"
DB_NAME="$(env_value POSTGRES_DB)"; DB_NAME="${DB_NAME:-jarvis_db}"

# The same superuser access setup-native.sh uses.
as_su() {  # as_su <pg command> [args...]
    local cmd="$1"; shift
    if [ -n "${PG_SUPERUSER:-}" ]; then "$cmd" -h localhost -p "$PG_PORT" -U "$PG_SUPERUSER" "$@"
    elif [ "$(uname -s)" = Darwin ]; then "$cmd" -h localhost -p "$PG_PORT" "$@"
    elif [ "$(id -u)" = 0 ]; then runuser -u postgres -- "$cmd" -p "$PG_PORT" "$@"
    else sudo -u postgres "$cmd" -p "$PG_PORT" "$@"; fi
}
# Temporary directories, removed however the script ends.
WORKDIRS=""
cleanup() { local d; for d in $WORKDIRS; do rm -rf "$d"; done; }
trap cleanup EXIT
new_workdir() { _w="$(mktemp -d)" || die "mktemp failed"; WORKDIRS="$WORKDIRS $_w"; }

sha() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | awk '{print $1}'; }

# The folders IRA keeps outside the database. Each is found the way the API
# finds it: the .env value when one is set, else IRA's default. start-ira.sh
# runs the API inside ira/, so a relative path is relative to ira/.
#   notes           NOTES_DIR           data/notes           notes, as .md files
#   projects        PROJECTS_DIR        data/projects        project folders
#   secrets         SECRETS_DIR         data/secrets         each project's .env
#   armory          ARMORY_VAULT_DIR    data/armory          the sealed vault
#   files           FILES_DATA_DIR      ../data/files        uploads
#   forge           (no setting)        data/forge           coding-agent tasks
#   self_knowledge  SELF_KNOWLEDGE_DIR  data/self_knowledge  "about me" files
# Left out on purpose: data/interview, a downloaded question set that is not
# redistributable and that its notice says how to rebuild.
DATA_LABELS="notes projects secrets armory files forge self_knowledge"
data_dir() {  # data_dir <label> [.env file] -> that folder's path here
    local key="" def v
    case "$1" in
        notes) key=NOTES_DIR; def=data/notes ;;
        projects) key=PROJECTS_DIR; def=data/projects ;;
        secrets) key=SECRETS_DIR; def=data/secrets ;;
        armory) key=ARMORY_VAULT_DIR; def=data/armory ;;
        files) key=FILES_DATA_DIR; def="$ROOT/data/files" ;;
        forge) def=data/forge ;;
        self_knowledge) key=SELF_KNOWLEDGE_DIR; def=data/self_knowledge ;;
        *) return 1 ;;
    esac
    v=""; [ -z "$key" ] || v="$(env_value "$key" "${2:-$ENV_FILE}")"
    [ -n "$v" ] || v="$def"
    case "$v" in
        /*) ;;
        # Only the self-knowledge setting expands ~ (memory/self_knowledge.py);
        # the others take it literally, as a folder named ~ inside ira/.
        "~/"*) if [ "$1" = self_knowledge ]; then v="$HOME/${v#\~/}"; else v="$ROOT/ira/$v"; fi ;;
        *) v="$ROOT/ira/$v" ;;
    esac
    printf '%s\n' "$v"
}
# The folder's path with every link resolved, as Python's Path.resolve() gives
# it; empty when the folder does not exist.
real_dir() { (cd "$1" 2>/dev/null && pwd -P); }
default_backup_dir() {
    local d; d="$(env_value BACKUP_DIR)"
    { [ -z "$d" ] || [ "$d" = /backups ]; } && d="$HOME/.ira/backups"
    printf '%s\n' "$d"
}

backup() {  # backup [output dir]
    local dest="${1:-}"
    [ -n "$dest" ] || dest="$(default_backup_dir)"
    mkdir -p "$dest" && chmod 700 "$dest" || die "cannot write to $dest"
    [ -f "$ENV_FILE" ] || die "no .env at $ENV_FILE — nothing to back up"
    local stamp work archive
    stamp="$(date -u +%Y%m%d-%H%M%S)"
    new_workdir; work="$_w"
    umask 077
    as_su pg_dump -Fc -d "$DB_NAME" > "$work/database.dump" \
        || die "pg_dump of $DB_NAME failed — is PostgreSQL running?"
    cp "$ENV_FILE" "$work/env"
    # One tar per data folder. A folder that does not exist here is left out,
    # and a restore of this archive then leaves that folder alone.
    local label dir members="database.dump env MANIFEST.txt" where="" sums="" files_root=""
    for label in $DATA_LABELS; do
        dir="$(real_dir "$(data_dir "$label")")"
        [ -n "$dir" ] || continue
        tar -cf "$work/data-$label.tar" -C "$dir" . \
            || die "could not archive $dir (if IRA was writing to it, run the backup again)"
        members="$members data-$label.tar"
        where="${where}data $label: $dir
"
        sums="${sums}sha256 data-$label.tar: $(sha "$work/data-$label.tar")
"
        [ "$label" = files ] && files_root="$dir"
    done
    {
        echo "IRA backup"
        echo "created_utc: $stamp"
        echo "commit: $(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
        echo "database: $DB_NAME"
        echo "postgresql: $(as_su psql -d "$DB_NAME" -tAc 'SHOW server_version' 2>/dev/null)"
        echo "migrations_applied: $(as_su psql -d "$DB_NAME" -tAc 'SELECT count(*) FROM schema_migrations' 2>/dev/null)"
        printf '%s' "$where"
        # Uploads' rows in the database name each file by its full path under
        # this folder; a restore elsewhere rewrites that start of the path.
        [ -z "$files_root" ] || echo "files_root: $files_root"
        echo "sha256 database.dump: $(sha "$work/database.dump")"
        echo "sha256 env: $(sha "$work/env")"
        printf '%s' "$sums"
    } > "$work/MANIFEST.txt"
    # Never over another archive: a restore takes two safety copies when the
    # archive names a second database, often within the same second.
    archive="$dest/ira-backup-$stamp.tar.gz"; local n=1
    while [ -e "$archive" ]; do n=$((n + 1)); archive="$dest/ira-backup-$stamp-$n.tar.gz"; done
    # $members is a list of names this script chose; it is split on purpose.
    # shellcheck disable=SC2086
    tar -czf "$archive" -C "$work" $members || die "could not write $archive"
    chmod 600 "$archive"
    ok "backup written: $archive ($(du -h "$archive" | cut -f1)) — it contains your .env secrets"
}

# Put each archived folder back where the restored .env says IRA keeps it. A
# folder already there is moved into the backups folder, never deleted, and
# never left beside the original: two of these folders are not git-ignored.
restore_data() {  # restore_data <work dir> <folder for what it replaces>
    local f label dir target
    for f in "$1"/data-*.tar; do
        [ -e "$f" ] || continue
        label="$(basename "$f" .tar)"; label="${label#data-}"
        if ! dir="$(data_dir "$label")"; then
            echo "  skipped $(basename "$f"): this version of IRA keeps no such folder"
            continue
        fi
        # A linked folder keeps its link: what changes is where it points.
        if [ -L "$dir" ]; then target="$(real_dir "$dir")"; [ -z "$target" ] || dir="$target"; fi
        if [ -d "$dir" ] && [ -z "$(ls -A "$dir" 2>/dev/null)" ]; then
            rmdir "$dir"
        elif [ -e "$dir" ]; then
            { mkdir -p "$2" && chmod 700 "$2" && mv "$dir" "$2/$label"; } \
                || die "could not move $dir aside — the database is restored, the data folders are not"
            echo "  the $label folder it replaces is kept in $2/$label"
        fi
        # Each project's .env and the sealed vault stay this account's alone, as
        # ira-backup.ps1 keeps them. An archive made on Windows records every
        # folder as 0777 and every file as 0666, and tar applies the archive's
        # mode to the folder, so it is restricted before its files arrive and after.
        local private=""
        case "$label" in secrets|armory) private=1 ;; esac
        { mkdir -p "$dir" && { [ -z "$private" ] || chmod 700 "$dir"; } && tar -xf "$f" -C "$dir" \
            && { [ -z "$private" ] || chmod 700 "$dir"; }; } || die "could not restore the $label folder into $dir"
        ok "$label folder restored to $dir"
    done
}

# Each upload's row names its file by full path, as the old computer had it.
# When this computer keeps uploads in a different folder, point the rows at it.
# Only paths under the old folder change. A Windows archive's rows (C:\...)
# match nothing here and are left as they are.
repoint_uploads() {  # repoint_uploads <work dir>
    local old new n
    old="$(sed -n 's/^files_root: //p' "$1/MANIFEST.txt" | head -1)"
    { [ -n "$old" ] && [ -f "$1/data-files.tar" ]; } || return 0
    new="$(real_dir "$(data_dir files)")"
    { [ -n "$new" ] && [ "$new" != "$old" ]; } || return 0
    n="$(printf '%s\n' "\\set old '$(psql_quote "$old")'" "\\set new '$(psql_quote "$new")'" \
        "WITH moved AS (UPDATE files SET storage_path = :'new' || substr(storage_path, length(:'old') + 1)" \
        "    WHERE left(storage_path, length(:'old') + 1) = :'old' || '/' RETURNING 1)" \
        "SELECT count(*) FROM moved;" | as_su psql -d "$DB_NAME" -v ON_ERROR_STOP=1 -tA -q -f -)" \
        || die "could not point the uploads at $new — the files are there, but their rows still name $old"
    ok "uploads: $n file rows now name $new (they named $old)"
}

restore() {  # restore <archive> --yes
    local archive="${1:-}" confirm="${2:-}"
    # A bare name is looked for in the backup directory too: that is where
    # `backup` writes, and INSTALL.md gives the name alone.
    local backups; backups="$(default_backup_dir)"
    if [ -n "$archive" ] && [ ! -f "$archive" ]; then
        [ -f "$backups/$archive" ] && archive="$backups/$archive"
    fi
    [ -f "$archive" ] || die "usage: ira-backup.sh restore <archive.tar.gz> --yes (no such file: ${archive:-none}; backups are written to ~/.ira/backups)"
    [ "$confirm" = "--yes" ] || die "restore REPLACES this computer's IRA database, .env and data folders — re-run with --yes"
    local api_port; api_port="$(env_value IRA_API_PORT)"; api_port="${api_port:-8000}"
    if curl -sf --max-time 3 "http://127.0.0.1:$api_port/health" >/dev/null 2>&1; then
        die "IRA is running — stop it first with ./stop-ira.sh"
    fi
    [ -x "$VENV/bin/python" ] || die "no .venv — install IRA on this computer with scripts/setup-native.sh first"

    local work; new_workdir; work="$_w"
    tar -xzf "$archive" -C "$work" || die "cannot read $archive"
    [ -f "$work/database.dump" ] && [ -f "$work/env" ] || die "$archive is not an IRA backup"
    grep -q "sha256 database.dump: $(sha "$work/database.dump")" "$work/MANIFEST.txt" \
        || die "database.dump does not match its manifest checksum — refusing to restore it"
    # Every data folder's checksum too, before anything is changed. An archive
    # made before data folders were included has none, and restores as before.
    local f
    for f in "$work"/data-*.tar; do
        [ -e "$f" ] || continue
        grep -q "sha256 $(basename "$f"): $(sha "$f")" "$work/MANIFEST.txt" \
            || die "$(basename "$f") does not match its manifest checksum — refusing to restore it"
    done
    local replaced; replaced="$backups/replaced-$(date -u +%Y%m%d-%H%M%S)"

    local restored_db; restored_db="$(env_value POSTGRES_DB "$work/env")"; restored_db="${restored_db:-jarvis_db}"
    [[ "$restored_db" =~ ^[A-Za-z0-9_]{1,63}$ ]] || die "the archive names an unusable database ($restored_db)"

    # A safety copy of every database this restore replaces, so a mistaken
    # restore is undoable: this install's, and the one the archive names when
    # that is a different database that already exists. Only this install's
    # used to be copied, and the archive's was then dropped with no copy.
    if [ -f "$ENV_FILE" ]; then
        local current="$DB_NAME" dbs="$DB_NAME" db
        [ "$restored_db" = "$current" ] || dbs="$current $restored_db"
        for db in $dbs; do
            [ -n "$(as_su psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$db'")" ] || continue
            echo "  safety backup of database $db, which this restore replaces:"
            DB_NAME="$db"; backup || die "safety backup failed — nothing was changed"
        done
        DB_NAME="$current"
    fi
    [ -f "$ENV_FILE" ] && cp -p "$ENV_FILE" "$ROOT/.env.backup-$(date -u +%Y%m%d-%H%M%S)"
    (umask 077; cp "$work/env" "$ENV_FILE") && chmod 600 "$ENV_FILE"
    DB_NAME="$restored_db"
    ok ".env restored (the previous one is kept as .env.backup-*)"

    # An empty database, the roles and extensions, then the data.
    as_su dropdb --if-exists "$DB_NAME" || die "could not drop $DB_NAME"
    as_su createdb "$DB_NAME" || die "could not create $DB_NAME"
    as_su psql -d "$DB_NAME" -v ON_ERROR_STOP=1 -q -f - < "$ROOT/postgres/provisioning/001_database_roles.sql" >/dev/null \
        || die "could not provision the database roles"
    as_su pg_restore -d "$DB_NAME" --exit-on-error < "$work/database.dump" \
        || die "pg_restore failed — the safety backup can put the previous state back"
    ok "database $DB_NAME restored"
    restore_data "$work" "$replaced"
    repoint_uploads "$work"

    # The restored .env's password for the runtime role, and any migrations
    # this (possibly newer) checkout has that the backup predates.
    local pw mig
    pw="$(env_value POSTGRES_PASSWORD)"
    set_pw() { _q="$(psql_quote "$2")"; printf '%s\n' "\\set pw '$_q'" \
        "SELECT format('ALTER ROLE %I PASSWORD %L', '$1', :'pw')\\gexec" | as_su psql -d postgres -q -f - >/dev/null; }
    set_pw ira_app "$pw" || die "could not set the ira_app password"
    mig="$("$VENV/bin/python" -c 'import secrets; print(secrets.token_hex(24))')"
    set_pw ira_migrator "$mig"
    ( cd "$ROOT/ira" && PGPASSWORD="$mig" "$VENV/bin/python" -m scripts.migrate apply \
        --dsn "postgresql://ira_migrator@127.0.0.1:$PG_PORT/$DB_NAME" ) >/dev/null 2>&1
    local status=$?
    printf '%s\n' "ALTER ROLE ira_migrator PASSWORD NULL;" | as_su psql -d postgres -q -f - >/dev/null
    [ "$status" -eq 0 ] || die "migrations after restore failed — run scripts/setup-native.sh to see why"
    ok "schema current; start IRA with ./start-ira.sh and sign in with the restored password"
}

case "${1:-}" in
    backup) backup "${2:-}" ;;
    restore) restore "${2:-}" "${3:-}" ;;
    *) sed -n '2,37p' "$0"; exit 2 ;;
esac
