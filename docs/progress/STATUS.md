# IRA CARE: build status

Updated at the start and end of every phase. If a session ends, this file and `NEXT.md`
say where it stopped.

## Phases

| # | Phase | Branch | State |
| --- | --- | --- | --- |
| 1 | ci-and-build-rules | `feat/ci-and-build-rules` | Pull request open; database job red on 8 inherited tests awaiting the owner (D-007) |
| 2 | care-data-model | | Not started |
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
  `scripts/staging/run_scheduler.py`. The 8 remaining failures test removed parts (below).

## Deferred

- **8 inherited database tests of removed parts fail in CI** (DECISIONS D-007). They are
  unchanged and still run; the `Backend database tests` job is red on exactly these until
  the owner decides to retire them or restore what they test:

  - `tests/readiness/test_phase_4_acceptance.py::test_the_closure_report_exists`
  - `tests/readiness/test_phase_4_acceptance.py::test_no_control_document_still_says_the_secrets_are_plaintext`
  - `tests/readiness/test_concurrency_and_failover.py::test_the_untestable_items_are_still_recorded_as_untestable`
  - `tests/callers/test_api_route_callers.py::test_technical_library_route_binds`
  - `tests/callers/test_api_route_callers.py::test_self_knowledge_route_binds`
  - `tests/callers/test_api_route_callers.py::test_files_route_binds`
  - `tests/callers/test_api_route_callers.py::test_agents_route_binds`

- **One inherited test** fails only on a full local clone (DECISIONS D-004).

## Needs the owner

- A Galaxy Watch with LTE for on-device testing.
- The vendor sensor SDK partner request before release.
- SMS and call provider keys (the adapter stays off without them).
- A monitoring-centre contract for tier 3.
- Regulatory advice before sale.
