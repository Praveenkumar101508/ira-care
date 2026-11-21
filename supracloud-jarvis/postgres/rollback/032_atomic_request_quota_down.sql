-- Rollback for 032_atomic_request_quota.sql.
--
-- Dropping these columns returns the daily request ceiling to check-then-act,
-- which `tests/readiness/test_behaviour_under_load.py` proves is bypassable by
-- sending requests in parallel. The rollback exists because every migration in
-- this repository has one and a chain that cannot be reversed cannot be tested
-- — not because reverting is advisable.
--
-- `core.models.store.reserve_daily_request` fails closed if the columns are
-- absent: it refuses the request rather than admitting it unmetered. So a
-- rollback without a matching application rollback denies traffic instead of
-- silently un-enforcing the ceiling.

BEGIN;

ALTER TABLE usage_quotas
    DROP CONSTRAINT IF EXISTS usage_quotas_daily_requests_used_check;

ALTER TABLE usage_quotas
    DROP COLUMN IF EXISTS daily_requests_used,
    DROP COLUMN IF EXISTS daily_period_started_at;

COMMIT;
