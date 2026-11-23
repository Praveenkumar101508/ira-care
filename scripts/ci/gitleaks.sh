#!/usr/bin/env bash
# Scan the whole git history for secrets with gitleaks. Used by CI and locally.
#
#   scripts/ci/gitleaks.sh            # scans the repository this script lives in
#
# The binary is downloaded from the gitleaks release page, pinned by version and
# SHA-256. Accepted historical findings are listed in .gitleaksignore with the
# reason for each; anything else fails the scan.
set -euo pipefail
cd "$(dirname "$0")/../.."
VERSION=8.24.3
SHA256=9991e0b2903da4c8f6122b5c3186448b927a5da4deef1fe45271c3793f4ee29c
BIN_DIR="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/gitleaks-${VERSION}"
if [ ! -x "$BIN_DIR/gitleaks" ]; then
  mkdir -p "$BIN_DIR"
  curl -fsSL -o "$BIN_DIR/gitleaks.tgz" \
    "https://github.com/gitleaks/gitleaks/releases/download/v${VERSION}/gitleaks_${VERSION}_linux_x64.tar.gz"
  echo "${SHA256}  $BIN_DIR/gitleaks.tgz" | sha256sum -c -
  tar -xzf "$BIN_DIR/gitleaks.tgz" -C "$BIN_DIR" gitleaks
fi
"$BIN_DIR/gitleaks" version
exec "$BIN_DIR/gitleaks" git --no-banner --redact=100 --exit-code 1 .
