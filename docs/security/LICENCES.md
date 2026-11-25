# Licence check of shipped dependencies

Requirement K.d. `scripts/licence_check.py` lists the licence of every third-party package we
ship, checks each one against the policy below, and keeps the result in
`docs/security/LICENCES.json`. CI fails when a forbidden licence appears or when that file no
longer matches what is installed.

## What is checked

Two scripts, one policy. `scripts/licence_check.py` reads what the builds declare;
`scripts/supply_chain.py` looks inside what the builds produce.

| Ecosystem | What | Where the licence comes from |
|---|---|---|
| `python` | every package pinned in `supracloud-jarvis/ira/requirements.lock` (166) | the installed package's metadata: `License-Expression`, then a recognisable `License` field, then the licence classifiers |
| `npm-dashboard` | the dashboard's production dependency tree (`npm ls --omit=dev --all`, 24 packages) | each package's own `package.json` (`license`, or the old `licenses` field) |
| `npm-mobile` | the phone app's production dependency tree (803 packages) | the same |
| `gradle-watch` | the watch's release runtime classpath, both flavours (`wearosReleaseRuntimeClasspath`, `aospReleaseRuntimeClasspath`), as Gradle resolves it (109 modules: 108 in the Wear OS build, 99 in the AOSP build) | each module's POM in the Gradle cache, following `<parent>` POMs when a POM names no licence. Six modules with POM packaging (bills of materials, and Kotlin Multiplatform roots whose code is in the `-jvm` module beside them) carry no code and are listed apart |

Development-only packages (test runners, type checkers, linters, Gradle's test configurations)
are left out: they do not ship.

### What the built artefacts contain

Declarations are not the whole story, so the artefacts themselves are read:

- **The watch's release APKs** (`supply_chain.py apk`). Every native library in the APK is
  traced to the module on the classpath whose AAR carries it, and every library version file
  androidx and kotlinx leave in the APK (`META-INF/*.version`, 58 in the Wear OS build, 56 in
  the AOSP build) must name a module the classpath resolved. The model files are listed with
  their hashes, and the release fall model must be v3. Both builds carry two native libraries
  for four ABIs: `libtensorflowlite_jni.so` (from `org.tensorflow:tensorflow-lite` 2.16.1,
  Apache-2.0) and `libandroidx.graphics.path.so` (from `androidx.graphics:graphics-path` 1.0.1,
  Apache-2.0). Result: `docs/security/sbom/watch-apk-contents.json`.
- **The container images** (`supply_chain.py image`). The image's file system is read in one
  pass (`docker export` of a container that is created, never started): the Debian or Alpine
  package database, every Python distribution's metadata and file list, every `package.json`
  under `node_modules`, every model file, and every ELF file's dynamic section (`DT_NEEDED`,
  read by the script itself). That gives, for each operating-system package, which of our
  components load a library from it directly. Results: `docs/security/sbom/<image>.inventory.json`.

Operating-system packages are recorded with the licences their copyright files name, but they
are not judged by the policy. A base image ships GPL programs (bash, coreutils, tar) beside our
code; that is aggregation, and Debian's copyright files name GPL for 70 of the API image's 87
packages because they cover whole source packages (programs, tests, build scripts). What is
judged is linking: an OS library that one of our components (a Python package, an npm package,
the base image's CPython or Node.js) loads directly must have its own licence reviewed in
`docs/security/native_libraries.json` and must pass the policy. Native libraries bundled inside
Python wheels (`site-packages/*.libs`) are judged the same way, keyed by library name.

| Image (snapshot, 25 November 2026) | Packages | Linked OS libraries | Bundled in wheels | Models |
|---|---|---|---|---|
| API (`deploy/Dockerfile.api`) | 128 Python, 87 Debian | 13: libc6 and libcrypt1 (LGPL-2.1-or-later, excepted), libgcc-s1 and libstdc++6 (GCC runtime exception), OpenSSL, zlib, libffi, liblzma, bzip2, SQLite, ncurses, libuuid | 37: Pillow 18, psycopg-binary 14, numpy 3, ctranslate2 2 | the MiniLM encoder, the classifier heads, the heart selector, the activity model, and Silero VAD inside the faster-whisper wheel |
| Dashboard (`deploy/Dockerfile.dashboard`) | 23 npm in the image (Next.js bundles the rest of the 24-package production tree into `.next`), 19 Alpine | 3: musl, libgcc, libstdc++ | none | none |
| Backup (`deploy/Dockerfile.backup`) | 45 Alpine | 16, linked by PostgreSQL from the base image | none | none |

The snapshots were built in this container, which cannot reach the Debian and Alpine package
mirrors, so the OS package steps (`curl`, `postgresql-client`, `openssl`) are missing from
them; each inventory says so in its `note`. CI builds the real images and runs the same scan
with `--check` on every image build: a forbidden package, an unreviewed bundled or linked
library, or a linked GPL library fails the job. The fresh inventories and full syft CycloneDX
SBOMs of the real images are kept as the `image-sboms` artifact of that job (30 days). The
Python packages in the API image are not fully pinned (`requirements-test.txt` has ranges), so
CI reports a drift from the snapshot as a note rather than a failure; the policy is what it
enforces.

### SBOMs and notices

`supply_chain.py render` writes, from the committed inventories alone (no network, no build):

- a CycloneDX 1.5 SBOM per distributed artefact in `docs/security/sbom/` (`watch-wearos`,
  `watch-aosp`, `phone-app`, `dashboard-image`, `api-image`, `backup-image`), with each
  component's licence, verdict, location and, for models and native libraries, SHA-256. The
  output is deterministic: no timestamps or serial numbers;
- a notices file per artefact in `docs/legal/notices/`, with every package, the copyright lines
  found in its own files (`docs/legal/copyright_notices.json`, collected by
  `supply_chain.py copyrights` from the installed packages), the models with their training
  data's required citations, and the full text of every licence named (SPDX texts in
  `docs/legal/licences/`); and the index `docs/legal/THIRD_PARTY_NOTICES.md`, which says which
  artefact must carry which file.

