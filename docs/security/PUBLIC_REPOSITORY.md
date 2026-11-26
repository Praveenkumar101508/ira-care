# The repository is public: what was checked, and what the owner should know

The repository `Praveenkumar101508/ira-care` was made public under the Apache License 2.0 on
26 November 2026. History is never rewritten here, so everything ever pushed, on any branch and
on every pull request, is now public for good. This page records the check made on that day,
at `main` commit `722c4dd`, what it found, and what only the owner can do.

No credential value appears on this page. Findings name the file, line, rule and kind of value.

## Summary

- **No real credential was found** in the tracked files or anywhere in history, on any ref GitHub
  serves. Every finding is a documented placeholder, a test fixture, a digest, or a local test
  database password (table below).
- **No raw dataset content is tracked or in history.** The only dataset-derived content is the
  documented 237 KB PPG-DaLiA excerpt (CC BY 4.0), small feature rows derived from PPG-DaLiA for a
  test, and trained model weights.
- **The SisFall-trained v1 fall model is in public history.** It was removed from the tree in #99
  and is in no build, but the file can be retrieved from older commits. See "In public history".
- **Personal data:** no personal email address or phone number of a real person was found. The
  founders' names appear, as intended.
- **New guards:** `.gitignore` now ignores dataset folders and archives, data and weight files,
  environment files and private-paper folders; `scripts/ci/check_staged_files.py` refuses them
  (and credential shapes, and oversize files) before a commit; and
  `supracloud-jarvis/ira/tests/readiness/test_public_repository.py` fails CI if a tracked file
  breaks those rules.

## What was checked, and how

| Check | Tool and scope | Result |
|---|---|---|
| Secrets in `main`'s history | gitleaks 8.24.3, pinned by SHA-256 in `scripts/ci/gitleaks.sh`, with `.gitleaks.toml` and `.gitleaksignore` (`--log-opts="HEAD"`, as CI runs it) | 123 commits scanned, no leaks |
| Secrets on every public ref | The same gitleaks over a mirror clone of GitHub: 121 refs (branches, `refs/pull/*/head`, `refs/pull/*/merge`), 389 commits (`--log-opts="--all"`) | 2 findings, both the documented sealing-key placeholder, under the commit id it had on #11's branch (`c6aea74`). Added to `.gitleaksignore`; the all-refs scan is now clean |
| High-entropy strings in tracked files | detect-secrets 1.5.0, `detect-secrets scan` over the working tree | 1,111 candidate lines in 165 files, every one reviewed (table below); none is a credential |
| Values once committed to the parent project | `supracloud-jarvis/ira/core/runtime/published_credentials.py` holds scrypt digests of six credentials once committed to the parent IRA project. Every value assigned to those six names anywhere in this repository's history (49 distinct values) was digested and compared | 0 matches. This repository's history starts at `abbb7c4` (21 November 2026) and does not contain them |
| Credential shapes, data and size rules | `python3 scripts/ci/check_staged_files.py --all` | 2,009 tracked files, nothing found |
| Personal data | Every unique blob in every ref (3,684 blobs) searched for personal-provider email addresses (Gmail, Outlook, iCloud and others), UK and Indian mobile numbers, chat webhook URLs, private host names and private IP addresses; tracked tree searched for every email address | See "Personal data" |
| Large and binary files | Every tracked file over 1 MB, and every binary or data file by type; every blob ever committed with a data, archive or weights suffix | See "Data and media" |

### Secret-scan findings, classified

