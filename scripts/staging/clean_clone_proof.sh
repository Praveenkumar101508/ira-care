#!/usr/bin/env bash
# IRA CARE staging from a clean clone (round 3, phase 14).
#
#   scripts/staging/clean_clone_proof.sh [--keep] [--skip-e2e]
#
# Clones this repository's HEAD into a fresh directory and brings the product up the
# way docs/DEPLOY.md and deploy/docker-compose.yml do, with nothing from the working
# tree: the deployment's PostgreSQL and Redis containers (deploy/compose.staging.yml
# maps their ports to this machine), a fresh Python environment with the server image's
# requirements, the migration chain applied as ira_migrator, the care server and the
# care worker as the image's entrypoint starts them, the dashboard built and served as
# its image serves it (standalone), the phone app exported for the web, the demo
# organisation seeded, then the end-to-end browser tests and the outage tests in e2e/.
# It writes docs/evidence/staging/clean_clone_proof.json with every step's timing and
# result, and tears everything down unless --keep is given.
#
# What it does not do here: build the two application images (deploy/Dockerfile.api and
# Dockerfile.dashboard) and run Caddy with a public certificate; those need a machine
# with ordinary network access and a domain name (docs/DEPLOY.md). Everything the images
# run is run here from the same files.
set -euo pipefail

SRC="$(git rev-parse --show-toplevel)"
REV="$(git -C "$SRC" rev-parse HEAD)"
WORK="${IRA_STAGING_DIR:-/tmp/ira-care-staging}"
KEEP=0; SKIP_E2E=0
for a in "$@"; do case "$a" in --keep) KEEP=1 ;; --skip-e2e) SKIP_E2E=1 ;; esac; done

API_PORT="${IRA_STAGING_API_PORT:-8010}"
DASH_PORT="${IRA_STAGING_DASH_PORT:-3010}"
APP_PORT="${IRA_STAGING_APP_PORT:-8091}"
export IRA_STAGING_PG_PORT="${IRA_STAGING_PG_PORT:-5433}"
export IRA_STAGING_REDIS_PORT="${IRA_STAGING_REDIS_PORT:-6381}"
OLLAMA_URL="${IRA_STAGING_OLLAMA_URL:-http://127.0.0.1:11434}"

EVIDENCE="$SRC/docs/evidence/staging/clean_clone_proof.json"
mkdir -p "$(dirname "$EVIDENCE")"
STEPS=()
T0=$(date +%s)
step() { # name, status, seconds, note
  STEPS+=("{\"step\": \"$1\", \"status\": \"$2\", \"seconds\": $3, \"note\": \"${4:-}\"}")
  printf '%-32s %-8s %5ss  %s\n' "$1" "$2" "$3" "${4:-}"
}
timed() { # name, command...
  local name="$1"; shift; local s=$(date +%s)
  if "$@" >"$WORK/logs/$name.log" 2>&1; then step "$name" ok $(( $(date +%s) - s )); else step "$name" failed $(( $(date +%s) - s )) "see logs/$name.log"; return 1; fi
}
PIDS=()
cleanup() {
  local rc=$?
  if [ "$KEEP" = 1 ]; then
    printf '%s\n' "${PIDS[@]:-}" > "$WORK/pids"
    echo "kept: $WORK/repo; the server, worker, dashboard and phone app are still running (pids in $WORK/pids; stop them with: xargs kill < $WORK/pids; containers: docker compose ... down -v)"
  else
    for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done
    (cd "$WORK/repo" && docker compose -f deploy/docker-compose.yml -f deploy/compose.staging.yml --env-file deploy/.env down -v >/dev/null 2>&1) || true
  fi
  write_evidence "$rc"
  exit $rc
}
write_evidence() {
  local rc="$1"; local joined; joined=$(IFS=,; echo "${STEPS[*]}")
  python3 - "$EVIDENCE" "$REV" "$rc" "$(( $(date +%s) - T0 ))" "$joined" <<'PY'
import json, sys, datetime, platform
out, rev, rc, total, steps = sys.argv[1:6]
doc = {"measured_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"), "revision": rev,
       "machine": platform.platform(), "result": "passed" if rc == "0" else "failed", "total_seconds": int(total),
       "steps": json.loads("[" + steps + "]") if steps else []}
open(out, "w").write(json.dumps(doc, indent=2) + "\n")
PY
}
trap cleanup EXIT