Copyright lines are taken from the licence files a package ships. Where none has one, the
author from the package metadata, or the publisher from the POM, is given and labelled as such:
that is attribution, not a copyright notice.

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

For native code, a few more permissive licences are allowed: FTL, IJG, libtiff, libpng-2.0,
PostgreSQL, OLDAP-2.8, BSD-Attribution-HPND-disclaimer, curl, blessing (SQLite), bzip2-1.0.6,
MIT-open-group, ICU and Unicode-3.0. GCC's runtime libraries (libgcc, libstdc++, libgfortran,
libgomp) are GPL-3.0-or-later with the GCC Runtime Library Exception 3.1, which exists so that
any program can ship with them: `GPL-3.0-or-later WITH GCC-exception-3.1` is allowed. Other
`WITH` clauses (Classpath, say) are not, and a GPL-2.0 id with the GCC exception is not either.

SPDX expressions are evaluated: `A OR B` is allowed when either option is allowed, and we take
that option. For example, node-forge is `BSD-3-Clause OR GPL-2.0`, so we use it under BSD.
`A AND B` is allowed only when both are.

## Exceptions

`docs/security/licence_exceptions.json` lists each exception with the ecosystem, the package,
the licence exactly as the inventory records it, and the reason. If the licence changes
upstream, the entry stops matching and the package is checked again. The check also fails when
an exception has no real reason, matches no shipped package, or is not needed. There are
fifteen exceptions at present:

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
- **@img/sharp-wasm32** (dashboard image only, Apache-2.0 AND LGPL-3.0-or-later AND MIT).
  Next.js's file tracing copies its JavaScript loader into the image (180 KB, no `.wasm`
  file); on Linux sharp uses the native build.
- **huggingface-hub** (API image only, declared "Apache" with no version). The LICENSE file in
  the installed wheel is the Apache License 2.0 text.
- **libquadmath** (bundled by numpy) and **libkeyutils** (bundled by psycopg-binary), both
  LGPL-2.1-or-later: separate shared libraries, loaded at run time, unmodified and replaceable.
- **libselinux** (bundled by psycopg-binary): public domain, which has no SPDX id.
- **libc6** and **libcrypt1** (Debian, LGPL-2.1-or-later): the operating system's C library and
  password-hashing library, which every compiled program links; Debian ships their notices in
  the image.

The LGPL exceptions depend on one condition. If we ever give an image or a build to someone
else, the licence text and notice for these libraries must go with it. The notices files in
`docs/legal/notices/` carry them; they are not yet copied into the images or the apps (below).

## How to run

