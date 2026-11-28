# IRA CARE: build status

Updated at the start and end of every phase. If a session ends, this file and `NEXT.md`
say where it stopped.

## Phases

| # | Phase | Branch | State |
| --- | --- | --- | --- |
| 1 | ci-and-build-rules | `feat/ci-and-build-rules` | Merged (pull request 1) |
| 2 | care-data-model | `feat/care-data-model` | Merged (pull request 2) |
| 3 | checkin-engine | `feat/checkin-engine` | Merged (pull request 3) |
| 4 | monitoring-and-escalation | `feat/monitoring-and-escalation` | Merged (pull request 4) |
| 5 | watch-app | `feat/watch-app` | Merged (pull request 5) |
| 6 | family-app | `feat/family-app` | Merged (pull request 6) |
| 7 | carer-dashboard | `feat/carer-dashboard` | Merged (pull request 7) |
| 8 | reports-and-history | `feat/reports-and-history` | Merged (pull request 8) |
| 9 | hardening | `feat/hardening` | Merged (pull request 9) |

Round 1 is merged to `main`. Round 2:

| # | Phase | Branch | State |
| --- | --- | --- | --- |
| R2-1 | continuous-monitoring | `feat/continuous-monitoring` | Merged (pull request 10) |
| R2-2 | home-and-location | `feat/home-and-location` | Merged (pull request 11) |
| R2-3 | fall-detection | `feat/fall-detection` | Merged (pull request 12) |
| R2-4 | ira-conversation | `feat/ira-conversation` | Merged (pull request 13) |
| R2-5 | demo-mode | `feat/demo-mode` | Merged (pull request 14) |
| R2-6 | onboarding-and-billing | `feat/onboarding-and-billing` | Merged (pull request 15) |
| R2-7 | deployment-ready | `feat/deployment-ready` | Merged (pull request 16) |
| R2-8 | store-readiness | `feat/store-readiness` | Merged (pull request 17) |
| R2-9 | compliance-drafts | `docs/compliance-drafts` | Merged (pull request 18) |
| R2-10 | final-readiness | `feat/final-readiness` | Merged (pull request 19, as a merge commit) |

Round 2 is merged to `main` (pull requests 10 to 19). Round 3 (the completion run):

| # | Phase | Branch | State |
| --- | --- | --- | --- |
| R3-0 | remove-demo-state | `fix/remove-demo-state` | Merged (pull request 20) |
| R3-1 | trustworthy-sos | `fix/trustworthy-sos` | Merged (pull request 21) |
| R3-2 | care-push-isolation | `fix/care-push-isolation` | Merged (pull request 22) |
| R3-3 | responder-follow-up | `fix/responder-follow-up` | Merged (pull request 23) |
| R3-4 | private-model | `feat/private-model` | Merged (pull request 24) |
| R3-5 | own-models | `feat/own-models` | Merged (pull request 25) |
| R3-6 | scheduled-care | `feat/scheduled-care` | Merged (pull request 26) |
| R3-7 | ira-personal | `feat/ira-personal` | Merged (pull request 27) |
| R3-8 | watch-platform-layer | `feat/watch-platform-layer` | Merged (pull request 28) |
| R3-9 | hardware-blueprint | `feat/hardware-blueprint` | Merged (pull request 29) |
| R3-10 | one-mobile-app | `feat/one-mobile-app` | Merged (pull request 30) |
| R3-11 | dashboard-care-record | `feat/dashboard-care-record` | Merged (pull request 31) |
| R3-12 | clean-product-ui | `feat/clean-product-ui` | Merged (pull request 32) |
| R3-13 | founder-demo-data | `feat/founder-demo-data` | Merged (pull request 33) |
| R3-14 | staging-proof-and-film | `feat/staging-proof-and-film` | Merged (pull request 34) |
| R3-15 | cost-cuts | `ci/cost-cuts` | Merged (pull request 35) |
| R3-16 | truth-sweep-and-final | `docs/truth-sweep-and-final` | Merged (pull request 36) |

Round 3 is merged to `main` (pull requests 20 to 36). Round 4 (the polish, pull requests 37 to 40, and the GPU profile, 41):

| # | Phase | Branch | State |
| --- | --- | --- | --- |
| R4-1 | fast-conversation | `feat/fast-conversation` | Merged (pull request 37) |
| R4-2 | watch-design-renders | `fix/watch-design-renders` | Merged (pull request 38) |
| R4-3 | demo-film-polish | `feat/demo-film-polish` | Merged (pull request 39) |
| R4-4 | polish-final | `docs/polish-final` | Merged (pull request 40) |
| R4-5 | gpu-profile (asked for after the round) | `feat/gpu-profile` | Pull request 41; merges on green CI |