rm -rf "$WORK"; mkdir -p "$WORK/logs"
timed clone git clone -q "$SRC" "$WORK/repo"
cd "$WORK/repo"; git checkout -q "$REV"

# 1. deploy/.env, as docs/DEPLOY.md section 3 says, with fresh secrets (never written anywhere else).
python3 - > deploy/.env <<PY
import secrets, base64, os
print("IRA_CARE_DOMAIN=localhost")
print("IRA_ENVIRONMENT=staging")
print("POSTGRES_DB=ira_care")
for k in ("POSTGRES_SUPERUSER_PASSWORD", "IRA_APP_PASSWORD", "IRA_MIGRATOR_PASSWORD", "IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "BACKUP_PASSPHRASE"):
    print(f"{k}={secrets.token_urlsafe(24)}")
print("IRA_CONNECTOR_ENCRYPTION_KEY=" + base64.b64encode(os.urandom(32)).decode())
print("IRA_MIGRATE_ON_START=true")
print("IRA_CARE_MODEL=ollama")
print("IRA_CARE_SMS_PROVIDER=log")
print("IRA_MAIL_TRANSPORT=null")
PY
chmod 600 deploy/.env
set -a; . deploy/.env; set +a
step env ok 0 "deploy/.env written with fresh secrets"

# 2. The deployment's database and Redis containers.
COMPOSE=(docker compose -f deploy/docker-compose.yml -f deploy/compose.staging.yml --env-file deploy/.env)
timed compose-up "${COMPOSE[@]}" up -d --wait postgres redis

# 3. A fresh Python environment with the server image's requirements (deploy/Dockerfile.api).
# The image uses python:3.12; uv gives that version anywhere, a system python3.12 otherwise.
if command -v uv >/dev/null; then uv venv -q --python 3.12 .venv; PY=.venv/bin/python
elif command -v python3.12 >/dev/null; then python3.12 -m venv .venv; PY=.venv/bin/python
else python3 -m venv .venv; PY=.venv/bin/python; fi
timed pip-install bash -c "if [ -x .venv/bin/pip ]; then .venv/bin/pip install -q -r supracloud-jarvis/ira/requirements-test.txt psutil==6.1.0; else uv pip install -q --python $PY -r supracloud-jarvis/ira/requirements-test.txt psutil==6.1.0; fi"

# 4. Migrations as ira_migrator, by the image's own entry point (deploy/entrypoint.sh "migrate").
export IRA_MIGRATOR_DSN="postgresql://ira_migrator:${IRA_MIGRATOR_PASSWORD}@127.0.0.1:${IRA_STAGING_PG_PORT}/${POSTGRES_DB}"
timed migrate env IRA_APP_DIR="$WORK/repo/supracloud-jarvis/ira" PATH="$WORK/repo/.venv/bin:$PATH" bash deploy/entrypoint.sh migrate

# 5. The care server and the care worker, with the compose file's environment.
export IRA_EDITION=cloud POSTGRES_HOST=127.0.0.1 POSTGRES_PORT="$IRA_STAGING_PG_PORT" POSTGRES_USER=ira_app POSTGRES_PASSWORD="$IRA_APP_PASSWORD"
export REDIS_HOST=127.0.0.1 REDIS_PORT="$IRA_STAGING_REDIS_PORT" REDIS_PASSWORD="" VLLM_API_KEY=unused
export IRA_CARE_PUBLIC_URL="http://localhost:$API_PORT" IRA_CARE_OLLAMA_URL="$OLLAMA_URL"
export IRA_CARE_MODEL_PRIMARY="${IRA_CARE_MODEL_PRIMARY:-gemma4:e4b}" IRA_CARE_MODEL_SECONDARY="${IRA_CARE_MODEL_SECONDARY:-gemma4:e2b}"
export IRA_WORKER_JOBS=care_checkins,care_monitoring,care_reports,care_retention IRA_CARE_JOBS_IN_API=false
export IRA_CARE_ALLOWED_ORIGINS="http://localhost:$DASH_PORT,http://127.0.0.1:$DASH_PORT,http://localhost:$APP_PORT,http://127.0.0.1:$APP_PORT"
export IRA_LEDGER_JOURNAL_DIR="$WORK/ledger"; mkdir -p "$IRA_LEDGER_JOURNAL_DIR"
(cd supracloud-jarvis/ira && exec "../../$PY" -m uvicorn care.server:app --host 127.0.0.1 --port "$API_PORT" --proxy-headers > "$WORK/logs/api.log" 2>&1) & PIDS+=($!)
s=$(date +%s); for i in $(seq 1 60); do curl -fsS "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "http://127.0.0.1:$API_PORT/health" >/dev/null && step api-up ok $(( $(date +%s) - s )) "care.server on :$API_PORT" || { step api-up failed $(( $(date +%s) - s )); exit 1; }
(cd supracloud-jarvis/ira && exec "../../$PY" -m care.worker > "$WORK/logs/worker.log" 2>&1) & PIDS+=($!)
s=$(date +%s); for i in $(seq 1 90); do curl -fsS "http://127.0.0.1:$API_PORT/health/jobs" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "http://127.0.0.1:$API_PORT/health/jobs" >/dev/null && step worker-up ok $(( $(date +%s) - s )) || step worker-up failed $(( $(date +%s) - s )) "jobs not reported healthy"

# 6. The dashboard as its image serves it: npm ci, next build, the standalone server.
timed dashboard-install bash -c "cd dashboard && npm ci --no-audit --no-fund"
timed dashboard-build bash -c "cd dashboard && NEXT_PUBLIC_CARE_API_BASE_URL=http://127.0.0.1:$API_PORT npm run build && cp -r .next/static .next/standalone/.next/static && cp -r public .next/standalone/public 2>/dev/null || true"
(cd dashboard/.next/standalone && PORT="$DASH_PORT" HOSTNAME=127.0.0.1 exec node server.js > "$WORK/logs/dashboard.log" 2>&1) & PIDS+=($!)
s=$(date +%s); for i in $(seq 1 60); do curl -fsS "http://127.0.0.1:$DASH_PORT/login" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "http://127.0.0.1:$DASH_PORT/login" >/dev/null && step dashboard-up ok $(( $(date +%s) - s )) || { step dashboard-up failed $(( $(date +%s) - s )); exit 1; }

# 7. The phone app on the web.
timed app-install bash -c "cd mobile && npm ci --no-audit --no-fund"
timed app-export bash -c "cd mobile && npx expo export --platform web --output-dir .web-staging"
(cd mobile && exec npx -y serve -l "$APP_PORT" .web-staging > "$WORK/logs/app.log" 2>&1) & PIDS+=($!)
s=$(date +%s); for i in $(seq 1 60); do curl -fsS "http://127.0.0.1:$APP_PORT/" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "http://127.0.0.1:$APP_PORT/" >/dev/null && step app-up ok $(( $(date +%s) - s )) || { step app-up failed $(( $(date +%s) - s )); exit 1; }

# 8. The demo organisation (made-up people; fresh passwords in .demo/, never committed).
export IRA_TEST_DATABASE_DSN="postgresql://ira_app:${IRA_APP_PASSWORD}@127.0.0.1:${IRA_STAGING_PG_PORT}/${POSTGRES_DB}"
export IRA_TEST_ADMIN_DSN="postgresql://postgres:${POSTGRES_SUPERUSER_PASSWORD}@127.0.0.1:${IRA_STAGING_PG_PORT}/${POSTGRES_DB}"
timed seed bash -c "IRA_CARE_MODEL=ollama $PY scripts/demo/seed_demo.py"

# 9. The end-to-end browser tests and the outage tests.
if [ "$SKIP_E2E" = 0 ]; then
  timed e2e-install bash -c "cd e2e && npm ci --no-audit --no-fund"
  export IRA_CARE_API="http://127.0.0.1:$API_PORT" DASHBOARD_URL="http://localhost:$DASH_PORT" FAMILY_URL="http://localhost:$APP_PORT"
  export IRA_DEMO_INFO="$WORK/repo/.demo/demo.local.json" IRA_STAGING_WORK="$WORK/repo" IRA_E2E_OUT="$SRC/docs/evidence/staging"
  export IRA_COMPOSE="docker compose -f $WORK/repo/deploy/docker-compose.yml -f $WORK/repo/deploy/compose.staging.yml --env-file $WORK/repo/deploy/.env"
  if timed e2e bash -c "cd e2e && npx playwright test --trace off"; then :; else cp -f e2e/results.json "$SRC/docs/evidence/staging/e2e_results.json" 2>/dev/null || true; exit 1; fi
  cp -f e2e/results.json "$SRC/docs/evidence/staging/e2e_results.json" 2>/dev/null || true
fi
echo "staging proof passed in $(( $(date +%s) - T0 )) s"