```sh
# check (what CI runs, one ecosystem per job, after npm ci or the watch build)
python3 scripts/licence_check.py --check --only npm-dashboard
python3 scripts/licence_check.py --check --only npm-mobile
python3 scripts/licence_check.py --check --only gradle-watch   # needs the Android SDK
python3 scripts/supply_chain.py apk --check                    # after assemble*Release
python3 scripts/supply_chain.py image api ira-care/api:ci --base python:3.12-slim --check

# check everything visible here; anything not installed is skipped with a note
python3 scripts/licence_check.py --check
python3 scripts/supply_chain.py render --check                 # SBOMs and notices up to date

# regenerate after a dependency change (npm ci first in dashboard/ and mobile/)
python3 scripts/licence_check.py --write --only npm-dashboard --only npm-mobile
python3 scripts/licence_check.py --write --only gradle-watch
python3 scripts/supply_chain.py apk --write
python3 scripts/supply_chain.py image api <image> --base python:3.12-slim --note "<how built>" --write
python3 scripts/supply_chain.py copyrights --only npm-dashboard --only npm-mobile --only gradle-watch
python3 scripts/supply_chain.py licence-texts                  # any new licence's SPDX text
python3 scripts/supply_chain.py render --write

# the Python section must be read from an interpreter with the lock installed (Python 3.12)
uv venv -p 3.12 /tmp/lockvenv
uv pip install --python /tmp/lockvenv/bin/python --no-deps --require-hashes \
    -r supracloud-jarvis/ira/requirements.lock
/tmp/lockvenv/bin/python scripts/licence_check.py --write --only python
/tmp/lockvenv/bin/python scripts/supply_chain.py copyrights --only python
```

Where Maven Central has to be reached through a mirror, pass Gradle an init script in
`LICENCE_CHECK_GRADLE_ARGS` (for example `-I /path/to/mirror.init.gradle`).

In `.github/workflows/test.yml`: the dashboard and mobile jobs run the npm checks after
`npm ci`; the watch job builds both release APKs and runs the `gradle-watch` check and
`supply_chain.py apk --check`; the images job scans the three images it builds and keeps their
inventories and syft SBOMs as an artifact. The backend jobs run
`supracloud-jarvis/ira/tests/readiness/test_licence_check.py` and `test_supply_chain.py`, which
check the classifier, the committed inventories and snapshots against the policy and the lock,
every exception, that the SBOMs and notices are exactly what the inventories render, that the
notices list every distributed package, and the CI wiring. Each of those tests has a negative
control.

## What is not covered

- **The phone app's native code.** The npm tree is checked, but the Android build of the phone
  app (React Native, Expo modules and their Gradle and CocoaPods dependencies, and the `.so`
  files in its APK) is not. Its release build needs `jitpack.io`, which this container cannot
  reach, so the classpath cannot be resolved here; the `gradle-watch` mechanism could be
  pointed at `mobile/android` in the CI job that builds the APK. The iOS build is not covered
  either (no macOS).
- **The torch lock** (`supracloud-jarvis/ira/requirements-torch.lock`: torch, torchaudio,
  jinja2, markupsafe, mpmath, networkx, setuptools, sympy). `download.pytorch.org` cannot be
  reached from this container (checked again on 25 November 2026), so these cannot be installed
  or scanned here, and the native libraries inside the torch wheels (which bundle large
  third-party libraries) are not inspected. Their licences from PyPI are listed in
  `supracloud-jarvis/ira/dependency-licences.json` (`check_licence_inventory.py`), without this
  policy. No image built in CI contains torch.
- **`Dockerfile.portable`** (the full foundation server, `supracloud-jarvis/`) is not built in CI
  and is not scanned.
- **Third-party images run unchanged** by `deploy/docker-compose.yml` (Ollama, which serves the
  models; Caddy; PostgreSQL with pgvector; Redis) are pulled from their publishers at
  installation and not scanned. The model weights Ollama pulls (Gemma 4, Apache-2.0) are in the
  notices index.
- **The operating system** (`os/`): no OS image is built in this repository (the AOSP build
  needs a dedicated host), so its third-party components cannot be inventoried.
- **Training dependencies** (`scripts/training/`, Kaggle notebooks): development only; the
  trained weights are what ships, and they are covered with their training data.
