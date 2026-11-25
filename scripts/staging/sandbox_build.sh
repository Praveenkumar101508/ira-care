#!/usr/bin/env bash
# Build the two IRA CARE images in a sandbox whose network policy blocks the operating
# system's package mirrors (deb.debian.org, dl-cdn.alpinelinux.org) and re-terminates TLS
# at an egress proxy. NOT how a deployment builds: use `docker compose build` there.
#
# It derives build files from deploy/Dockerfile.api and deploy/Dockerfile.dashboard with
# exactly two changes, and runs every other step unchanged:
#   1. The OS package step (apt-get curl postgresql-client / apk add curl) cannot run, so
#      it is replaced by a minimal `curl -fsS URL` stand-in for the health checks: Python in
#      the API image, busybox wget in the dashboard image. postgresql-client is not used by
#      the care server or the care worker's jobs.
#   2. The steps that download (the two pip installs, the encoder fetch, npm ci) go through
#      the sandbox's TLS-intercepting proxy; its CA is mounted as a BuildKit secret for those
#      steps only, so it is in no layer of either image.
# Usage: scripts/staging/sandbox_build.sh [api|dashboard|both]   (from the repository root)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT"
CA="${IRA_SANDBOX_CA:-/root/.ccr/ca-bundle.crt}"
PROXY="${HTTPS_PROXY:-${https_proxy:-}}"
OUT="$(mktemp -d)"; trap 'rm -rf "$OUT"' EXIT
which="${1:-both}"

python3 - "$OUT" <<'PY'
import sys, pathlib
out = pathlib.Path(sys.argv[1])
api = pathlib.Path("deploy/Dockerfile.api").read_text()
apt = api[api.index("RUN apt-get update"):api.index("useradd --create-home --uid 10001 ira") + len("useradd --create-home --uid 10001 ira")]
shim = ("RUN useradd --create-home --uid 10001 ira \\\n"
        "    && printf '%s\\n' '#!/usr/bin/env python3' 'import sys, urllib.request' "
        "'url = [a for a in sys.argv[1:] if not a.startswith(\"-\")][-1]' "
        "'try:' '    urllib.request.urlopen(url, timeout=5).read()' 'except Exception as e:' "
        "'    print(e, file=sys.stderr); sys.exit(22)' > /usr/local/bin/curl && chmod +x /usr/local/bin/curl")
assert apt in api
api = api.replace(apt, shim)
fetch = "RUN python /tmp/fetch_encoder.py"
assert fetch in api
api = api.replace(fetch, "RUN --mount=type=secret,id=ca,target=/run/ca.crt SSL_CERT_FILE=/run/ca.crt python /tmp/fetch_encoder.py")
# Both installs (requirements-api.lock, then requirements-nodeps.txt) are --no-deps.
nodeps = "RUN pip install --no-deps"
assert api.count(nodeps) == 2
api = api.replace(nodeps, "RUN --mount=type=secret,id=ca,target=/run/ca.crt PIP_CERT=/run/ca.crt pip install --no-deps")
(out / "Dockerfile.api").write_text(api)
dash = pathlib.Path("deploy/Dockerfile.dashboard").read_text()
apk = "RUN apk add --no-cache curl && adduser -D -u 10001 ira"
assert apk in dash
dash = dash.replace(apk, "RUN adduser -D -u 10001 ira && printf '%s\\n' '#!/bin/sh' 'for a; do u=$a; done' 'exec wget -q -O /dev/null \"$u\"' > /usr/local/bin/curl && chmod +x /usr/local/bin/curl")
npm = "RUN npm ci --no-audit --no-fund"
assert npm in dash
dash = dash.replace(npm, "RUN --mount=type=secret,id=ca,target=/run/ca.crt NODE_EXTRA_CA_CERTS=/run/ca.crt npm ci --no-audit --no-fund")
(out / "Dockerfile.dashboard").write_text(dash)
PY

common=(--network host --secret "id=ca,src=$CA" --build-arg "HTTPS_PROXY=$PROXY"
        --build-arg "NO_PROXY=localhost,127.0.0.1,pypi.org,files.pythonhosted.org,registry.npmjs.org")
build() {  # name, dockerfile, tag
  local s=$(date +%s)
  DOCKER_BUILDKIT=1 docker build "${common[@]}" -f "$OUT/$2" -t "$3" .
  echo "built $1 in $(( $(date +%s) - s )) s"
}
[ "$which" = dashboard ] || build api Dockerfile.api ira-care/api:local
[ "$which" = api ] || build dashboard Dockerfile.dashboard ira-care/dashboard:local
