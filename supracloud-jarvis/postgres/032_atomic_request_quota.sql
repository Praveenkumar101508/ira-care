-- ─────────────────────────────────────────────────────────────────────────────
-- 032 — an atomic daily request ceiling
--
-- WHAT THIS FIXES, and how it was found.
--
-- `tests/readiness/test_behaviour_under_load.py` fired 24 concurrent requests
-- at a tenant whose `daily_request_limit` was 5. **All 24 were admitted.**
--
-- The enforcement in `core/models/gateway.route` is check-then-act:
--
--     load_quota()    -- SELECT COUNT(*) of today's usage rows
--     check_quota()   -- decide against that snapshot
--     ...the model call...
--     record_usage()  -- INSERT the row the next reader will count
--
-- Nothing holds between the read and the write, and nothing in the schema caps
-- the row count. Every request that reads before any request writes sees the
-- same under-limit snapshot and admits itself. In production the window is
-- WIDER than in the test, because a real model call sits inside it.
--
-- Counting rows can never be made atomic by reading harder. A ceiling that is
-- enforced by a count is enforced against the past. So the count moves into a
-- column, and admission becomes a single conditional UPDATE: PostgreSQL takes a
-- row lock for the duration, concurrent updaters serialise behind it, and the
-- statement either reserves a slot or matches no row.
--
-- ── What this deliberately does NOT do ───────────────────────────────────────
--
-- It makes the **request** ceiling exact. It does not make the monthly *token*
-- and *cost* ceilings exact, and that is a property of the world rather than an
-- omission: token counts do not exist until the provider has answered, so those
-- limits are necessarily checked against the last known total. They can
-- overshoot by at most the number of requests in flight. `KEY: this is stated
-- in RELEASE_READINESS R04 and in the gateway docstring` rather than left for
-- somebody to discover.
--
-- ── Semantics this changes, on purpose ───────────────────────────────────────
--
-- A reserved slot counts an *attempt*, not a billable success. A request that
-- reserves and then fails (no provider reachable, say) has still consumed one
-- of the day's requests. That is the correct behaviour for a rate limit — the
-- work of admitting it was done — and it is the only version that is safe under
-- concurrency. Billing continues to be derived from `model_usage`, which is
-- unchanged.
-- ─────────────────────────────────────────────────────────────────────────────

BEGIN;

ALTER TABLE usage_quotas
    ADD COLUMN IF NOT EXISTS daily_requests_used integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS daily_period_started_at timestamptz NOT NULL
        DEFAULT date_trunc('day', now());

ALTER TABLE usage_quotas
    DROP CONSTRAINT IF EXISTS usage_quotas_daily_requests_used_check;
ALTER TABLE usage_quotas
    ADD CONSTRAINT usage_quotas_daily_requests_used_check
    CHECK (daily_requests_used >= 0);

-- Backfill from the rows that already exist, so an existing tenant does not get
-- a free day's allowance the moment this migration lands. Counts only today's
-- successes, which is exactly what the old `load_quota` query counted.
UPDATE usage_quotas q
SET daily_requests_used = COALESCE((
        SELECT COUNT(*) FROM model_usage u
        WHERE u.tenant_id = q.tenant_id
          AND u.outcome = 'ok'
          AND u.at >= date_trunc('day', now())
    ), 0),
    daily_period_started_at = date_trunc('day', now());

COMMENT ON COLUMN usage_quotas.daily_requests_used IS
    'Slots reserved today. Maintained by the conditional UPDATE in '
    'core.models.store.reserve_daily_request — never by a client SELECT COUNT. '
    'Counts attempts admitted, not billable successes; billing derives from '
    'model_usage.';

COMMENT ON COLUMN usage_quotas.daily_period_started_at IS
    'Start of the day daily_requests_used belongs to. The reservation statement '
    'rolls this forward and resets the counter in the same atomic UPDATE, so no '
    'scheduled job is required and no request can slip through mid-rollover.';

COMMIT;
