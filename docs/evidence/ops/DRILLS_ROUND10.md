# Operations drills run in round 10 (28 November 2026)

Round 9 ran the backup and restore drill (`DRILLS_ROUND9.md`) and left two runbook sections as
drafts: **the worker stopped** (section 2) and **key rotation** (section 5), because they were
written for the compose stack and the build machine has no Docker. This round ran both with the
production entry points the containers start (`deploy/entrypoint.sh`): the server
`uvicorn care.server:app` and the worker `python -m care.worker`, against a migrated and seeded
PostgreSQL 16 with pgvector and Redis 7 on the build machine (not the owner's computer). Where the
runbook says `docker compose restart worker` or `docker compose up -d api worker`, the drill stops
and starts those same processes.

Script: `scripts/ops/runbook_drills.py`. Results: `drills_round10.json`. **10 of 10 passed**
(248 s), after one fix the first run found.

| Section | Step | Result |
|---|---|---|
| 2 | `/health/jobs` fails while the worker is down | 503, `monitoring_paused: true`, both jobs over five minutes old |
| 2 | Help pressed on the watch during the gap is kept, and nothing is sent | the alert opened (201); no delivery attempt at the press or 20 s later |
| 2 | `/health/jobs` turns healthy after the worker starts | 200 within 65 s |
| 2 | the alert the gap left is sent on, with nobody touching it | the first tier's push, text and call to both circle members, within 65 s (the providers log; nothing leaves the machine) |
| 5 | `IRA_SECRET_KEY`: a new value and a restart sign everyone out | the old access and refresh tokens refused (401); signing in again works (200) |
| 5 | one account's sessions: `POST /auth/logout/all` | both of that account's sessions and its refresh token refused (401); another account unaffected (200) |
| 5 | a watch's device token: revoked, then paired again | the old token refused (401); a new pairing code pairs it (200) and the new token works (200) |
| 5 | `BACKUP_PASSPHRASE`: one backup before and one after the change | the new one restores with the new passphrase (120 migrations); the old one is refused with the new passphrase (exit 3, before touching the database) and restores with the old one |
| 5 | every backup file is private to its owner (G2.a) | 0600 for the dump and its checksum, the folder 0700 |

## The defect the first run found, fixed

**The deployment's backups were readable by every account on the host.** `deploy/backup.sh` wrote
the encrypted dump and its checksum with the process umask: 0644 on a stock host. The dump is
ciphertext, but its name, size and timing still say something, and the in-app backups had already
been made private (`tests/test_backup_files_are_private.py`); the deployment's own script had only
been checked as configuration. It now sets `umask 077` and makes the backup folder 0700;
`deploy/offsite.sh` does the same for fetched copies (`deploy/restore.sh` already decrypted into a
private `mktemp` file). `tests/tenancy/test_deploy_backup_private_db.py` runs the script against a
real PostgreSQL under umask 022 and fails without the fix (the folder was 0755); CI's backup round
trip now checks the modes too.

## Not run, and why

The provider secrets in section 5 (`IRA_CARE_TWILIO_AUTH_TOKEN`, `IRA_CARE_CALLBACK_SECRET`,
`IRA_CARE_ARC_SECRET`, the off-site bucket keys) each need the provider's own console, and none is
configured. The `docker compose` wrappers themselves are exercised by the deployment-guide workflow
(`.github/workflows/deploy-guide.yml`), which runs on a free runner once it is on `main`.
