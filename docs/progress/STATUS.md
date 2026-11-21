# IRA CARE: build status

Updated at the start and end of every phase. If a session ends, this file and `NEXT.md`
say where it stopped.

## Phases

| # | Phase | Branch | State |
| --- | --- | --- | --- |
| 1 | ci-and-build-rules | `feat/ci-and-build-rules` | Pull request 1 open; inherited tests retired (D-007) |
| 2 | care-data-model | `feat/care-data-model` | Built and tested locally; pull request stacked on phase 1 until phase 1 merges |
| 3 | checkin-engine | | Not started |
| 4 | monitoring-and-escalation | | Not started |
| 5 | watch-app | | Not started |
| 6 | family-app | | Not started |
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

## Deferred

- **8 inherited tests of removed parts** were retired on the owner's decision (DECISIONS D-007).
- **One inherited test** fails only on a full local clone (DECISIONS D-004).

## Needs the owner

- A Galaxy Watch with LTE for on-device testing.
- The vendor sensor SDK partner request before release.
- SMS and call provider keys (the adapter stays off without them).
- A monitoring-centre contract for tier 3.
- Regulatory advice before sale.