Round 5 (the master directive, pull requests 63 to 80; the remediation programme's earlier pull requests, 42 to 62, are in `COMPLETION_REGISTER.md`):

| # | Phase | Branch | State |
| --- | --- | --- | --- |
| R5-1 | care-memory-promises | `fix/care-memory-promises` | Merged (pull request 63) |
| R5-2 | demo-rerun | `fix/demo-rerun` | Merged (pull request 64) |
| R5-3 | dataset-licence-register | `fix/dataset-licence-register` | Merged (pull request 65) |
| R5-4 | fall-model-research-only | `fix/fall-model-research-only` | Merged (pull request 66) |
| R5-5 | demo-pages-production-off | `fix/demo-pages-production-off` | Merged (pull request 67) |
| R5-6 | watch-degraded-storage | `fix/watch-degraded-storage` | Merged (pull request 68) |
| R5-7 | register-block-sync | `fix/register-block-sync` | Merged (pull request 69) |
| R5-8 | no-false-all-well | `fix/no-false-all-well` | Merged (pull request 70) |
| R5-9 | completion-framework | `fix/completion-framework` | Merged (pull request 71) |
| R5-10 | security-review | `fix/security-review` | Merged (pull request 72) |
| R5-11 | compliance-traceability | `fix/compliance-traceability` | Merged (pull request 73) |
| R5-12 | care-plans-staff-roles | `fix/care-plans-staff-roles` | Merged (pull request 74) |
| R5-13 | ai-fall-v2 | `fix/ai-fall-v2` | Merged (pull request 75) |
| R5-14 | ai-heart-learned | `fix/ai-heart-learned` | Merged (pull request 76) |
| R5-15 | ai-activity-wisdm | `fix/ai-activity-wisdm` | Merged (pull request 77) |
| R5-16 | staging-containers | `fix/staging-containers` | Merged (pull request 78) |
| R5-17 | care-journey-22 | `fix/care-journey-22` | Merged (pull request 79) |
| R5-18 | requirements-sync | `fix/requirements-sync` | Merged (pull request 80) |

Round 5 is merged to `main` (`eec19a0`). Verification: `VERIFICATION.md`; the GO / NO-GO decision: `FINAL_REPORT.md`.

## Stand-in watch test (28 November 2026)

A real-watch test build and a test server, so the owner can run the watch care journey on a
Samsung Galaxy Watch 4 (Wear OS 6) as a **stand-in device**, never as IRA watch validation.

| Piece | Where | State |
| --- | --- | --- |
| Test server address typed on the watch before pairing (debug builds only; https, or http to the emulator's 10.0.2.2 only) | `watch/app/src/debug/java/care/ira/watch/testserver/TestServer.kt`, `watch/app/src/main/java/care/ira/watch/api/ServerAddress.kt` | Debug 220 and release 219 JVM tests pass locally; negative controls seen failing |
| Release keeps its configured https address and refuses cleartext | `watch/app/src/release/java/care/ira/watch/testserver/TestServer.kt`, `ReleaseServerAddressTest`, `scripts/ci/check_watch_release_server.py` | Release build unchanged; checked in the unit tests and on the built APKs |
| Manual workflow "watch device test server (manual)" | `.github/workflows/watch-device-test.yml` | Rehearsed locally up to the tunnel (database, seed, API, dashboard, proxy, sign-in, pairing code, pairing, Help, alert); its first real run is the owner's |
| Owner's guide and tunnel terms | `docs/evidence/device/GALAXY_WATCH4_TEST.md`, `docs/evidence/device/TUNNEL_CHOICE.md` | Written |

No requirement changed state: results are recorded when the owner sends them.

## Round 9 (26 to 27 November 2026), one pull request

| Workstream | Branch and pull request | State |
| --- | --- | --- |
| Credential history scanner (D-004 resolved, D-097) | `praveen/vigilant-tesla-bhe022`, pull request 128 | Merged into the branch; 20 tests |
| Step-up authentication for deployment administrators (D-095) | the same | 47 tests; T43 controlled, S15 fixed |
| Deployment administration screen (D-096) | the same | 22 dashboard tests (160 in all), build green; browser run pending on staging |
| Environment-dependent tests run in CI; inherited skips recorded (D-098) | the same | Redis, role-separation, PyAV and failover tests run; one stale test fixed |
| OS host checks against an AOSP reference (D-099) | the same | 76 tests; four product-layer defects fixed; 13 OS items accepted |
| Watch emulator journey, 11 to 44 steps (D-100) | the same | 43 of 44 passed on the emulator (run 45910155640); the Unpair scroll fix awaits its run |
| Phone app and the authenticated Android journey (D-101, D-105) | the same | 11 of 16 steps passed on the emulator (run 45910155640); two script faults fixed, run pending |
| iOS simulator launch (D-105) | the same | built with its JavaScript bundled, launched, no crash; the screen probe's second channel awaits its run |
| API memory: pool churn found and fixed; 1-hour soak PASS at 1.996 % an hour (D-102) | the same | G2.j accepted; G2.e (24 hours) needs a remote server that stays up a day (never the owner's Mac) |
| Fall confirmation stage, off by default (D-103) | the same | 8 JVM and 9 Python tests; synthetic: 154 of 160 falls kept, alarms 88 to 62 |
| Round 9 conversation scenarios and rule fixes (D-104) | the same | development 129 of 129; hold-out 37 of 41, misses recorded |
| Review packs for the technical reviewer (F3.c, F7.a, F7.e, F8.a) | the same | written; decisions are the reviewer's |

The verification record of the final tree is `VERIFICATION.md` ("Round 9"); the completion
measures are in `completion_history.json` ("round 9"). The founder actions that remain are in
`NEXT.md`.

## Round 4, phases 2 to 4 (24 November 2026)

- **Phase 2 (pull request 38).** One look (graphite case `#2B2E33`, coral Help button
  `#E2574C`; D-072) in the Blender model, the eight renders at 1600 × 1200, the 20-second
  turntable, the glTF, `viewer.html` (three.js 0.170.0) and the dashboard's browser watch. The
  exploded view is computed from each layer's height, 7 mm apart, in the order cover glass,
  display, display frame, case with buttons, logic board, battery, sensor board, back cover;
  labels drawn after rendering, each line ending on its part. Every render reviewed at full
  size; the viewer exploded in headless Chromium with no console errors.
- **Phase 3 (pull request 39).** The film `docs/film/ira_care.mp4`: 1920 × 1080, 30 fps,
  5 min 6 s, 15.2 MB; title card, the turntable, the journey with cross-fades and zooms on the
  key moments, captions, the Piper voice-over and IRA's own lines. Recording against the real
  model found four conversation faults and one wording fault (D-073), each fixed with a test
  that fails without its fix; after them, on staging, the acknowledgement arrives at once and
  the model's first sentence 3.5 to 5.5 s after the person spoke. Local suites: care unit 159
  passed, care database 366 passed, dashboard 36 passed. The clean-clone staging proof with
  the end-to-end browser tests passed in 186 s on the merged tree
  (`docs/evidence/staging/clean_clone_proof.json`).
- **Phase 4 (pull request 40).** The documents brought in line with the merged code; the
  Kaggle GPU measurement (below, in `docs/MODEL_CHOICE.md`); `FINAL_REPORT.md` section
  "Round 4".

## Round 4, phase 1 (fast-conversation), measured locally (24 November 2026)

- 4 vCPU Xeon 2.10 GHz, 15.7 GB, no GPU, Ollama 0.40.18. 162-turn evaluation on the round 4
  code: `gemma4:e4b` quality 0.909, red-flag recall 1.00, first words 0.0 s (server side),
  first sentence 2.8 s p50, 5.9 s p95; `gemma4:e2b` 0.882, 1.9 s; `gemma4:12b` 0.895, 10.5 s;
  `gemma4:e4b-it-qat` 0.844, 2.0 s; `qwen3.5:2b` 0.834, 1.6 s. Chosen: `gemma4:e4b`, then
  `gemma4:e2b` (`docs/MODEL_CHOICE.md`, D-069 to D-071).
- Variants on `gemma4:e4b`: round 3 code cold 15.4 s to the first sentence, kept loaded 5.1 s,
  shorter prompt 4.0 s, context 1024 3.5 s, acknowledgement 0.0 s to the first words.
- Negative controls: the acknowledgement removed (2 tests fail), `keep_alive` dropped from the
  request (1 fails), nothing prepared ahead (the database test fails), the warm job not
  registered (the job list refuses it). Each restored before committing.
- Local suites: the care conversation, eval runner, worker jobs and server tests (77 passed);
  the check-in database suite against PostgreSQL 16 (8 passed); dashboard Vitest passed.

## Round 3, phase 15 (cost-cuts), measured from the Actions API (24 November 2026)

- Before: run 45305028277 (the push to `main` after pull request 24) billed 21.1 minutes
  across eight jobs; with the pull request's own run, 42.2 minutes a phase.
- After, pull request 35's own run (45394100905): 19.3 minutes of job time across the same
  eight jobs (the change touches the workflow and `scripts/ci`, which count as shared, so
  every area ran); a client-only or backend-only change runs less.
- After, the push to `main` that merged it (45396148725): the heavy jobs did not skip.
  `scripts/ci/already_tested.sh` built the workflow name from `GITHUB_WORKFLOW_REF` in the
  wrong order (it kept the branch name, not the file name), the Actions API answered 404 and
  the script, as designed for any doubt, ran everything. Phase 16 corrects the order; the
  corrected script, run on a checkout of that `main` commit against the real API, answers
  `already_tested=true` and names pull request 35's head. The first run that can show the
  skip on `main` is the push that merges pull request 36. Details in
  `docs/evidence/ci/minutes.json`.

## Round 3, phase 16 (truth-sweep-and-final), measured locally (24 November 2026)

- `python3 scripts/check_docs_truth.py`: every row of `docs/READINESS.md` is in one of the
  three states and every proof file named in a row exists (75 rows). The same script runs in
  CI as the job "Readiness tells the truth".
- Stale states corrected: round 1 rows (nine phases) and R2-10 in this file still read
  "open" or "stacked" although merged; the final report's round 2 table said pull request 19
  was open.
- Repository scan for assistant or tool names over every tracked file and every commit
  message of round 3: none. Authors on `main`: Praveen Kamineti's account only, with Sravani
  Perni as co-author on every round 3 commit from phase 4 on.

## Round 3, phase 14 (staging-proof-and-film), measured locally (24 November 2026)

- `scripts/staging/clean_clone_proof.sh`: 16 steps, 181 s from `git clone` to the last browser
  test (compose up 11 s, migrations 2 s, server up 2 s, worker healthy 63 s, dashboard build
  25 s, phone export 4 s, seed 2 s, browser tests 51 s). Seven browser tests passed: five
  journey tests and two outage drills.
- Outage timings (`docs/evidence/staging/outages.json`): Redis stopped, server unhealthy
  within the first check (under 0.5 s), the watch's help press answered "Sending…" at once,
  the request visible to the care team within the first check after Redis returned;
  PostgreSQL stopped, unhealthy within the first check, healthy 0.5 s after it returned.
- Six defects found by the proof and fixed (D-066), three of them with new tests
  (`test_care_server.py`, `test_care_sos_db.py`, `dashboard/src/__tests__/face.test.ts`).
- The film `docs/film/ira_care.mp4`: 5 minutes 30 seconds, 1280 × 720, 3.9 MB; nine scenes (the
  turntable, six recorded from the staging stack, the hardware renders) with the narration of
  `docs/film/NARRATION.md` spoken by Piper's `en_GB-cori-high` voice.

## Round 3, phase 13 (founder-demo-data), measured locally (24 November 2026)

- The seed creates four accounts (Praveen Kamineti, coordinator; Sravani Perni, family; Ada
  Hartley, the wearer; a read-only viewer) and the two made-up people with their circles;
  it ran against the local demo database. `test_demo_data_rules.py`: 6 tests pass.
- `scripts/screens/capture.mjs` retook 68 screenshots (34 screens × phone and desktop),
  none wider than the phone; the wearer's home, care, alerts and settings screens are new.

## Round 3, phase 12 (clean-product-ui), measured locally (24 November 2026)

- The browser watch split into `/watch` (what the person sees) and `/presenter` (what a demo
  needs), joined by a BroadcastChannel; `/simulator` redirects. Design system written down
  in `docs/design/DESIGN_SYSTEM.md`; dashboard tokens equal the phone theme's, dark mode added.
- Developer-words tests: dashboard (pages and components) and phone (screens and shell) pass
  with the words they forbid listed in each file. Dashboard: 32 tests, type check and lint
  clean. Phone: 43 tests.
- `scripts/screens/capture.mjs` took 60 screenshots (30 screens × phone and desktop) against
  the demo stack (demo database, second Redis, demo API, dashboard build, phone web export).
  First pass found seven flaws (D-063), all fixed; second pass: no phone screenshot wider than
  390 px, every screen reviewed by eye. `docs/screens` is 17 MB.
- The wearer's own phone experience is not in the screenshots: the demo seed has no circle
  member with the relationship "self" yet (phase 13 adds one).

## Round 3, phase 11 (dashboard-care-record), measured locally (24 November 2026)

- `GET /overview` (`care/overview.py`), `GET /people/{id}/access-notes` (managers, logged),
  richer daily and weekly reports (visits, reminders, what IRA remembers).
- Dashboard: the overview page and the ten-tab person record; type check, lint and 31 tests
  (2 new: a reading's freshness in plain words; the record's headline).
- Server: 3 tests in `test_care_record_db.py` (the overview's reasons; entry instructions for
  managers only with the access log; the report's new content with no quoted words).

## Round 3, phase 10 (one-mobile-app), measured locally (24 November 2026)

- Migration 111 (`care_user_preferences`, 150 to 151 tables), routes `/me/experience`,
  `/me/preferences`, `/people/{id}/help`; account erasure covers the new table.
- Phone: type check and 42 tests (15 new: appearance rules, experience model, the shell at
  each experience, settings controls, the tab bar); the shared UI now follows the theme.
- Server: 3 tests in `test_care_one_app_db.py` (experience by relationship and role; the
  appearance per account and device with a photo, a bad colour and an oversized photo
  refused; Help from the phone), run against PostgreSQL.

## Round 3, phase 9 (hardware-blueprint), measured locally (24 November 2026)

- Blender 5.3 (the `bpy` wheel from PyPI, Python 3.13) runs headless on this machine: the
  build script makes the model, seven renders at 1600 × 1200 with 96 samples on 4 CPU cores
  (about 2 to 3 minutes each), the turntable (PNG frames encoded by ffmpeg) and the glTF.
- The viewer (three.js 0.170, embedded) was opened in a headless browser at phone (Pixel 7
  profile) and desktop sizes: 31 meshes loaded, no page or console errors, the explode slider
  and part descriptions work.
- Component figures were read from manufacturers' pages where their sites allowed it (the
  owner allowed those hosts on the environment); the rest are marked not verified.
- Folder size: `docs/hardware` is 17 MB in all (eight renders of about 1.9 MB each, the glTF
  0.95 MB, the viewer 2.35 MB with the model embedded, the 15 s turntable at 540 × 540 0.39 MB,
  the Blender file 0.25 MB), under the 60 MB budget.

## Round 3, phase 8 (watch-platform-layer), measured locally (24 November 2026)

- Two product flavours (`wearos`, `aosp`); Health Services moved into the Wear OS flavour;
  `SensorManagerSensorSource` for AOSP with `SensorEventMapper`; `SimulatedSensorSource`;
  `Haptics`, `Connectivity`, `ServerChannel`, `SpeechCapabilities` interfaces; the service
  routes through them. Both flavours build (`assembleWearosDebug`, `assembleAospDebug`) and
  pass the unit tests; `scripts/ci/check_watch_aosp_deps.sh` finds no Google service library
  on the aosp runtime classpath. `docs/hardware/SENSOR_HAL.md` is the hardware contract.

## Round 3, phase 7 (ira-personal), measured locally (23 November 2026)

- Migration 110: `care_communication_profiles`, `care_reminders` (148 to 150 tables),
  `care_checkins.reminder_id`, the `topics` consent, memory edit marks, `hydration_every_minutes`.
- Server: 8 rule tests and 4 database tests (profile and consent gating, a spoken reminder
  asked at its time and ticked off, two-step dose confirmation and hydration, memory edited and
  cleared by the representative with a carer refused, a concern alert labelled and linked).
- The evaluation set grew from 150 to 162 turns (repeated worries, timed reminders); the rules
  alone pass 159 of 162 (quality 0.966, red-flag recall 1.0), recorded in
  `docs/evidence/eval/phase7_rules.json`.
- Watch: 54 unit tests pass (voice during the fall countdown, profile prompting); phone 27,
  dashboard 29 with type checks and lint.

## Round 3, phase 6 (scheduled-care), measured locally (23 November 2026)

- Migration 109: `care_visits` and `care_visit_tasks` under forced row-level security
  (146 to 148 tables in the manifest), `visit_late_minutes` (default 15), alert kinds
  `visit_late` (notice) and `visit_missed` (urgent). The rollback refuses while any visit
  records an arrival or an outcome (proved in `test_care_visits_db.py`).
- Server: 4 rule tests and 4 database tests pass against a fresh PostgreSQL 16 as `ira_app`
  (late at +15 min with a notice push and an extra check-in; missed at the window's end with
  an urgent alert at tier 1; arrival closes the alert, the watch reads "here", a due
  check-in is pushed ten minutes; departure records outcome, tasks and handover; a second
  family gets 404; rollback refusal). The related tenancy, manifest and erasure suites:
  386 passed.
- The watch acknowledges a carer's arrival: a vibration, then `POST /devices/me/visits/{id}/acknowledge` (recorded once).
- Dashboard: type check, 29 tests (timeline sources and visit lines added) and lint pass.
  Phone: type check and 24 tests (visit model added) pass. Watch: `VisitStateTest` 3 pass
  beside the outbox tests; the screen itself needs hardware.

## Round 3, phase 5 (own-models), measured on Kaggle and locally (23 and 24 November 2026)

- Fall detector: trained in 186 GPU seconds on SisFall and UCI HAR; on 6,243 held-out windows
  from 21 unseen subjects sensitivity 0.990, specificity 0.9998, 3.8 false alarms a day (the
  rule detector on the same windows: 0.906, 0.984, 344 a day). The 17.9 kB TensorFlow Lite
  file is in the watch's assets; `ModelFallDetectorTest` (5) and `FallDetectorTest` (5) pass.
- Answer classifier: trained in 48 GPU seconds; equals the rules on the 150-turn eval at its
  gates (147 of 150) and is worse without them (144, five false alarms). Ships default off.
  `test_care_classifier.py`: 5 pass.
- Conversation fine-tune: four Kaggle attempts (about 2.6 GPU hours); the fourth trained
  (457 steps, 1,170 s) and scored 0.92 against the base model's 0.94 on 50 held-out prompts
  with a loss that did not fall. Not adopted. The GGUF conversion of the merged weights also
  failed in llama.cpp's tokenizer reader; it was not retried because the result would not
  have shipped.

## Round 3, phase 4 (private-model), measured locally (23 November 2026)

- The hosted model path is closed (`reasoning/api_consent.py`; 9 tests). IRA's words come from
  Ollama on the same machine: `gemma4:12b` then `gemma4:e4b`, then the set questions.
- Benchmark on 4 vCPU / 15.7 GB, no GPU, 150 turns each: gemma4:12b quality 0.86, first
  sentence p50 10.0 s; gemma4:e4b 0.78, 4.6 s; qwen3.5:9b 0.78, 6.5 s; qwen3.5:4b 0.73, 4.4 s
  (2 fallbacks); gemma4:26b-a4b did not load in 16 GB; rules alone 0.97. Red-flag recall 1.00
  everywhere (the rules decide). `docs/MODEL_CHOICE.md`, `docs/evidence/eval/*.json`.
- The eval set found three red-flag phrasings the rules missed and one reminder read as a
  call; all four are fixed in the rules and covered by tests.

## Round 3, phase 0 (remove-demo-state), measured locally (23 November 2026)

- `.demo/*` untracked (3 files). `scripts/demo/_local_state.py` generates the sealing key
  and per-account passwords on every seed; the seed ran against a fresh PostgreSQL 16 with
  pgvector (104 migrations) and `scenarios.py normal-day` ran on the result.
- gitleaks 8.24.3 over all 27 commits: 14 findings, all placeholders (`.gitleaksignore`),
  0 credentials; with the ignore list: "no leaks found". New CI job `secrets`.
- New ratchet: `tests/readiness/test_demo_state_not_tracked.py` (6 tests; the negative
  control with a fixed password failed 2 of them).
- Full local suites on this branch: unit suite without a database **4,828 passed, 536 skipped,
  2 failed** (both environmental and pre-existing: `test_public_url_allowed_by_egress_guard`
  needs public DNS, which this build machine blocks; `test_the_table_matches_a_fresh_scan_of_history`
  needs a history script that was never carried over, D-004); database suites **3,414 passed,
  15 skipped** once run on their own (a first run concurrent with the unit suite shared Redis
  and failed 85 sign-in tests with 503; rerun alone, every one passed). Watch unit tests pass
  under Gradle with the Android SDK installed locally.
- Local environment for this round: Docker (pgvector/pgvector:pg16), Redis, Python 3.12
  virtual environment with `requirements-test.txt`, Android SDK 35 at `/opt/android-sdk`.
  The two Python suites must run one after the other: they share one Redis.

## Measured locally (21 November 2026, Linux, Python 3.12)

Local measurements before the first phase, on the foundation as received:

- Backend suite without a database: **4,690 passed, 1 failed, 457 skipped** (278 s). The
  failure is `tests/test_published_credentials.py::test_the_table_matches_a_fresh_scan_of_history`,
  which needs a history-scanning script that was not carried over; it skips on the
  shallow clone CI uses (DECISIONS D-004).
- Migration chain `init.sql` through `098` applies cleanly as `ira_migrator` on a fresh
  PostgreSQL 16 with pgvector, and `scripts.check_migration_integrity` passes.
- Database suites, first run for this repository (`tests/tenancy`, `readiness`,
  `architecture`, `callers`, `idempotency`, `runtime`): **3,145 passed, 8 failed,
  34 errors**. The 34 errors were a local artefact and pass with the `postgres` role
  (DECISIONS D-007). One further failure was fixed by writing the missing
  `scripts/staging/run_scheduler.py`. The 8 remaining failures tested removed parts and were
  retired on the owner's decision (D-007).

## Phase 2 (care-data-model), measured locally

- Migration `099_care_foundation.sql`: fifteen `care_*` tables, forced RLS, append-only
  access log, rollback file, hash recorded, tenancy manifest at 141 tables.
- `supracloud-jarvis/ira/care/`: authorisation, consent rules, device credentials, audit,
  repository and 40 routes under `/api/v1/care`.
- New tests: 29 unit (authorisation, credentials, consent, route registration), 70
  adversarial RLS tests on the care tables, 11 database-backed API tests (two families, a
  watch pairing and reporting, help button, acknowledgement, consent gating, erasure).
  The workspace-table ratchet (`test_3c06`) now covers the fifteen care tables too.
- Negative control: with the manage rule and the consent rule deliberately broken, 10 unit
  tests failed; restored, all pass.

## Phase 3 (checkin-engine), measured locally

- `care/checkins.py` (rules: quiet hours, next check-in, question choice, wording,
  answer reading) and `care/checkin_cycle.py` (the one-minute worker job `care_checkins`
  and the watch's side: due, repeated, answer, missed; `ask-now` for family and carers).
- Five new routes; the job is registered in `worker/jobs.py` and `worker/scheduler.py`
  (on when `IRA_WORKER_JOBS` names it).
- Tests: 29 unit tests on the rules; 7 database-backed tests driving a full day with an
  explicit clock (scheduling window, watch collects the question once, repeat, answer with
  a dose confirmed, consent gating of answers, quiet hours and the morning question, two
  kinds of miss, the whole cycle over two tenants, the watch-side routes).
- Negative control: with quiet hours disabled and the miss deadline stretched, 9 tests
  failed; restored, all pass.

## Phase 4 (monitoring-and-escalation), measured locally

- `care/monitoring.py` (the rules), `care/providers.py` (log-only default, Twilio adapter
  off without keys, emergency numbers refused), `care/escalation.py` (tiers, waits, the
  tier-3 fallback, one-minute worker job `care_monitoring`).
- `scripts/simulate_patient.py`: the "person does not answer and stops moving" scenario
  end to end against a test database, printed as a timeline; the same code is what the
  database test asserts on.
- Tests: 11 unit tests (rules, wording, providers) and 4 database-backed tests (the full
  scenario to tier 3 and the acknowledgement that stops it; a notice told once; one open
  alert per kind and help escalating at once; three misses with normal readings).

## Phase 5 (watch-app), measured locally

- `watch/`: the Wear OS app (API client, check-in state machine, Health Services sensor
  source behind an interface, reading batcher, offline queue, pairing store, speaker and
  listener, foreground service, sync worker, boot receiver, three Compose screens).
- `./gradlew assembleDebug testDebugUnitTest` passes locally with an Android SDK (platform
  35): 22 JVM unit tests (state machine, offline queue, batcher, pairing code, API client
  against a mock server). The CI `watch` job now activates.
- Negative control: with the repeat timing broken, 2 tests failed; restored, all pass.
- Not done: nothing has run on a real watch or an emulator in this environment.

## Phase 6 (family-app), measured locally

- `mobile/`: the Expo app (sign-in and care-circle choice, home with people and an alert
  banner, alerts with one-tap "I'm on my way" and resolve, the "call 999 now" screen when
  the backend flags tier 3, a person screen with status, latest readings, check-in history
  under consent rules, contacts with call buttons, consent recording, and watch pairing
  codes for administrators; push registration).
- `npm run typecheck` and `npm test` pass locally: 14 Jest tests (API client against a fake
  fetch, alert ordering and wording, time and reading formatting). The CI `mobile` job now
  activates.
- Negative control: with the "needs someone" rule broken, 1 test failed; restored, all pass.
- Not done: nothing has run on a phone or in Expo Go in this environment.

## Phase 7 (carer-dashboard), measured locally

- `dashboard/`: Next.js 15 app (sign-in and organisation choice, overview with each
  person's worst open alert, alerts with acknowledge and resolve, person page with timeline,
  readings as SVG sparklines, routines, medication and doses, circle, safety settings form,
  consents, conversation under consent, access log, pairing codes).
- `npm run typecheck`, `npm test` (12 Vitest tests: view helpers, API client, alert card)
  and `npm run build` pass locally. The CI `dashboard` job now activates.
- Negative control: with the alert ordering rule broken, 1 test failed; restored, all pass.

## Phase 8 (reports-and-history), measured locally

- Migration `100_care_reports.sql` (one table, forced RLS, ratchets updated: manifest at
  142 tables, hash record, erasure registry, workspace-table suite).
- `care/reports.py` (gathering, summaries, the daily and weekly wording, generation,
  delivery, the hourly `care_reports` job) and `care/history.py` (one audited search).
  Four new routes. Dashboard: reports and search tabs. Family app: weekly report tab.
- Check-in summaries no longer quote the person's words (found by the report test that
  asserts a report never carries them; DECISIONS D-020).
- Tests: 5 unit (summaries, wording, no diagnosis words) and 4 database-backed (idempotent
  generation, family can read, other family cannot, weekly report, the hourly job in the
  person's timezone, search with and without consent, audit rows).
- Negative control: with the once-per-period rule broken, 1 test failed; restored, all pass.

## Phase 9 (hardening), measured locally

- `care/retention.py` and the daily `care_retention` job; a static audit-coverage test over
  the care routes; the 1,000-person load test (schedule pass 1.1 s, due pass 1.9 s,
  monitoring pass 0.65 s locally); the simulation now ends with the next morning's report.
- Tests: 2 static, 3 database-backed (retention keeps newer rows, open alerts and the access
  log; a shorter consented period removes more; the load test).
- Negative control: with the retention cutoff broken, 2 tests failed; restored, all pass.
- Final `README.md`, `STATUS.md` and `docs/progress/FINAL_REPORT.md` written.

## Round 2, phase 1 (continuous-monitoring), measured locally

- Migration `101_care_continuous_monitoring.sql`: six new safety settings (ask-first wait,
  heart-rate confirmation count and span, night off-wrist minutes, usual wake time, wake
  grace), the `asking` alert state, the `off_wrist_night` alert kind, `safety` and `morning`
  check-in kinds. Rollback file, hash recorded, manifest updated.
- `care/continuous.py` (the arrival-time range rule and movement sum), `care/askfirst.py`
  (ask first, answered, no answer, promotion by the server), `care/ops.py` (job heartbeats
  in `job_runs`, the status check, the operations alert through the provider). Readings are
  evaluated in `record_report`; the escalation pass promotes overdue questions; the check-in
  pass asks the morning check; both passes write a heartbeat. `GET /care/ops/status`.
- Watch: a `safety` question vibrates first, is spoken loud, repeats after 20 s and gives
  up after 25 s more (the server's deadline is the one that counts). 23 JVM tests (was 22).
- Dashboard: the "monitoring paused" banner on every page, polled each minute; the six new
  settings on the safety settings page. 14 tests (was 12); type check and build pass.
- Tests: 9 unit (range rule, movement, the status check), 6 database-backed (confirmed
  breach at night asks first and silence becomes an SOS; "I'm OK" resolves and "help"
  opens; the Help button during a question; night off-wrist notice and nothing for no
  movement; the morning check once a day and not for a person who moved; the operations
  check notices when the jobs stop and tells the operations contact once).
- `scripts/simulate_patient.py` now runs four scenarios (the original, abnormal heart rate
  at 02:00 with no answer, abnormal heart rate answered "I'm OK", no movement by wake
  time); all four ran locally.
- Negative control: see the pull request description and D-024 to D-027.

## Round 2, phase 2 (home-and-location), measured locally

- Migration `102_care_home_and_location.sql`: home address, coordinates and radius,
  emergency summary and sealed access notes on `care_people`; the one-off location and
  `at_home` on `care_alerts`; `access_notes` as an access-log resource kind. Rollback file,
  hash recorded, manifest updated (encryption now required for `care_people`).
- `care/location.py` (distance, at home, map link, the whereabouts sentence, the alert
  link), `care/accessnotes.py` (seal and open through `core/connectors/secrets`, who may
  view). The help route accepts an optional location; the escalation text carries address,
  whereabouts and alert link. `GET /care/alerts/{id}/access-notes`.
- Watch: one location fix asked for when help starts (never in the background), sent with
  the request; location permissions added. 24 JVM tests (was 23).
- Family app: whereabouts, emergency information, map link and the access-notes button on
  every SOS. 15 tests (was 14). Dashboard: the same on alert cards, plus a Home tab on the
  person page. 16 tests (was 14); type check and build pass.
- Tests: 7 unit (distance, at home with accuracy, unknown never guessed, map link, message
  wording with and without a fix, who may view, sealing bound to the row), 5 database-backed
  (the record never returns notes in clear and stores ciphertext; help with a location is
  told to the circle with address and map link; at home, no location and no home
  coordinates all stated honestly; access notes for tier 1 while open, refused to a viewer,
  allowed to the manager, refused once resolved, three views logged, other family 404; no
  route accepts a location outside an SOS).
- `scripts/simulate_patient.py`: a fifth scenario, the Help button away from home.
- Negative control: see the pull request description.

## Round 2, phase 3 (fall-detection), measured locally

- Migration `103_care_fall_detection.sql`: five per-person fall settings (on by default,
  countdown, impact, still window, movement threshold) and the `suspected_fall` alert kind.
  Rollback file, hash recorded, manifest updated.
- Watch: `FallDetector` (pure Kotlin, accelerometer only), `FallCountdown` (vibrate, ask,
  big buttons, help at zero), the accelerometer source, settings refreshed from
  `GET /devices/me` after each sync, the countdown on the screen. `care/falls.py` on the
  server: start, I'm OK, confirm with location, refuse when off. Two new device routes.
- **Synthetic-signal results (50 Hz, made-up signals, fixed seed):** on 20 synthetic falls,
  **0 false negatives**; on 20 hard sit-downs, 20 arm-swing walks and 10 table drops with the
  watch known to be off the wrist, **0 false positives**; on 10 table drops with the wrist
  state unknown, **10 of 10 read as a fall** (an impact followed by stillness is a fall to
  the accelerometer alone; the off-wrist signal is what tells them apart). **These numbers
  say nothing about real falls. Real-world accuracy is unmeasured until the app has run on
  real watches worn by real people**, and the maker's own fall detection should be on as a
  second layer (`docs/DEVICE_SETUP.md`).
- Tests: 8 watch JVM tests added (32 in all); 2 backend unit tests; 4 database-backed tests
  (the watch learns the settings and a carer can turn detection off, which refuses a start;
  a countdown asks first and I'm OK resolves it; Help or silence opens the SOS with location
  and the server's own deadline promotes it; a watch offline at the countdown still opens a
  fall alert). Dashboard: fall settings on the Settings tab; both apps name the alert
  "Suspected fall".
- `docs/DEVICE_SETUP.md`: pairing, permissions, IRA CARE's detector, and how to turn on the
  watch maker's own fall detection on Galaxy Watch, Pixel Watch and Apple Watch.
- Negative control: see the pull request description.

## Round 2, phase 4 (ira-conversation), measured locally

- Migration `104_care_conversation.sql`: `care_conversation_memory` (RLS, grants, manifest,
  erasure registry, adversarial suite, retention purge), `care_checkins.question_source`,
  the `concerning_phrase` alert kind. Rollback file, hash recorded, manifest at 143 tables.
- `care/conversation.py` (model adapter with the fake, guardrails, sentence streaming with
  the first-sentence budget, composing, structured reading with stricter-wins merging, red
  flags, talk intents, the rolling summary) and `care/talk.py` (context from the record,
  the exchange, transcripts, memory, ask-first on a concerning phrase). Check-ins are
  composed by the model by default (`IRA_CARE_MODEL=local`), templates remain the fallback.
  Routes: `POST /devices/me/talk` and `POST /people/{id}/talk` (one JSON object per line),
  `GET /people/{id}/memory`. The operations status reports which model is configured.
- Watch: a "Talk to IRA" button; the reply is spoken sentence by sentence as it streams; a
  safety-check action makes the watch ask "are you OK?" at once. 33 JVM tests (was 32).
- Family app: the "Talk to IRA (on this phone)" screen with the contact card for a call.
  16 tests (was 15). Dashboard: what IRA remembers, and a "composed" marker on check-ins.
- Tests: 10 unit (fake model, guard, sentences, stricter wins, red flags, structure with bad
  JSON, intents, streamed talk with fallback, rolling summary, the setting), 6
  database-backed (composed then template, structure and stricter wins with the person's
  words kept out of summaries, red flag asks first then escalates, talk streams and keeps
  transcripts under consent and rolls the memory, phone talk shows a contact and asks first
  on a concerning phrase, memory purged and erased).
- **First-sentence latency: unmeasured.** No local model runtime exists in this environment
  (4 CPUs, 15 GB, no GPU); the 4-second budget is a setting. CI uses the fake model.
- Negative control: see the pull request description.

## Round 2, phase 5 (demo-mode), measured locally

- `scripts/demo/seed_demo.py` (one command, idempotent, fake people and accounts),
  `scripts/demo/api.py` (the lean demo API: real login, workspace and care routers, care
  jobs, localhost browser origins), `scripts/demo/scenarios.py` (normal day with a
  conversation; abnormal heart rate with no answer and the chain to tier 3; a suspected fall
  with location; the next morning's report), `scripts/demo/up.sh` and `apps.sh`,
  `scripts/demo/record_demo.mjs` and `record_demo.sh` (Playwright).
- Dashboard: `/simulator`, the watch in the browser (same device API and token as the real
  watch; speech out, typed or spoken answers, I'm OK and Help, injectors for heart rate, no
  movement, a fall and removing the watch, Talk to IRA). 19 tests (was 16).
- Family app: web platform enabled and the web build produced (`expo export --platform
  web`); both apps sign in by email on a cloud deployment (owner route as the fallback); the
  bare browser `fetch` bug fixed in both apps. 17 tests (was 16).
- `docs/demo/`: 12 screenshots and `demo.mp4` (the recording), `DEMO_SCRIPT.md` (the
  10-minute walkthrough). Folder size is recorded in the pull request.
- Recorded end to end in a real browser: sign-in, people, the home record, the timeline,
  the simulator pairing with a device token, a confirmed heart-rate breach asking "are you
  OK?", Talk to IRA, the fall countdown, Help with location, the alerts page, the family app
  signing in, its alert cards and "I'm on my way".
- Found by the demo and recorded: the full server's delegated-scope gate refuses every cloud
  account on `/api/*` (see "Needs the owner" and the deployment phase).
## Round 2, phase 6 (onboarding-and-billing), measured locally

- Migration `105_care_onboarding.sql`: the invitation link on circle members, two care plans
  and their entitlements (no prices). Rollback file, hash recorded, manifest updated.
- `care/onboarding.py` and six routes: sign-up (identity register, 404 unless enabled), the
  organisation with a 30-day trial, invitations by link with consent captured at invitation,
  acceptance as the invited account, the invitation list, the plan with limits and usage;
  the person limit and the circle limit enforced (402).
- Dashboard: `/signup` (three steps), `/join` (the invitation link), `/billing` (plan,
  usage, catalogue with "price not set"), the invitation form with consent boxes on the
  Circle tab, a Watch tab that pairs step by step. 16 tests; type check passes.
- Family app: a pairing screen with five steps that watches for the first sync, and "I have
  an invitation link" on the sign-in screen. 16 tests; type check passes.
- Tests: 5 database-backed (sign-up closed unless enabled and never an oracle; an invitation
  creates the pending member, records consent, mails the link and is logged; acceptance by
  the invited account links the member, once only, strangers refused; plan limits for people
  and circle members; viewer and member 403, other organisation 404 on every new route). The
  fixture accounts now hold real tenant memberships, as accounts do.
- Negative control: see the pull request description.
- **CI note:** from 20:43 UTC on 21 November, GitHub reported `startup_failure` for every
  workflow run on an unchanged, valid workflow file. The run page later showed the reason:
  "recent account payments have failed or your spending limit needs to be increased"; three
  card payments had been declined. The guess recorded at the time (the Actions allowance)
  was wrong. The payment cleared on 23 November and CI started again; 14 to 18 merged that
  day. Each phase still started from `main`.
## Round 2, phase 7 (deployment-ready), measured locally

- `care/server.py` (the deployable server with `/health` and `/health/jobs`) and
  `care/worker.py` (the care jobs). Both booted locally against the test database and Redis;
  the health routes answered; the browser preflight answered for a listed origin.
- `deploy/`: `docker-compose.yml` (postgres, redis, api, worker, dashboard, caddy, backup;
  health checks and restart policies on every service), `Dockerfile.api`,
  `Dockerfile.dashboard`, `Caddyfile` (HTTPS by Caddy), `postgres-init/01-roles.sh`,
  `backup.sh`, `restore.sh`, `entrypoint.sh`, `.env.example`. The compose and workflow files
  parse; **the images have not been built here** (no container runtime in this
  environment), so the first `docker compose up` on a machine is the proof that remains.
- Backup and restore: run locally against the test database (151 tables and 104 migrations
  before and after); a wrong passphrase and a tampered file are both refused. The same round
  trip is a CI step on every pull request.
- `docs/DEPLOY.md`: a single UK-region machine, step by step, every variable explained.
- **Soak test** (`scripts/soak_24h.py`, 200 people, 24 simulated hours in 5-minute steps,
  289 passes each of check-ins and monitoring, 6873 check-ins answered and
  385 missed by 20 people who went silent): **20 of 20 silent people escalated,
  0 missing, 0 late, every one reached tier 3**. Check-in pass:
  mean 0.094 s, p95 0.131 s, max 0.163 s. Monitoring pass: mean
  0.202 s, p95 0.309 s, max 0.349 s. Measured on this build machine
  (4 CPUs, local PostgreSQL 16), not on a rented one.
- Tests: 2 unit tests on the server; the care suites unchanged and green.
- Negative control: see the pull request description.
## Round 2, phase 8 (store-readiness), measured locally

- Watch: release build configuration (minify, shrink, keep rules, signing from the owner's
  environment, server and version code at build time); the unsigned release package and
  shrinker mapping were produced locally; launcher icons; content descriptions, a live
  status region and 48 dp buttons. 35 JVM tests (was 33). `docs/store/WEAR_OS_LISTING.md`.
- Family app: icons, splash, identifiers, build numbers, notification usage text,
  `eas.json` with build profiles (no submission); accessibility labels, hints, states,
  headers, scalable text, 56-point buttons. 19 tests (was 16). `docs/store/FAMILY_APP_LISTING.md`.
- Dashboard: skip link, focus ring, larger text and targets, field labels, named alert
  cards, live region on the simulator, reduced-motion and high-contrast support; `jest-axe`
  and a contrast test. 18 tests (was 16); type check and build pass.
- `docs/legal/PRIVACY_POLICY.md` and `TERMS_OF_USE.md`, both marked DRAFT FOR LEGAL REVIEW.
- Not done here, listed under "Needs the owner": a screen-reader pass on a real watch and
  phone; store accounts and submissions; the signing key.
## Round 2, phase 9 (compliance-drafts)

- `docs/compliance/`: `INTENDED_PURPOSE.md`, `CLINICAL_SAFETY_CASE.md` (13 hazards, each tied
  to code and tests), `DPIA.md`, `DTAC_ANSWERS.md`, `MHRA_ADVISER_QUESTIONS.md` (13 questions),
  `INCIDENT_PROCESS.md`. All marked DRAFT FOR REVIEW BY A QUALIFIED PERSON; no compliance
  claimed. Documents only: no code or test changed in this phase.

## Round 2, phase 10 (final-readiness)

- `feat/final-readiness` merges pull requests 14 to 18 in order (D-049); the only conflicts
  were the progress pages and the family app's manifest and package file.
- From a clean clone of the integrated branch: lint clean; backend unit 3,768 passed;
  database suites 3,491 passed and one test that depended on the time of day (fixed to be
  independent of the clock); watch 35 tests and the unsigned release build; family app 20
  tests, type check and web export; dashboard 21 tests, type check and build; the demo
  seeded, the scenarios and the simulator ran, the soak escalated 20 of 20, and the recording
  produced its 12 screenshots and video. Details in `FINAL_REPORT.md`.
- `docs/READINESS.md` written; README, STATUS, FINAL_REPORT and NEXT updated.

## Deferred

- **8 inherited tests of removed parts** were retired on the owner's decision (DECISIONS D-007).
- **One inherited test** fails only on a full local clone (DECISIONS D-004).

## Needs the owner

- A Galaxy Watch with LTE for on-device testing (the watch app has only been unit tested).
- Deleting merged branches on GitHub: the session's access cannot delete branches; `feat/ci-and-build-rules` and `feat/care-data-model` can be removed from the GitHub branches page.
- The vendor sensor SDK partner request before release.
- SMS and call provider keys (the adapter stays off without them).
- A monitoring-centre contract for tier 3.
- Regulatory advice before sale.
- The full server's delegated-scope middleware (`security/scope_enforcer.py`) answers 403 to
  every cloud-edition account on `/api/*`, because it resolves roles from the private
  edition's delegated accounts only. The demo API does not install it; the deployment phase
  must decide how the full server handles cloud accounts (workspace roles already authorise
  every care route).
- Keep a working card as GitHub's default payment method: the two-day CI stop on 21 to 23
  November was declined payments, and the next monthly charge would stop CI the same way.
- A screen-reader and large-text pass on a real watch and a real phone (the automated checks
  cover labels, roles, contrast and sizes only).
- Store accounts, the signing key for the watch app, the build-service project for the family
  app, and the submissions themselves; the listing text and the policy drafts are ready for
  legal review first.
- Regulatory advice before sale: the questions are written (`docs/compliance/MHRA_ADVISER_QUESTIONS.md`).
- A Clinical Safety Officer to review the hazard log, a data protection officer to review the
  DPIA, and a security test; none has happened.
- Measuring the conversation model's first-sentence latency on the owner's machine with the
  configured small local model, and recording machine and model in STATUS.md.
- A decision on where the access-notes sealing module lives: inside `core/connectors/` (as
  built, which the architecture ratchet allows) or as a third named exemption of that ratchet
  for a module that owns its own secrets (which the ratchet's comments prefer). Same
  cryptography either way; the test was not changed by the build.
