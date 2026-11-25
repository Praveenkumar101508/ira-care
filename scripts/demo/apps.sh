#!/usr/bin/env bash
# The carer dashboard (http://localhost:3000, with the browser watch at /watch and its presenter controls at /presenter) and the family app on the
# web (http://localhost:8081), built for the demo. Stop with Ctrl-C.
set -euo pipefail
cd "$(dirname "$0")/../.."
API="${IRA_CARE_API:-http://127.0.0.1:8000}"
# NEXT_PUBLIC_IRA_DEMO_PAGES=1: this is a demonstration build, so the browser watch and the presenter page exist.
(cd dashboard && NEXT_PUBLIC_IRA_DEMO_PAGES=1 NEXT_PUBLIC_CARE_API_BASE_URL="$API" npm run -s build && NEXT_PUBLIC_CARE_API_BASE_URL="$API" npx next start -p 3000) &
(cd mobile && npx expo export --platform web --output-dir .web-demo >/dev/null && npx serve -l 8081 .web-demo) &
wait
