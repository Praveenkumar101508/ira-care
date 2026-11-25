# IRA CARE completion register

The single place that says, for every requirement in the owner's master directive (25 November
2026), what exists, what proves it, and what is still missing. The detailed safety findings and
their negative controls are in `REMEDIATION.md`; this register points to them.

Started 25 November 2026. **Keep it current:** a change to a row's status needs its evidence in
the same pull request.

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

## Pull requests in this programme

| PR | Branch | Group | State |
|---|---|---|---|
| [#44](https://github.com/Praveenkumar101508/ira-care/pull/44) | `fix/care-safety-answers` | A: dose answers, safety check, wearer-only talk, logs, migration 112 | merged, CI green |
| [#45](https://github.com/Praveenkumar101508/ira-care/pull/45) | `fix/dashboard-live-person` | B: live patient record, four-state wrist | merged, CI green |
| [#46](https://github.com/Praveenkumar101508/ira-care/pull/46) | `fix/care-delivery-reliability` | A2: outbox, delivery states, signed Twilio callbacks, migration 113 | open, CI running |
| [#47](https://github.com/Praveenkumar101508/ira-care/pull/47) | `fix/phone-session-and-talk` | C3/C4: sign-out isolation, session renewal | merged, CI green |
| [#48](https://github.com/Praveenkumar101508/ira-care/pull/48) | `fix/watch-speech-and-storage` | D: Help before speech, speech core, encrypted per-wearer storage | open, CI running |

## C. Software safety remediation (the 24 items)

| # | Requirement | Status | Evidence / blocker |
|---|---|---|---|
| C-1 | Conservative medication answers | integration tested | #44 · REMEDIATION A1 |
| C-2 | Every medication write path | integration tested | #44 · A2, A3a–c |
| C-3 | Dose identity, timing, provenance, correction history | integration tested | #44 · A3, A3d, A3e (migration 112) |
| C-4 | No false safety-check confirmation | integration tested | #44 · A4 |
| C-5 | No false fall-countdown cancellation | unit tested | #44 · A4b (JVM tests; CI Gradle build) |
| C-6 | Sensitive log protection | unit tested | #44 · A5. Logs outside `care/` not swept: **not started** |
| C-7 | Wearer-only conversation | integration tested | #44 · A6 |
| C-8 | Delivery states and verified callbacks | in progress | #46 · A2-3 |
| C-9 | Dispatch concurrency and retries | in progress | #46 · A2-1, A2-2 |
| C-10 | Live patient-record refresh | unit tested | #45 · B1 (component tests; not browser tested) |
| C-11 | Unknown and stale wrist status | unit tested | #45 · B2 |
| C-12 | Mobile conversation streaming | not started | needs `expo/fetch` or a native module; device builds to verify |
| C-13 | Native mobile voice | not started | needs native speech modules; **needs external**: device testing |
| C-14 | Complete logout isolation | unit tested | #47 · C3 |
| C-15 | Push-token unregister on sign-out | unit tested | #47 · C3 |
| C-16 | Secure automatic session renewal | unit tested | #47 · C4. Dashboard session renewal: **not started** |
| C-17 | Watch speech-recognition threads and lifecycle | in progress | #48 · D1 |
| C-18 | Watch text-to-speech lifecycle and time limits | in progress | #48 · D2 |
| C-19 | Encrypted watch storage | in progress | #48 · D5 (Keystore hardware backing: **needs external**, device) |
| C-20 | Atomic offline-queue and outbox writes | in progress | #48 · D5 |
| C-21 | Secure unpairing and re-pairing | in progress | #48 · D5 (kept per wearer). Explicit unpair flow with warning: **not started** |
| C-22 | Backend and person-level authorisation review | not started | Only the routes touched so far were reviewed (A6, A3c) |
| C-23 | Accessibility and interface consistency | pre-existing | Automated accessibility checks from round 4. Dark-mode colour review (C5): **not started** |
| C-24 | Full-system outage and recovery tests | not started | |

## D. The software platform

| # | Requirement | Status | Evidence / blocker |
|---|---|---|---|
| D1 | Watch app: check-ins, voice and touch, reminders, routines, visits, heart rate, falls, wrist, battery, Help, GPS, offline, outbox, delivery and responder status | pre-existing, partly re-tested | `STATUS.md` rounds 1–4; Help path and storage hardened in #48. **needs external**: physical watch |
| D1b | AOSP application flavour | pre-existing | CI builds both flavours |
| D2 | Phone app, wearer / family / carer experiences, personalisation | pre-existing | Round 4 one-app work. iOS build: **needs external** (macOS, signing). Play and App Store: **needs external** (developer accounts) |
| D3 | Care dashboard: overview, record, visits, alerts, reports, audit, search | pre-existing; live record improved | #45. Care plans as a separate object, staff role screens: **not started** |
| D4 | Product promises reconciled (food-intake check, camera, SpO2, ECG, HRV, sleep, blood pressure, hearing access) | not started | Hardware-dependent ones will get interfaces and an explicit "not available", never fake readings |

## E. IRA CARE OS (AOSP)

| # | Requirement | Status | Evidence / blocker |
|---|---|---|---|
| E1 | Distinguish app / launcher / system image / firmware / production OS | not started | |
| E2 | Product config, launcher, boot animation, settings, emergency UI, services, Cuttlefish image | not started | A full AOSP build needs about 400 GB disk and 64 GB RAM (source.android.com); **this container has neither**. Config, launcher and scripts can be prepared here; a built and booted image needs a build host |
| E3 | Sensor HAL interfaces with simulated drivers | pre-existing (contract) | `docs/hardware/SENSOR_HAL.md`; simulated drivers: **not started** |
| E4 | Verified boot, Keystore, signed OTA, rollback, provisioning | not started | Hardware-backed parts: **needs external** (target board) |
| E6 | OS architecture document, build instructions, Qualcomm dependency register | not started | |

## F. IRA intelligence (seven components)

| # | Component | Status | Evidence / blocker |
|---|---|---|---|
| F1 | Dataset licence register (PPG-DaLiA, UMAFall, KFall, FallAllD, conversation data, model weights) | pre-existing (partial) | `docs/DATASETS.md`. Re-verification against the original sources, plus permission-request drafts: **not started**. KFall and FallAllD: **needs external** (permission) |
| F2 | IRA Care Conversation: new versioned dataset, LoRA / QLoRA run, evaluation, promotion gate | pre-existing experiment | 507 synthetic conversations; rule pass 0.94 base against 0.92 fine-tuned on 50 prompts; the 162-turn product evaluation is a different measure (`docs/MODEL_CHOICE.md`). A new run: **not started**; GPU time: **needs external** (Kaggle account session) |
| F3 | IRA Care Heart (PPG-DaLiA, subject-independent) | not started | PPG-DaLiA is CC BY 4.0 (UCI). Download size and compute to be measured first |
| F4 | IRA Care Fall (wrist-specific, cross-dataset) | pre-existing model + deterministic fallback | Improvement run: **not started** |
| F5 | IRA Care Safety (deterministic rules, model only more cautious) | integration tested | #44 (A1, A2, A4) |
| F6 | IRA Care Memory (consent, correction, deletion) | pre-existing | Correction and deletion routes exist; re-verification: **not started** |
| F7 | IRA Care Activity (IMU) | not started | needs a dataset whose licence allows commercial use |
| F8 | IRA Care Speech (recognition and synthesis; elderly speech evaluation) | watch lifecycle in progress (#48) | Accuracy evaluation on elderly speech: **needs external** (consented recordings) |

## G. Server and deployment

| # | Requirement | Status | Evidence / blocker |
|---|---|---|---|
| G1 | Containers, CPU / GPU profiles, Postgres, Redis, workers, proxy, HTTPS | pre-existing | `deploy/`, `docs/DEPLOY.md`. Container images rebuilt in this programme: **not started** |
| G2 | Observability, outage recovery, encrypted backups, restore | pre-existing (partial) | CI restore round trip. Outage drills: **not started** |
| G3 | Push, SMS, call, email, monitoring-centre adapters | SMS / call states in progress (#46) | Live delivery: **needs external** (provider credentials, a decision to pay). Monitoring centre: **needs external** (contract) |
| G4 | Hosting proposal, cost model, runbooks | pre-existing (partial) | Renting a server: **needs external** (owner's approval to spend) |

## H, I. Hardware and compliance preparation

| # | Requirement | Status | Evidence / blocker |
|---|---|---|---|
| H1–H4 | Sensor evaluation plan, Qualcomm engagement document, ODM specification, EVT / DVT / PVT roadmap | pre-existing (partial) | `docs/hardware/`. Updates: **not started**. All physical work: **needs external** |
| I | Intended purpose, claims register, hazard log, DPIA, pilot protocol | pre-existing drafts | `docs/compliance/`. Updating against current MHRA / ICO guidance: **not started**. Approval: **needs external** (qualified review) |

## J, K, L. Demonstration, final verification, evidence

| # | Requirement | Status | Evidence / blocker |
|---|---|---|---|
| J | Integrated live demonstration, the 32 steps, presenter guide | pre-existing demo | `docs/demo/`. Re-running it on the remediated stack: **not started** |
| K | Full verification on integrated `main` (every suite, builds, load, outage, backup, licence scan) | not started | Each PR runs CI. One integrated run after all merges is still to do |
| L | Endorsement evidence pack, GO / NO-GO decisions | not started | |

## External blockers (owner actions)

- A physical watch and a phone, for device testing.
- A Linux build host for AOSP: about 400 GB disk and 64 GB RAM.
- macOS with Apple signing, for the iOS build.
- Twilio (or similar) credentials, and a decision to pay.
- A monitoring-centre contract.
- KFall and FallAllD permissions.
- A Kaggle GPU session.
- Qualcomm supplier access.
- MHRA and clinical-safety review.
- App store developer accounts.