- **The image snapshots lack the OS package steps** (above). CI scans the real images.
- **Development tools that ship anyway.** The API image installs `requirements-test.txt`, so
  pytest, pytest-asyncio, moto, responses, Werkzeug, boto3 and their dependencies are in the
  production image. The dashboard image contains TypeScript (copied by Next.js's file tracing).
  They are inventoried and pass the policy, but they need not ship.
- **Licence texts of libraries bundled without them.** psycopg-binary's wheel bundles fourteen
  libraries (libpq, OpenSSL, MIT Kerberos, OpenLDAP, Cyrus SASL and others) and states only its
  own LGPL-3.0 licence; ctranslate2's bundles GCC's libgomp without a notice. Their licences in
  `native_libraries.json` are the upstream projects', and our notices carry the texts.
- **The notices do not yet travel with the artefacts.** The files exist, but no app shows them
  and no image contains them yet (`docs/legal/THIRD_PARTY_NOTICES.md` says where each must go).
- **Copyright lines are found by a heuristic** over licence files; where a package has none,
  its author or publisher is given instead and labelled. They have not been reviewed by hand.

## Closed finding: GPL libraries linked by the base image's Python (25 November 2026)

**What it was.** The first scan of the API image found that the CPython of `python:3.12-slim`
links three libraries that do not pass the policy, through standard-library modules:
`readline` links GNU readline (`libreadline8t64`, GPL-3.0-or-later), `_gdbm` links GDBM
(`libgdbm6t64`, GPL-3.0-or-later) and `_dbm` links Berkeley DB 5.3 (`libdb5.3t64`, Sleepycat).
The IRA server imports none of them: they serve an interactive prompt and two on-disk
key-value stores.

**What changed (D-084).** `deploy/Dockerfile.api` deletes those three extension modules and
fails the build if one is still importable. The libraries stay in the image as Debian packages
(bash uses readline), which is aggregation; nothing of ours links them. The image scan
(`linked_os_libraries`) no longer lists them, and `test_supply_chain.py` fails if the Dockerfile
stops removing them. `Dockerfile.portable` is unchanged (not built in CI, above).

## Closed finding: PyAV removed (25 November 2026)

**What it was.** faster-whisper, the speech recogniser, declares PyAV (`av`) as a hard
dependency. PyAV's wheels bundle FFmpeg, including `libx264` and `libx265`, which are
GPL-2.0-or-later. The owner decided PyAV must not ship (D-083).

**What changed.**

- IRA decodes audio itself: `supracloud-jarvis/ira/voice/audio_decode.py` reads WAV (PCM at 8,
  16, 24 or 32 bits, or float at 32 or 64 bits, any channel count, 4 kHz to 384 kHz) with the
  standard library and numpy, and converts it to 16 kHz mono. Every other format is refused by
  name. No client in this repository sends anything but WAV (`docs/ai/speech/RESULTS.md`).
- faster-whisper is installed with `pip install --no-deps --require-hashes -r
  requirements-nodeps.txt`; its other dependencies are pinned in `requirements.txt` and
  `requirements-test.txt`. The lock is generated with an override that drops `av` (see its
  header). `voice/whisper_engine.py` puts IRA's decoder where faster-whisper looks for its own,
  so `import av` is never attempted.
- `deploy/Dockerfile.api` and `Dockerfile.portable` fail to build if `av` is present.
  `tests/architecture/test_no_pyav.py` fails if `av` appears in any manifest or the lock, if
  any installer adds faster-whisper without `--no-deps`, if any file imports `av`, or if
  anything but `voice/whisper_engine.py` imports faster-whisper.

**Evidence.**

| Where | `import av` | `av`, `av.libs` | Shared libraries named or containing `libx264`, `libx265` or `libavcodec` |
|---|---|---|---|
| Previous lock, installed in a Python 3.12 venv (control) | works (18.1.0) | present | 7, all in `av.libs` (`libx264`, `libx265`, `libavcodec`, `libavformat`, `libavfilter`, `libavutil`, `libavdevice`) |
| New lock, fresh Python 3.12 venv, `uv pip install --no-deps --require-hashes -r requirements.lock` (166 packages) | `ModuleNotFoundError` | absent | 0 of 325 |
| API image built from `deploy/Dockerfile.api` (`scripts/staging/sandbox_build.sh api`) | `ModuleNotFoundError`; `pip show av` finds nothing | absent | 0 of 587 in the whole image; no `ffmpeg` binary |

The content search looked for the strings `x264 - core`, `x265 (build` and `libavcodec` in every
`.so` file; on the control venv it finds the seven libraries above. In both the fresh venv and
the image, `voice.stt` loads faster-whisper and transcribed a LibriSpeech utterance (converted
to WAV) correctly with PyAV absent.

What this costs: uploads in MP3, M4A/AAC, Ogg, Opus, WebM, FLAC and video files are no longer
transcribed. They are refused with a message naming the format and asking for WAV. A future
browser client must record WAV (PCM) itself.

`pip check` reports "faster-whisper 1.2.1 requires av, which is not installed". That is this
decision, not a broken install.

This is an engineering check against a written policy. It is not legal advice.
