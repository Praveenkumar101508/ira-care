# Licence check of shipped dependencies

Requirement K.d. `scripts/licence_check.py` lists the licence of every third-party package we
ship, checks each one against the policy below, and keeps the result in
`docs/security/LICENCES.json`. CI fails when a forbidden licence appears or when that file no
longer matches what is installed.

## What is checked

| Ecosystem | What | Where the licence comes from |
|---|---|---|
| `python` | every package pinned in `supracloud-jarvis/ira/requirements.lock` (167) | the installed package's metadata: `License-Expression`, then a recognisable `License` field, then the licence classifiers |
| `npm-dashboard` | the dashboard's production dependency tree (`npm ls --omit=dev --all`, 24 packages) | each package's own `package.json` (`license`, or the old `licenses` field) |
| `npm-mobile` | the phone app's production dependency tree (803 packages) | the same |

Development-only packages (test runners, type checkers, linters) are left out: they do not ship.

## Policy

Allowed: permissive licences, namely MIT (and MIT-0, MIT-CMU), BSD (any clause count), 0BSD,
Apache-2.0, ISC, PSF-2.0, Python-2.0, CNRI-Python, Zlib, BSL-1.0, CC0-1.0, Unlicense,
BlueOak-1.0.0 and HPND. MPL-2.0 is allowed too: its copyleft applies only to the files
themselves, and we do not change them. CC-BY-3.0 and CC-BY-4.0 are allowed as attribution
licences (caniuse-lite's browser data uses CC-BY-4.0).

Forbidden unless there is a reviewed exception:

- GPL and AGPL of any version (strong and network copyleft);
- LGPL and other weak copyleft (EPL, EUPL, CDDL and similar), because each use needs a check;
- SSPL, BUSL and other source-available licences;
- anything non-commercial, such as CC-BY-NC;
- a missing or unknown licence, or one we cannot read. No licence means no permission.

SPDX expressions are evaluated: `A OR B` is allowed when either option is allowed, and we take
that option. For example, node-forge is `BSD-3-Clause OR GPL-2.0`, so we use it under BSD.
`A AND B` is allowed only when both are.

## Exceptions

`docs/security/licence_exceptions.json` lists each exception with the ecosystem, the package,
the licence exactly as the inventory records it, and the reason. If the licence changes
upstream, the entry stops matching and the package is checked again. The check also fails when
an exception has no real reason, matches no shipped package, or is not needed. There are eight
exceptions at present:

- **psycopg, psycopg-binary, psycopg-pool, recurring-ical-events, x-wr-timezone** (Python,
  LGPL-3.0). Each is installed from PyPI as a separate, unmodified package and imported at run
  time. None is copied or changed here, and each can be swapped for a modified version, which
  is the case LGPL-3.0 allows (section 4).
- **@img/sharp-libvips-linux-x64 and -linuxmusl-x64** (dashboard, LGPL-3.0-or-later). This is
  libvips, used by Next.js image optimisation through sharp. It is a separate shared library
  that sharp's native addon loads at run time (checked with `ldd`), and it is unmodified and
  replaceable.
- **@segment/loosely-validate-event** (phone app, no licence in `package.json`). The LICENSE
  file in the published package is the MIT text; we checked it. The package comes in through
  Expo's command-line tool.

The LGPL exceptions depend on one condition. If we ever give an image or a build to someone
else, the licence text and notice for these libraries must go with it.

## How to run

```sh
# check (what CI runs, one ecosystem per job, after npm ci)
python3 scripts/licence_check.py --check --only npm-dashboard
python3 scripts/licence_check.py --check --only npm-mobile

# check everything visible here; anything not installed is skipped with a note
python3 scripts/licence_check.py --check

# regenerate after a dependency change (npm ci first in dashboard/ and mobile/)
python3 scripts/licence_check.py --write --only npm-dashboard --only npm-mobile

# the Python section must be read from an interpreter with the lock installed (Python 3.12)
uv venv -p 3.12 /tmp/lockvenv
uv pip install --python /tmp/lockvenv/bin/python --no-deps --require-hashes \
    -r supracloud-jarvis/ira/requirements.lock
/tmp/lockvenv/bin/python scripts/licence_check.py --write --only python
```

The dashboard and mobile jobs in `.github/workflows/test.yml` run the check after `npm ci`. The
backend jobs run `supracloud-jarvis/ira/tests/readiness/test_licence_check.py`, which checks the
classifier, checks that the committed inventory obeys the policy, that its Python section
matches the lock's packages and versions, that every exception is valid, and that the check is
wired into CI. Each of those tests has a negative control.

## What is not covered

- **Watch app (Gradle).** Not covered. The watch build needs the Android SDK and Google's Maven
  repository, and Gradle has no built-in licence report. A Gradle licence plugin would have to
  be added and run in the watch CI job. Until then, the watch's licences are unchecked.
- **The torch lock** (`supracloud-jarvis/ira/requirements-torch.lock`: torch, torchaudio,
  jinja2, markupsafe, mpmath, networkx, setuptools, sympy). The CPU torch index cannot be
  reached from the build container, so we cannot install these here. Their licences from PyPI
  are listed in `supracloud-jarvis/ira/dependency-licences.json`, which
  `supracloud-jarvis/ira/scripts/check_licence_inventory.py` keeps current. That check lists
  each package's licence but does not apply this policy.
- **The API image is not the lock.** `deploy/Dockerfile.api` installs
  `supracloud-jarvis/ira/requirements-test.txt`, which is mostly a subset of the lock. It also
  adds pytest, pytest-asyncio and moto with their dependencies, and pins onnxruntime 1.20.1 and
  tokenizers 0.21.0 rather than the lock's versions. Those extra packages and versions are not
  in this inventory.
- **Native libraries bundled inside packages.** We use the licence the package declares and do
  not look inside wheels or prebuilt binaries. One known case is an open finding: PyAV (`av`,
  declared BSD-3-Clause, installed with faster-whisper for speech-to-text) bundles FFmpeg,
  which reports "LGPL version 3 or later". The wheel also carries `libx264` and `libx265`,
  which are GPL-2.0-or-later upstream. The owner must decide what to do about it.
- **Build tools counted as shipped.** The phone app's production tree includes Expo's
  command-line tooling, which is not bundled into the app. This over-counts, which is the safe
  direction.
- **Attribution.** We do not yet generate a notices file with the licence texts.

This is an engineering check against a written policy. It is not legal advice.
