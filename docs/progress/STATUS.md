# IRA CARE: build status

Updated at the start and end of every phase. If a session ends, this file and `NEXT.md`
say where it stopped.

## Phases

| # | Phase | Branch | State |
| --- | --- | --- | --- |
| 1 | ci-and-build-rules | `feat/ci-and-build-rules` | Pull request 1 open; inherited tests retired (D-007) |
| 2 | care-data-model | `feat/care-data-model` | Built and tested locally; pull request stacked on phase 1 until phase 1 merges |
| 3 | checkin-engine | `feat/checkin-engine` | Built and tested locally; stacked on phase 2 |
| 4 | monitoring-and-escalation | `feat/monitoring-and-escalation` | Built and tested locally; stacked on phase 3 |
| 5 | watch-app | `feat/watch-app` | Built; unit tests pass; stacked on phase 4; not yet run on a real watch |
| 6 | family-app | `feat/family-app` | Built; type check and tests pass; stacked on phase 5; not yet run on a phone |
| 7 | carer-dashboard | `feat/carer-dashboard` | Built; type check, tests and production build pass; stacked on phase 6 |
| 8 | reports-and-history | `feat/reports-and-history` | Built and tested locally; stacked on phase 7 |
| 9 | hardening | `feat/hardening` | Built and tested locally; stacked on phase 8 |

Round 1 is merged to `main`. Round 2:

| # | Phase | Branch | State |
| --- | --- | --- | --- |
| R2-1 | continuous-monitoring | `feat/continuous-monitoring` | Merged (pull request 10) |
| R2-2 | home-and-location | `feat/home-and-location` | Merged (pull request 11) |
| R2-3 | fall-detection | `feat/fall-detection` | In progress |
| R2-4 | ira-conversation | `feat/ira-conversation` | Not started |
| R2-5 | demo-mode | `feat/demo-mode` | Not started |
| R2-6 | onboarding-and-billing | `feat/onboarding-and-billing` | Not started |
| R2-7 | deployment-ready | `feat/deployment-ready` | Not started |
| R2-8 | store-readiness | `feat/store-readiness` | Not started |
| R2-9 | compliance-drafts | `docs/compliance-drafts` | Not started |
| R2-10 | final-readiness | `feat/final-readiness` | Not started |

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
- A decision on where the access-notes sealing module lives: inside `core/connectors/` (as
  built, which the architecture ratchet allows) or as a third named exemption of that ratchet
  for a module that owns its own secrets (which the ratchet's comments prefer). Same
  cryptography either way; the test was not changed by the build.
