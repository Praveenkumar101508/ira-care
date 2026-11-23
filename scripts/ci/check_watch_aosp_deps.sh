#!/usr/bin/env bash
# The aosp flavour of the watch app must carry no Google service dependency:
# no Play Services, no Firebase, no Health Services client. CI runs this after
# the build; it prints the offending lines and fails if any exist.
set -euo pipefail
cd "$(dirname "$0")/../../watch"
report=$(./gradlew --no-daemon --console=plain -q :app:dependencies --configuration aospDebugRuntimeClasspath 2>/dev/null)
pattern='com\.google\.android\.gms|com\.google\.firebase|androidx\.health:health-services-client|com\.google\.android\.wearable:wearable'
if echo "$report" | grep -E "$pattern"; then
  echo "The aosp flavour depends on a Google service library (above). It must not." >&2
  exit 1
fi
count=$(echo "$report" | grep -cE '^[+|\\ ]*[-+]-- ' || true)
echo "aosp runtime classpath: $count dependency lines, none from a Google service."