| Where | Rule | What it is | Class |
|---|---|---|---|
| 14 fingerprints in `.gitleaksignore` (demo scripts, `.demo/demo.local.json`, `scripts/soak_24h.py`, `scripts/simulate_patient.py`, `tests/tenancy/_care_fixtures.py`) | gitleaks `generic-api-key` | The base64 spelling of the simulator and test-suite sealing keys, and two device tokens of a local demo database that was reseeded. The scripts now generate every key and password on the machine that runs them | Known placeholder, documented |
| `c6aea74` (`refs/pull/11/head`): the same two lines of `scripts/simulate_patient.py` and `_care_fixtures.py` | gitleaks `generic-api-key` | Byte-for-byte the placeholder accepted at `35fb6e4`; both decode to the documented test strings | Known placeholder, now documented |
| 845 lines: `docs/ai/**/*.json`, `docs/evidence/**`, `docs/security/sbom/*`, `supracloud-jarvis/postgres/0*_model*_pins.sql`, `core/models/artefacts.py`, `scripts/supply_chain.py` and others | detect-secrets hex entropy | SHA-256, SHA-1 and MD5 digests: file hashes, dataset archive hashes, model revisions, commit ids, SBOM hashes. `core/runtime/published_credentials.py` holds scrypt digests of retired values, by design | False positive |
| 222 lines, almost all under `supracloud-jarvis/ira/tests/` | detect-secrets secret keyword | Test fixture passwords and keys (readable phrases such as "a long enough passphrase 1"), `change-me` placeholders, environment variable names, `api_key="ollama"` (Ollama ignores the key) | False positive |
| 33 lines: `.github/workflows/test.yml`, `scripts/demo/up.sh`, `.env.test.example`, rollback test scripts, tests | detect-secrets basic auth | Local and CI test database URLs (`ira_app:ira_app@localhost`, `postgres:postgres@localhost`), redaction examples in comments | False positive: local test databases only |
| `care/devices.py`, `security/mfa_recovery.py` | detect-secrets base64 entropy | Alphabets used to generate codes | False positive |
| `watch/app/src/test/resources/device_policy_instruction.json` | detect-secrets base64 entropy | A public key and signatures made by a fixed test key (`scripts/os_device_policy_fixture.py`); no private key is tracked | False positive |
| `tests/runtime/test_log_redaction.py` (lines 100 and 145) | detect-secrets base64, private key | A synthetic JWT assembled from parts, and a truncated fake RSA block, both proving the log redactor hides them | False positive (test fixture) |
| `tests/care/test_care_delivery_rules.py:16` | detect-secrets Twilio | An account id made of one repeated digit, a fictional number (`+447700900123`) and a fixed fake auth token | False positive (test fixture) |

No row is a real credential, so nothing needs revoking because of this check.

### Personal data

- **Email addresses:** outside test fixtures (`example.invalid`, `x@y.com` and similar) the only
  addresses are those of third-party authors in licence notices and SBOMs
  (`docs/legal/notices/`, `docs/legal/copyright_notices.json`, `docs/security/sbom/`), copied
  from the packages' own public metadata. Commit authors and committers use only
  `users.noreply.github.com` and `noreply@github.com`.
- **Phone numbers:** every UK mobile number in history is in Ofcom's range for drama
  (07700 900000 to 900999). Other digit runs that look like numbers are hashes and test nonces.
- **Addresses:** the demo's address ("Flat 3, Mill House, Lowbridge, LB1 4QT") is fictional.
- **Dataset permission drafts** (`docs/legal/dataset_permission_requests/`) name the datasets'
  authors as their papers do and leave the founders' reply address as `[contact email]`. They
  are public now, as is the fact that they have not been sent.
- **Hosts:** only placeholders (`*.internal`, `tailnet-name.ts.net`, `demo.local`), the Android
  emulator's `10.0.2.2`, and private addresses inside tests. No chat webhook URL anywhere.

## Data and media

Every tracked file over 1 MB, and every tracked binary or data file. All are fine to be public.

