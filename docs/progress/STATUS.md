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
| R2-3 | fall-detection | `feat/fall-detection` | Merged (pull request 12) |
| R2-4 | ira-conversation | `feat/ira-conversation` | Merged (pull request 13) |
| R2-5 | demo-mode | `feat/demo-mode` | Merged (pull request 14) |
| R2-6 | onboarding-and-billing | `feat/onboarding-and-billing` | Merged (pull request 15) |
| R2-7 | deployment-ready | `feat/deployment-ready` | Merged (pull request 16) |
| R2-8 | store-readiness | `feat/store-readiness` | Merged (pull request 17) |
| R2-9 | compliance-drafts | `docs/compliance-drafts` | Merged (pull request 18) |
| R2-10 | final-readiness | `feat/final-readiness` | Integrates 14 to 18; proved from a clean clone; pull request 19 open |

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
