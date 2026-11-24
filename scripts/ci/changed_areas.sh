#!/usr/bin/env bash
# Which areas of the repository changed in this CI event, as GitHub Actions outputs.
#
#   scripts/ci/changed_areas.sh <event_name> <pull_request_base_sha> <push_before_sha>
#
# Prints backend=, watch=, mobile=, dashboard= as true or false. Anything shared (the
# workflow itself, scripts/ci, the migrations, the deploy files) counts for every area,
# and an unknown base (a new branch, a force push, a missing sha) counts as "everything
# changed", so the safe answer is always the full run.
set -euo pipefail
event="${1:-}"; base="${2:-}"; before="${3:-}"
if [ "$event" = "pull_request" ] && [ -n "$base" ]; then range="$base...HEAD"
elif [ "$event" = "push" ] && [ -n "$before" ] && [ "$before" != "0000000000000000000000000000000000000000" ] && git cat-file -e "$before" 2>/dev/null; then range="$before..HEAD"
else range=""; fi
if [ -z "$range" ]; then
  for a in backend watch mobile dashboard; do echo "$a=true"; done; exit 0
fi
files=$(git diff --name-only "$range" || true)
if [ -z "$files" ]; then for a in backend watch mobile dashboard; do echo "$a=true"; done; exit 0; fi
shared=false
echo "$files" | grep -qE '^(\.github/|scripts/ci/|supracloud-jarvis/postgres/|deploy/|\.gitleaksignore)' && shared=true
area() { if [ "$shared" = true ] || echo "$files" | grep -qE "$2"; then echo "$1=true"; else echo "$1=false"; fi; }
area backend '^(supracloud-jarvis/|scripts/|e2e/|tests/)'
area watch '^watch/'
area mobile '^mobile/'
area dashboard '^dashboard/'
