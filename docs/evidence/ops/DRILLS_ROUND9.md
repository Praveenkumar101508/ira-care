# Operations drills run in round 9 (26 November 2026)

Register G4.c says the runbooks exist and that a test keeps their file names true. This page
records the drills that were actually executed in this round, on the build container (Linux,
4 CPUs, 15 GB RAM, PostgreSQL 16.15 with pgvector, Redis 7, no Docker). Where the runbook
wraps a command in `docker compose exec`, the same script was run directly against a local
PostgreSQL; the scripts are the ones the containers carry (`deploy/backup.sh`,
`deploy/restore.sh`).

## Encrypted backup and restore (runbook section 4)

| Step | Result |
|---|---|
| `sh deploy/backup.sh` against the migrated test database (169 tables, 120 applied migrations) | wrote one `.sql.gz.enc` file of 97,760 bytes |
| `CREATE DATABASE iratest_restore`; `sh deploy/restore.sh <file> <dsn>` | restored without error |
| Table count before and after | 169 and 169 |
| `schema_migrations` rows before and after | 120 and 120 |
| Wall-clock time for dump, encrypt, restore and compare | 3 s |

The passphrase used was a throwaway for this drill and is not a secret. `pg_dump` 16.15 matched
the 16 server, as `deploy/backup.sh` requires (D-050: a newer client's dump is refused on
restore). The same round trip runs in CI's `backend-database` job on every backend change.

## The 22-step care journey (presenter guide, J.c and J.d)

`tests/tenancy/test_care_journey_db.py` was run against the migrated test database with the
deterministic model (`IRA_CARE_MODEL=fake`): **1 passed in 3.34 s**, all 22 steps. The real-model
repetition (8 of 8 runs) is in `docs/evidence/demo/REAL_MODEL_RUN.md` and was not repeated here
(no model server on this machine).

## Not run here, and why

- **Failover drill** (`supracloud-jarvis/scripts/failover-drill.sh`): it SIGKILLs the primary
  PostgreSQL and promotes a standby. The container's PostgreSQL was shared with other test runs
  during this round, so the drill was not run against it; the destructive failover *test*
  (`tests/readiness/test_concurrency_and_failover.py`, `IRA_TEST_ALLOW_DESTRUCTIVE=1`) is run
  against a database of its own and recorded in `docs/progress/VERIFICATION.md`.
- **Key rotation** (section 6) and **the worker stopped** (section 2) need the compose stack;
  this machine has no Docker. They remain drafts until run on a deployment.
