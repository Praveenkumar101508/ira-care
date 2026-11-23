#!/usr/bin/env bash
# server  : apply migrations (as the migrator) then serve
# worker  : the care jobs
# migrate : apply migrations only
set -euo pipefail
cd /app
case "${1:-server}" in
  migrate)
    exec python -m scripts.migrate apply --dsn "${IRA_MIGRATOR_DSN:?set IRA_MIGRATOR_DSN}" ;;
  server)
    if [ -n "${IRA_MIGRATOR_DSN:-}" ]; then python -m scripts.migrate apply --dsn "$IRA_MIGRATOR_DSN"; fi
    exec uvicorn care.server:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' ;;
  worker)
    exec python -m care.worker ;;
  *)
    exec "$@" ;;
esac