| File(s) | Size | What it is | Provenance and licence |
|---|---|---|---|
| `docs/film/ira_care.mp4` | 18.3 MB | The product film | Our own recording (`docs/film/RECORDING.md`); voice by Piper `en_GB-cori-high`, trained on public-domain LibriVox recordings |
| `docs/demo/demo.mp4`, `docs/demo/*.png` (12) | 0.3 MB; small | Demo recording and screenshots | Our own, from the demo with fictional people |
| `docs/hardware/turntable.mp4`, `docs/hardware/renders/*.png` (8, 1.3 to 2.1 MB), `docs/hardware/blueprint/*` (PNG, PDF) | up to 2.1 MB | Renders and drawings of the watch | Our own Blender model (`docs/hardware/ira_watch.blend`, `ira_watch.glb`) |
| `docs/hardware/viewer.html`, `viewer_blueprint.html` | 2.4 MB each | Self-contained 3D viewers with the watch model inlined | Our own |
| `docs/screens/*.png` (68; three over 1 MB) | up to 1.3 MB | Screenshots of our own apps | Our own |
| `mobile/assets/*.png`, `watch/app/src/main/res/**.png` | small | App icons | Our own |
| `watch/app/src/main/assets/fall_model.tflite` | 18 KB | Fall model v3, shipped | Our own weights, trained on BITS-2 and UCI HAR (both CC BY 4.0) (`docs/ai/MODEL_WEIGHTS.md`) |
| `docs/ai/fall/fall_model_v2.tflite` | 18 KB | Fall model v2, evaluated, not shipped | Our own weights, BITS-2 and UCI HAR (`docs/ai/fall/MODEL_CARD_V2.md`) |
| `supracloud-jarvis/ira/care/models/activity_model.json` and its watch copy | 242 KB each | Activity model weights | Our own, trained on WISDM (CC BY 4.0); weights only, no recordings |
| `supracloud-jarvis/ira/care/models/heart_selector.json` | 467 KB | Heart candidate selector (gradient-boosted trees) | Our own, trained on PPG-DaLiA features (CC BY 4.0) |
| `supracloud-jarvis/ira/care/research_samples/ppg_dalia_s1_excerpt.json` | 237 KB | A short excerpt of PPG-DaLiA subject S1 | PPG-DaLiA, CC BY 4.0, documented in `docs/DATASETS.md`; the only raw dataset content tracked |
| `supracloud-jarvis/ira/tests/care/fixtures/heart_selector_rows.json` | 22 KB | 56 rows of training features and the trainer's probabilities, so the evaluator is checked | Derived from PPG-DaLiA (CC BY 4.0); features, not recordings |
| `watch/app/src/test/resources/activity_parity.json` | 329 KB | Parity cases for the watch's port of the activity model | Synthetic signals (`scripts/training/activity_watch_fixture.py`) |
| `docs/ai/conversation/dataset/*.jsonl`, `scripts/training/classifier_data.jsonl`, `supracloud-jarvis/ira/tests/care/eval/turns.jsonl` | up to 203 KB | Conversation and classifier examples | Written for this repository from templates; no third-party content (`MANIFEST.json`) |
| `docs/evidence/training/classifier_heads.json` | 112 KB | Classifier head weights over a public encoder | Our own, trained on the synthetic examples above |
| `docs/cloud/evidence/DATABASE_TENANCY_CLASSIFICATION.json`, `docs/security/sbom/*`, `docs/security/LICENCES.json`, `mobile/package-lock.json` | up to 553 KB | Evidence, SBOMs and lockfiles | Generated from this repository |
| `watch/gradle/wrapper/gradle-wrapper.jar` | 44 KB | Gradle wrapper | Gradle, Apache 2.0 |

**Datasets confirmed absent.** No file from KFall, FallAllD, SisFall, UMAFall, WEDA-FALL, UP-Fall,
GalaxyPPG, PPG-DaLiA (other than the excerpt above), WISDM or BITS-2 is tracked, and none has ever
been committed on any ref: no blob in history has a data or archive suffix, and the only
number-heavy text blobs in history are the six model, excerpt and fixture files listed above.

**Training logs** (`docs/evidence/training/`): metrics, hashes and configuration only. No account
names, tokens or local paths; the one Kaggle model address is a placeholder (`owner/...`).

## In public history: what the owner should know

These cannot be removed without rewriting history, which this project does not do. They are
recorded so the owner can decide.

