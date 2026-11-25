# IRA CARE completion register

The single place that says, for every requirement in the owner's master directive (25 November
2026), what exists, what proves it, and what is still missing. The detailed safety findings and
their negative controls are in `REMEDIATION.md`; this register points to them.

Started 25 November 2026. **Keep it current:** a change to a row's status needs its evidence, and the
matching atomic requirements in `requirements.json`, in the same pull request.

## Status words

These are the only words used, and each means exactly this:

| Status | Meaning |
|---|---|
| **not started** | No work in this programme yet. |
| **pre-existing** | Built before this programme (rounds 1 to 4). Not re-verified here unless stated. |
| **in progress** | A branch or PR exists; not merged. |
| **implemented** | Merged code. |
| **unit tested** | Merged, with automated tests that run without a database or device. |
| **integration tested** | Merged, with tests against a real PostgreSQL. |
| **browser tested** | Exercised in a real browser. |
| **emulator tested** | Run on an Android emulator or Cuttlefish. |
| **trained** | A model was trained, with a dated run. |
| **evaluated** | A model was measured on held-out data, with numbers and the commit they came from. |
| **device tested** | Run on a physical watch or phone. |
| **needs external** | Blocked by something outside the software: hardware, credentials, a contract, a permission, money, or qualified review. The row says which. |

Nothing is marked done because code or a document exists.

## Completion

Completion is no longer scored from this register's rows. It is measured from
`docs/progress/requirements.json`: a fixed, versioned list of atomic requirements, each with
one register row as its parent, one state and the evidence for it. `scripts/completion_summary.py`
checks the rules (documents and configuration checks never count as acceptance; verification needs
database, browser, emulator, model-evaluation or device evidence; every file it names exists; blocked
items stay in every denominator) and reports seven separate measures. `--snapshot <label>` appends a
dated record to `docs/progress/completion_history.json`, which is append-only.

The numbers below are quoted from one dated snapshot, so they stay true when later pull requests
change the requirements; run the script for the current figures.

At snapshot **main after #66**, `08766d2` (recorded 2026-11-25):

| Measure | Done / total | % |
|---|---|---|
| Hardware-independent software implementation | 121 / 177 | 68.4 % |
| Hardware-independent software acceptance | 104 / 177 | 58.8 % |
| Software verification | 45 / 191 | 23.6 % |
| AI development (accepted, area ai) | 17 / 37 | 45.9 % |
| OS development (accepted, area os, hardware-independent) | 2 / 27 | 7.4 % |
| Hardware integration (accepted, area hardware) | 0 / 14 | 0.0 % |
| External and commercial readiness (accepted, area external) | 0 / 34 | 0.0 % |

104 of 239 requirements are accepted or better; 135 remain, 47 of them blocked by something
outside the software. None is device validated: no physical device has been used.

The history's first entry recomputes the audit baseline (`1f41c5d`, before #44) as a lower bound,
crediting only evidence present there verbatim (for example, software acceptance 32 / 177).
It is not a progress figure: states were never recorded at that commit, and evidence files
edited since are not credited.

## Pull requests in this programme

