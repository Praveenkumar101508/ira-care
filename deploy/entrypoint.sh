#!/usr/bin/env bash
# server  : apply migrations (as the migrator) then serve
# worker  : the care jobs
# migrate : apply migrations only
#
# The migration tool refuses a staging or production database unless the person running
# it says they are authorised. In this deployment that person is the operator who set
# IRA_MIGRATE_ON_START=true in deploy/.env (docs/DEPLOY.md, section 3); the image passes
# their authorisation on. With the setting off, the server starts without migrating and
# the operator runs `docker compose run --rm api migrate` when they choose.
set -euo pipefail
cd "${IRA_APP_DIR:-/app}"

migrate_flags=()
if [ "${IRA_MIGRATE_ON_START:-false}" = "true" ]; then
  migrate_flags+=("--i-am-authorised-to-migrate-production")
fi

case "${1:-server}" in
  migrate)
    exec python -m scripts.migrate apply --dsn "${IRA_MIGRATOR_DSN:?set IRA_MIGRATOR_DSN}" "${migrate_flags[@]}" ;;
  server)
    if [ -n "${IRA_MIGRATOR_DSN:-}" ] && [ "${IRA_MIGRATE_ON_START:-false}" = "true" ]; then
      python -m scripts.migrate apply --dsn "$IRA_MIGRATOR_DSN" "${migrate_flags[@]}"
    fi
    exec uvicorn care.server:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' ;;
  worker)
    exec python -m care.worker ;;
  *)
    exec "$@" ;;
esac