1. **The SisFall-trained v1 fall model** (`fall_model.tflite`, 17,864 bytes, SHA-256
   `8182ea37154ab52e1d9383b3de42add2a2bb902cafb38daa6056f767d84ae6cb`). Added in `d6ab9d6`
   (24 November 2026) at `watch/app/src/main/assets/fall_model.tflite` and
   `docs/evidence/training/ira_fall_model.tflite`; moved to the debug assets in `08766d2` (#66);
   removed from the tree in `235bab1` (#99). It is on `main`'s history and in pull request refs,
   so anyone can retrieve it. SisFall's original terms are not confirmed (`docs/DATASETS.md`).
   The weights are 6,705 numbers and contain no recordings, but they are derived from SisFall.
   If the SisFall authors ask for removal, the options are a history rewrite (against this
   project's rule) or a request to GitHub Support, who can also remove cached pull request
   refs. `test_public_repository.py` fails if the file is ever tracked again under any name,
   and `FallModelReleaseTest` refuses it in any build.
2. **Demo placeholders** (the 14 fingerprints in `.gitleaksignore`, plus the two now added):
   fixed local sealing keys and two device tokens of a reseeded local database. They protected
   nothing outside one developer machine and are no longer used.
3. **Removed `.demo/` files** (`demo.local.json`, `scenarios.log`, `soak_report.json`): seeded
   logins and logs of a local demo database, since reseeded.
4. **Unmerged branches are public too.** `docs/business-research`, `docs/founder-actions` and
   pull request #116 hold business research (market evidence, competitor prices, projection
   assumptions, interview guides), a compute-resources note and a signing-key procedure. No
   secrets or personal data were found in them, but they are readable now whether or not they
   are merged.
5. **The parent project's credentials.** `published_credentials.py` says that six credentials
   were committed to the parent IRA project's `.env.example` in July 2026. They are not in this
   repository (checked above), but the module's text tells readers they exist elsewhere, and the
   register it cites (`docs/cloud/SECRET_ROTATION_REGISTER.md`) is not in this repository. Keep
   the parent project private and confirm all six have been rotated.

## Changes made with this check

- `.gitignore`: a commented block for dataset folders (`datasets/`, `raw_data/`, `data/raw/`),
  archives, array and tabular data, recordings, model weights (with the two committed fall
  models allow-listed), environment files (keeping every `*.example`), and private-paper
  folders (`private/`, `confidential/`, `investor/`, `investors/`, with the product's
  `editions/private/` source folder allow-listed). No tracked file became ignored.
- `scripts/ci/check_staged_files.py`: refuses staged files that match those patterns, files over
  1 MB that are not on its size allow-list, and credential shapes (known token formats, private
  key blocks, chat webhooks, and a high-entropy value assigned to a key, secret, token or password
  name). It prints the path, line and rule, never the value. `--all` checks every tracked file.
  To run it before every commit on your machine:

  ```sh
  printf '#!/bin/sh\nexec python3 scripts/ci/check_staged_files.py\n' > .git/hooks/pre-commit
  chmod +x .git/hooks/pre-commit
  ```

- `supracloud-jarvis/ira/tests/readiness/test_public_repository.py` and
  `test_check_staged_files.py`: run in CI with the readiness suite, each rule with a negative
  control.
- `.gitleaksignore`: the two pull-request fingerprints above.
- `docs/DATASETS.md`, `docs/ai/fall/CROSS_DATASET_WEDA.md`: the WEDA-FALL provenance caveat
  (below).

## WEDA-FALL

The owner reports that a separate browser review of WEDA-FALL found no clear licence; the clone
used for F4.f (commit `9bf15151131644e26403eadd11ecd7949e917740`) contains an ODC-By 1.0 `LICENSE`
and README statement added by the author on 20 November 2026. Since 26 November 2026 the F4.f
results are kept as historical evidence only; nothing further, commercial training or
evaluation, uses WEDA-FALL until the authors confirm permission, and its data is not
redistributed. `docs/DATASETS.md` records commercial use as `unclear`.

## Recommended owner actions

1. **Turn on GitHub's free protections for public repositories**, in the repository's
   Settings, then Code security (also called "Advanced Security" or "Code security and
   analysis"): **Secret scanning** and **Push protection** (blocks a push that contains a
   known token type), **Dependabot alerts** and **Dependabot security updates**, and
   **Private vulnerability reporting**. Whether they are on could not be read from here.
2. **Add a reporting route to `SECURITY.md`** once private vulnerability reporting is on; the
   file describes the threat model but not how to report a problem.
3. **Decide on the SisFall v1 weights in history** (item 1 above), ideally together with the
   SisFall confirmation request (`docs/legal/SISFALL_PERMISSION_REQUEST_2026-11-26.md`, sent 26 November 2026).
4. **Confirm WEDA-FALL permission with its authors** before any further use.
5. **Confirm the parent project stays private** and its six published credentials are rotated.
6. **Check the licence statement of the conversation dataset.**
   `docs/ai/conversation/dataset/MANIFEST.json` says "IRA CARE proprietary", but the repository,
   which includes that dataset, is now Apache 2.0. Either change the statement or name the
   dataset as an exception in `NOTICE`.
7. **Install the pre-commit check** above on each machine that commits.