| PR | Branch | Group | State |
|---|---|---|---|
| [#44](https://github.com/Praveenkumar101508/ira-care/pull/44) | `fix/care-safety-answers` | A: dose answers, safety check, wearer-only talk, logs, migration 112 | merged, CI green |
| [#45](https://github.com/Praveenkumar101508/ira-care/pull/45) | `fix/dashboard-live-person` | B: live patient record, four-state wrist | merged, CI green |
| [#46](https://github.com/Praveenkumar101508/ira-care/pull/46) | `fix/care-delivery-reliability` | A2: outbox, delivery states, signed Twilio callbacks, migration 113 | merged, CI green |
| [#47](https://github.com/Praveenkumar101508/ira-care/pull/47) | `fix/phone-session-and-talk` | C3/C4: sign-out isolation, session renewal | merged, CI green |
| [#48](https://github.com/Praveenkumar101508/ira-care/pull/48) | `fix/watch-speech-and-storage` | D: Help before speech, speech core, encrypted per-wearer storage | merged, CI green |
| [#49](https://github.com/Praveenkumar101508/ira-care/pull/49) | `fix/completion-register` | this register | merged |
| [#50](https://github.com/Praveenkumar101508/ira-care/pull/50) | `fix/completion-register` | register: groups A2 and D merged | merged, CI green |
| [#51](https://github.com/Praveenkumar101508/ira-care/pull/51) | `fix/care-authz-and-outages` | E: person-level authorisation (D-076), outage recovery | merged, CI green |
| [#52](https://github.com/Praveenkumar101508/ira-care/pull/52) | `fix/dashboard-session-renewal` | C4b: dashboard session renewal | merged, CI green |
| [#53](https://github.com/Praveenkumar101508/ira-care/pull/53) | `fix/watch-explicit-unpair` | D7: explicit unpair with warning, migration 114 | merged, CI green |
| [#54](https://github.com/Praveenkumar101508/ira-care/pull/54) | `fix/dashboard-dark-mode` | C5: dark-mode and high-contrast colours | merged, CI green |
| [#55](https://github.com/Praveenkumar101508/ira-care/pull/55) | `fix/log-exception-text` | A5b: exception text out of logs across the backend | merged, CI green |
| [#56](https://github.com/Praveenkumar101508/ira-care/pull/56) | `fix/mobile-talk-streaming` | C1: phone Talk streaming | merged, CI green |
| [#57](https://github.com/Praveenkumar101508/ira-care/pull/57) | `fix/product-capabilities` | F1: what IRA measures and what it does not (D4) | merged, CI green |
| [#58](https://github.com/Praveenkumar101508/ira-care/pull/58) | `fix/ai-heart-ppg-dalia` | F3: IRA Care Heart, evaluated on PPG-DaLiA | merged, CI green |
| [#59](https://github.com/Praveenkumar101508/ira-care/pull/59) | `fix/os-product-layer` | E1/E2/E6: OS product layer and docs | merged, CI green |
| [#60](https://github.com/Praveenkumar101508/ira-care/pull/60) | `fix/watch-home-screen` | D8: watch app as the OS home screen | merged, CI green |
| [#61](https://github.com/Praveenkumar101508/ira-care/pull/61) | `fix/mobile-voice` | C2: phone voice in and out | merged, CI green |

## C. Software safety remediation (the 24 items)

| # | Requirement | Status | Evidence / blocker |
|---|---|---|---|
| C-1 | Conservative medication answers | integration tested | #44 · REMEDIATION A1 |
| C-2 | Every medication write path | integration tested | #44 · A2, A3a–c |
| C-3 | Dose identity, timing, provenance, correction history | integration tested | #44 · A3, A3d, A3e (migration 112) |
| C-4 | No false safety-check confirmation | integration tested | #44 · A4 |
| C-5 | No false fall-countdown cancellation | unit tested | #44 · A4b (JVM tests; CI Gradle build) |
| C-6 | Sensitive log protection | unit tested | #44 · A5 (care); #55 · A5b (rest of the backend) |
| C-7 | Wearer-only conversation | integration tested | #44 · A6 |
| C-8 | Delivery states and verified callbacks | integration tested | #46 · A2-3 |
| C-9 | Dispatch concurrency and retries | integration tested | #46 · A2-1, A2-2 |
| C-10 | Live patient-record refresh | unit tested | #45 · B1 (component tests; not browser tested) |
| C-11 | Unknown and stale wrist status | unit tested | #45 · B2 |
| C-12 | Mobile conversation streaming | unit tested | #56 · C1 (`expo/fetch`, incremental NDJSON). On-device check: **needs external**, a device build |
| C-13 | Native mobile voice | unit tested | #61 · C2 (speech out and in; one utterance; no recording kept). Device check and elderly-speech accuracy: **needs external** (device build, consented recordings); DPIA to cover platform recognisers |
| C-14 | Complete logout isolation | unit tested | #47 · C3 |
| C-15 | Push-token unregister on sign-out | unit tested | #47 · C3 |
| C-16 | Secure automatic session renewal | unit tested | #47 · C4 (phone); #52 · C4b (dashboard) |
| C-17 | Watch speech-recognition threads and lifecycle | unit tested | #48 · D1 (JVM tests; CI Wear OS and AOSP builds) |
| C-18 | Watch text-to-speech lifecycle and time limits | unit tested | #48 · D2 |
| C-19 | Encrypted watch storage | unit tested | #48 · D5 (Keystore hardware backing: **needs external**, device); #68 · D9 (no plain-text fallback for sensitive data; Help still queued, minimised) |
| C-20 | Atomic offline-queue and outbox writes | unit tested | #48 · D5 |
| C-21 | Secure unpairing and re-pairing | integration tested | #48 · D5 (kept per wearer); #53 · D7 (explicit unpair with warning, migration 114). On-device UI check: **needs external**, a watch |
| C-22 | Backend and person-level authorisation review | integration tested | #51 · REMEDIATION E1 (D-076) |
| C-23 | Accessibility and interface consistency | unit tested | Automated accessibility checks from round 4; #54 · C5 (dark-mode and high-contrast colour review, measured). A screen-reader pass on real devices: **needs external** |
| C-24 | Full-system outage and recovery tests | integration tested | #51 · REMEDIATION E2–E5 |

## D. The software platform

| # | Requirement | Status | Evidence / blocker |
|---|---|---|---|
| D1 | Watch app: check-ins, voice and touch, reminders, routines, visits, heart rate, falls, wrist, battery, Help, GPS, offline, outbox, delivery and responder status | pre-existing, partly re-tested | `STATUS.md` rounds 1–4; Help path and storage hardened in #48. **needs external**: physical watch |
| D1b | AOSP application flavour | pre-existing | CI builds both flavours |
| D2 | Phone app, wearer / family / carer experiences, personalisation | pre-existing | Round 4 one-app work. iOS build: **needs external** (macOS, signing). Play and App Store: **needs external** (developer accounts) |
| D3 | Care dashboard: overview, record, visits, alerts, reports, audit, search | pre-existing; live record improved | #45. Care plans as a separate object, staff role screens: **not started** |
| D4 | Product promises reconciled (food-intake check, camera, SpO2, ECG, HRV, sleep, blood pressure, hearing access) | unit tested | #57 · REMEDIATION F1: one capability list; not-available measurements are refused and never shown with a value. New sensors: **needs external** (hardware, validation) |

## E. IRA CARE OS (AOSP)

| # | Requirement | Status | Evidence / blocker |
|---|---|---|---|
| E1 | Distinguish app / launcher / system image / firmware / production OS | unit tested | #59 · `docs/os/ARCHITECTURE.md`: the five layers, what exists (with paths), what is unbuilt, what needs hardware or suppliers; the sensor HAL boundary. Document only; nothing in it is built |
| E2 | Product config, launcher, boot animation, settings, emergency UI, services, Cuttlefish image | unit tested (configuration only); image **needs external** (AOSP build host) | #59 · `os/`: Cuttlefish product makefile, privileged prebuilt app with privapp allowlist (empty: the manifest requests no privileged permission), proposed first-boot grants, framework overlay, boot animation specification, settings and emergency-UI notes, build and launch scripts. Readiness tests `tests/readiness/test_os_product_layer.py` check references, the allowlist against the manifest, and that the build script refuses on low resources and never downloads without the opt-in. **The image is unbuilt and has never booted**: a full AOSP build needs about 400 GB disk and 64 GB RAM (source.android.com) and this container has neither. Several AOSP names are marked uncertain (`docs/os/BUILD.md`). IRA as the home screen: **not started**, needs a HOME intent filter in the watch app's aosp flavour (`os/device/ira/ira_watch_cf/HOME_LAUNCHER.md`). Device-owner policy app (developer options, factory reset behind an administrator): **not started**; app side of the home screen done (#60, REMEDIATION D8) |
| E3 | Sensor HAL interfaces with simulated drivers | pre-existing (contract) | `docs/hardware/SENSOR_HAL.md`; simulated drivers: **not started** |
| E4 | Verified boot, Keystore, signed OTA, rollback, provisioning | not started | Plan in `docs/os/ARCHITECTURE.md` section 3 (#59), saying which links can be shown on Cuttlefish and which cannot. Hardware-backed parts: **needs external** (target board). Signing keys: **needs external** (owner's key-custody decision) |
| E6 | OS architecture document, build instructions, Qualcomm dependency register | unit tested | #59 · `docs/os/ARCHITECTURE.md`, `docs/os/BUILD.md` (every step marked not run in this repository's CI), `docs/os/QUALCOMM_DEPENDENCIES.md` (14 supplier components with availability, licence and owner action). Running the build steps: **needs external** (AOSP build host). Supplier parts: **needs external** (Qualcomm supplier access) |

## F. IRA intelligence (seven components)

| # | Component | Status | Evidence / blocker |
|---|---|---|---|
| F1 | Dataset licence register (PPG-DaLiA, UMAFall, KFall, FallAllD, conversation data, model weights) | unit tested; KFall, FallAllD, SisFall and UMAFall terms: **needs external** (permission or confirmation from the authors) | `fix/dataset-licence-register` · `docs/DATASETS.md` re-verified 2026-11-25 row by row against the original source, or marked not verified with the page to open where the build environment could not reach it; `docs/ai/MODEL_WEIGHTS.md` (every shipped or downloaded model: Gemma 4 E4B, E2B and 12B Apache 2.0, MiniLM encoder Apache 2.0, our own fall model and classifier heads; foundation-only models listed, OmniVoice is CC BY-NC); drafts, not sent, in `docs/legal/dataset_permission_requests/` (KFall, FallAllD, SisFall, UMAFall); test `supracloud-jarvis/ira/tests/readiness/test_dataset_register.py`. Finding: the SisFall copy the shipped watch fall model was trained on (Zenodo 22212281) names no creator, so its CC BY 4.0 label is not the authors' own: the owner must accept that risk or ship the rule detector alone until the authors reply. UMAFall: the owner's CC BY 4.0 and a CC BY-NC repository record disagree; not used |
| F2 | IRA Care Conversation: new versioned dataset, LoRA / QLoRA run, evaluation, promotion gate | pre-existing experiment | 507 synthetic conversations; rule pass 0.94 base against 0.92 fine-tuned on 50 prompts; the 162-turn product evaluation is a different measure (`docs/MODEL_CHOICE.md`). A new run: **not started**; GPU time: **needs external** (Kaggle account session) |
| F3 | IRA Care Heart (PPG-DaLiA, subject-independent) | evaluated | #58 · `docs/ai/heart/MODEL_CARD.md`, `docs/ai/heart/results.json`, `scripts/training/heart_ppg_dalia.py`. Leave-one-subject-out on all 15 PPG-DaLiA subjects (CC BY 4.0): classical spectral estimator with accelerometer suppression and tracking 13.28 ± 5.01 bpm MAE (shipped, numpy only: `supracloud-jarvis/ira/care/heart_estimate.py`, tests `supracloud-jarvis/ira/tests/care/test_heart_estimate.py`); learned candidate selection 7.75 ± 3.23 bpm (training script only); Deep PPG 7.65 bpm as reported, not reproduced. Adults 30.6 ± 9.6 years on an Empatica E4: accuracy on the IRA watch and on older adults: **needs external** (recordings with an ECG reference) |
| F4 | IRA Care Fall (wrist-specific, cross-dataset) | evaluated (v2 candidate, not shipped) | #66 · D-077: the SisFall-trained v1 is research-only (debug builds); release builds and the OS image run the fall rules; `FallModelReleaseTest`. `fix/ai-fall-v2` · v2 trained only on BITS-2 (Zenodo 10013090, the authors' own record, CC BY 4.0, left wrist, 41 volunteers, simulated falls) and UCI HAR (CC BY 4.0), 5-fold subject-independent (`docs/ai/fall/MODEL_CARD_V2.md`, `docs/ai/fall/results_v2.json`, `scripts/training/fall_model_v2.py`). In the watch pipeline on held-out subjects: rule alone sensitivity 0.6171, 52.84 alarms per hour of daily activity; rule + v2 sensitivity 0.6013 (5 of 316 falls vetoed), 10.75 alarms per hour. **Fails the shipping gate** (must not lose sensitivity against the rule), so it is not in the watch assets; shipping it is a decision for the owner and a clinical reviewer. SisFall terms: **needs external** (authors). Older adults, the IRA watch, real falls: **needs external** |
| F5 | IRA Care Safety (deterministic rules, model only more cautious) | integration tested | #44 (A1, A2, A4) |
| F6 | IRA Care Memory (consent, correction, deletion) | integration tested | `fix/care-memory-promises` · REMEDIATION F2: correction kept and read back, forgotten day gone from context, erasure removes memory; check-in notes no longer copied into the general memory store (migration 115 purges old ones). Topic lists need the `topics` consent (110) |
| F7 | IRA Care Activity (IMU) | not started | needs a dataset whose licence allows commercial use |
| F8 | IRA Care Speech (recognition and synthesis; elderly speech evaluation) | watch speech lifecycle unit tested (#48) | Accuracy evaluation on elderly speech: **needs external** (consented recordings) |

## G. Server and deployment

| # | Requirement | Status | Evidence / blocker |
|---|---|---|---|
| G1 | Containers, CPU / GPU profiles, Postgres, Redis, workers, proxy, HTTPS | pre-existing | `deploy/`, `docs/DEPLOY.md`. Container images rebuilt in this programme: **not started** |
| G2 | Observability, outage recovery, encrypted backups, restore | pre-existing (partial) | CI restore round trip. Outage drills: **not started** |
| G3 | Push, SMS, call, email, monitoring-centre adapters | SMS / call / push delivery states integration tested (#46) | Live delivery: **needs external** (provider credentials, a decision to pay). Monitoring centre: **needs external** (contract) |
| G4 | Hosting proposal, cost model, runbooks | pre-existing (partial) | Renting a server: **needs external** (owner's approval to spend) |

## H, I. Hardware and compliance preparation

| # | Requirement | Status | Evidence / blocker |
|---|---|---|---|
| H1–H4 | Sensor evaluation plan, Qualcomm engagement document, ODM specification, EVT / DVT / PVT roadmap | pre-existing (partial) | `docs/hardware/`. Updates: **not started**. All physical work: **needs external** |
| I | Intended purpose, claims register, hazard log, DPIA, pilot protocol | pre-existing drafts | `docs/compliance/`. Updating against current MHRA / ICO guidance: **not started**. Approval: **needs external** (qualified review) |

## J, K, L. Demonstration, final verification, evidence

| # | Requirement | Status | Evidence / blocker |
|---|---|---|---|
| J | Integrated live demonstration, the 32 steps, presenter guide | browser tested | `fix/demo-rerun` · `docs/demo/RERUN.md`: scripted scenarios and the browser demo re-run on the remediated stack (fake model, log-only providers); two defects found and fixed (REMEDIATION B4, B5). Film voice-over and devices: not re-run |
| K | Full verification on integrated `main` (every suite, builds, load, outage, backup, licence scan) | integration tested | `docs/progress/VERIFICATION.md`: backend unit and database suites, dashboard, phone, watch builds, outage, backup and restore, secret scan, licences. Load (24 h soak): not run. Device, emulator and OS image runs: **needs external** |
| L | Endorsement evidence pack, GO / NO-GO decisions | not started | |

## External blockers (owner actions)

- A physical watch and a phone, for device testing.
- A Linux build host for AOSP: about 400 GB disk and 64 GB RAM.
- macOS with Apple signing, for the iOS build.
- Twilio (or similar) credentials, and a decision to pay.
- A monitoring-centre contract.
- KFall and FallAllD permissions, and confirmation of the SisFall and UMAFall terms from their authors
  (drafts in `docs/legal/dataset_permission_requests/`). SisFall matters most: the shipped watch
  fall model was trained on it.
- A Kaggle GPU session.
- Qualcomm supplier access.
- A decision on who holds the OS signing keys (release, platform and verified-boot keys) and where, for E4.
- MHRA and clinical-safety review.
- App store developer accounts.
