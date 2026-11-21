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
| 7 | carer-dashboard | | Not started |
| 8 | reports-and-history | | Not started |
| 9 | hardening | | Not started |

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
