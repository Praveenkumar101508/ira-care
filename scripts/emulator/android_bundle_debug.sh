#!/usr/bin/env bash
# Make the generated Android project bundle the app's JavaScript into the DEBUG build too.
#
#   bash scripts/emulator/android_bundle_debug.sh mobile/android/app/build.gradle
#
# React Native's Gradle plugin bundles JavaScript only for variants that are not in
# `debuggableVariants` (default ["debug"]); a debug APK otherwise needs a development server.
# Emptying the list makes `assembleDebug` self-contained, so the phone emulator journey can run
# the debug build, the only one whose generated manifest allows cleartext HTTP to the emulator's
# host (Expo's android/app/src/debug/AndroidManifest.xml). Nothing about the release build or its
# network policy changes: the line goes inside the `react { ... }` block and names debug alone.
set -euo pipefail
gradle_file="${1:?path to android/app/build.gradle}"
if grep -qE '^\s*debuggableVariants\s*=\s*\[\]' "$gradle_file"; then
  echo "already bundling the debug variant: $gradle_file"
  exit 0
fi
grep -qE '^react \{' "$gradle_file" || { echo "no react { } block in $gradle_file" >&2; exit 1; }
# Insert directly after the opening line of the react block.
awk 'BEGIN{done=0} {print} /^react \{/ && !done {print "    debuggableVariants = []   // bundle the JavaScript into the debug APK too (emulator journey)"; done=1}' \
  "$gradle_file" > "$gradle_file.tmp" && mv "$gradle_file.tmp" "$gradle_file"
grep -nE '^\s*debuggableVariants\s*=\s*\[\]' "$gradle_file"
