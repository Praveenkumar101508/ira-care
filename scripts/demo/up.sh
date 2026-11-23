#!/usr/bin/env bash
# IRA CARE demo: Redis, the migration chain, the demo organisation, the demo API.
# Everything is local and fake. Stop with Ctrl-C.
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${IRA_TEST_DATABASE_DSN:=postgresql://ira_app:ira_app@127.0.0.1:5432/ira_test}"
: "${IRA_TEST_ADMIN_DSN:=postgresql://postgres:postgres@127.0.0.1:5432/ira_test}"
: "${IRA_MIGRATOR_DSN:=postgresql://ira_migrator:ira_migrator@127.0.0.1:5432/ira_test}"
export IRA_TEST_DATABASE_DSN IRA_TEST_ADMIN_DSN
PY=supracloud-jarvis/.venv/bin/python
[ -x "$PY" ] || PY=python3
redis-cli ping >/dev/null 2>&1 || redis-server --daemonize yes >/dev/null
(cd supracloud-jarvis/ira && "../.venv/bin/python" -m scripts.migrate apply --dsn "$IRA_MIGRATOR_DSN")
"$PY" scripts/demo/seed_demo.py
echo "Demo API on http://127.0.0.1:${IRA_CARE_DEMO_PORT:-8000}"
exec "$PY" scripts/demo/api.py
