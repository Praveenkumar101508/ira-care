-- =============================================================================
-- 043_tenant_scope_operational_content.sql — Phase 3B Batch 4A
--
-- `business_events`, `model_performance` and `strategy_predictions` gain a
-- tenant. Three tables with the same shape of change: add a column, backfill it
-- deterministically, index it, force RLS on it. Nothing here moves a key.
--
-- `strategy_calibration` is deliberately NOT in this file — its primary key has
-- to move, which is a different rollback story. See `044`.
--
-- ── Why these three are tenant content ───────────────────────────────────────
--
-- The classification is not obvious from the names, so the reasoning is worth
-- restating rather than pointing at the manifest.
--
--   business_events       webhook and monitoring events about a customer's
--                         business. Plainly theirs.
--   model_performance     latency and token counts per request. Feels like
--                         platform telemetry, and is not: one tenant's usage
--                         pattern is inferable from an aggregate they can read.
--                         Volume, timing and model choice describe what a
--                         customer is doing.
--   strategy_predictions  the prediction log — questions asked, options
--                         weighed, outcomes recorded. The `question` column is
--                         free text a customer wrote.
--
-- ── The backfill, and what it assumes ────────────────────────────────────────
--
-- Every existing row is attributed to `PRIVATE_TENANT_ID`, the deterministic
-- UUIDv5 tenant that `023_tenancy.sql` seeded. That is sound for exactly one
-- reason: **this schema has only ever run as a single-owner install**, so a row
-- that predates a `tenant_id` column can only have come from that owner.
--
-- It is not a default and must never become one. A row written after this
-- migration gets its tenant from the transaction context or fails the NOT NULL,
-- and §5's verification blocks the contract stage while any row is unattributed.
--
-- Rollback: rollback/043_tenant_scope_operational_content_down.sql
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 0. Backfill window
--
--    None of these three has RLS yet, so nothing is filtered on a first run and
--    these statements are no-ops. They matter on a RE-RUN, where section 4 has
--    already forced RLS: the backfill would then match zero rows *without
--    failing*, and a new row added between runs would keep a NULL tenant.
--
--    Full reasoning in `035_memberships.sql` section 0. `NO FORCE`, never
--    `DISABLE` — RLS stays enabled throughout, so only the owner is exempt and
--    only inside this migration. Restored in section 6.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE business_events      NO FORCE ROW LEVEL SECURITY;
ALTER TABLE model_performance    NO FORCE ROW LEVEL SECURITY;
ALTER TABLE strategy_predictions NO FORCE ROW LEVEL SECURITY;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Expand
--
--    `tenant_id` is nullable for now. Making it NOT NULL before the backfill is
--    verified would fail the migration on the first unattributable row, and the
--    right outcome there is a blocked contract stage with the row visible — not
--    a migration that cannot be applied at all.
--
--    ON DELETE RESTRICT, not CASCADE. Deleting a tenant must be a deliberate
--    operation that names what it is destroying; a cascade from `tenants` would
--    silently take content with it, and the deletion matrix (D-14) requires
--    tenant deletion to be an explicit erasure path rather than a foreign-key
--    side effect.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE business_events      ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE RESTRICT;
ALTER TABLE model_performance    ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE RESTRICT;
ALTER TABLE strategy_predictions ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE RESTRICT;

-- `created_by` is attribution, not authorisation. It is nullable for good:
-- `model_performance` rows are written by a background worker with no user, and
-- inventing one would be a lie in an attribution column. ON DELETE SET NULL,
-- because deleting a person must not delete the tenant's content.
ALTER TABLE business_events      ADD COLUMN IF NOT EXISTS created_by UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE model_performance    ADD COLUMN IF NOT EXISTS created_by UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE strategy_predictions ADD COLUMN IF NOT EXISTS created_by UUID REFERENCES users(id) ON DELETE SET NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Backfill — deterministic, and only for rows that predate the column
-- ─────────────────────────────────────────────────────────────────────────────

UPDATE business_events      SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;
UPDATE model_performance    SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;
UPDATE strategy_predictions SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;

-- `created_by` is deliberately NOT backfilled. Attributing every historical row
-- to the private owner would be a guess that looks authoritative: these rows
-- were written by workers and webhooks, and some of them had no user at all.
-- NULL means "unknown", which is the truth.

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Constrain and index
--
--    The DEFAULT is what makes an INSERT that omits `tenant_id` land in the
--    caller's tenant — the only value the WITH CHECK below would accept anyway.
--    With no tenant bound it resolves to NULL, hits NOT NULL and fails loudly.
--    That is the fail-closed direction: an unscoped write is refused rather than
--    landing somewhere arbitrary.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE business_events      ALTER COLUMN tenant_id SET DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid;
ALTER TABLE model_performance    ALTER COLUMN tenant_id SET DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid;
ALTER TABLE strategy_predictions ALTER COLUMN tenant_id SET DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid;

-- NOT NULL is applied here because the backfill above leaves no NULL behind on
-- any database this chain has ever built — every row is attributable, because
-- every row came from the single owner. Section 5 verifies it; if a future
-- database somehow reached this point with an unattributed row, this statement
-- fails the migration rather than letting the row through, which is the correct
-- direction for a column every policy depends on.
ALTER TABLE business_events      ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE model_performance    ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE strategy_predictions ALTER COLUMN tenant_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_business_events_tenant      ON business_events      (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_model_performance_tenant    ON model_performance    (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_predictions_tenant ON strategy_predictions (tenant_id, created_at DESC);

-- `idx_model_perf_recent` is (model_name, created_at DESC) — a global index on a
-- now tenant-scoped table. It is kept and superseded rather than dropped: a
-- tenant-scoped query uses the new index, and dropping the old one inside the
-- same migration that adds the replacement would leave no way back if a
-- reporting query still depended on it. Batch 9 reviews leftover indexes.

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Row-level security — per command, not FOR ALL
--
--    `038` documents why: a `FOR ALL` policy checks an UPDATE's new row against
--    its USING clause as well as WITH CHECK, which silently couples read and
--    write semantics. Here the four commands genuinely differ in one way that
--    matters — see the UPDATE clause.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['business_events', 'model_performance', 'strategy_predictions']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);

        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_select ON %I', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_insert ON %I', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_update ON %I', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_delete ON %I', t);

        EXECUTE format(
            'CREATE POLICY tenant_select ON %I FOR SELECT '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);

        EXECUTE format(
            'CREATE POLICY tenant_insert ON %I FOR INSERT '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);

        -- USING and WITH CHECK are the SAME predicate here, and that is the
        -- point: a row must belong to the caller's tenant both before and after.
        -- Writing only USING would let an UPDATE move a row into another tenant
        -- — it would pass the read check on the way in and land anywhere. That
        -- is how a tenant steals a row rather than reads one.
        EXECUTE format(
            'CREATE POLICY tenant_update ON %I FOR UPDATE '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);

        EXECUTE format(
            'CREATE POLICY tenant_delete ON %I FOR DELETE '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
    END LOOP;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. The verification view lives in `044`, not here
--
--    It reports all four Batch 4 tables, and `strategy_calibration` does not
--    have a `tenant_id` until `044` adds it — so defining the view here would
--    fail on a column that does not yet exist. Splitting it across two files to
--    match the split of the migrations would give two half-views and a reader
--    two places to look.
-- ─────────────────────────────────────────────────────────────────────────────

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. Close the backfill window
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE business_events      FORCE ROW LEVEL SECURITY;
ALTER TABLE model_performance    FORCE ROW LEVEL SECURITY;
ALTER TABLE strategy_predictions FORCE ROW LEVEL SECURITY;

COMMENT ON COLUMN business_events.tenant_id IS
    'Tenant owner. Backfilled to the private tenant for rows that predate the '
    'column — sound only because this schema has only ever run single-owner. '
    'Never a runtime default: an unscoped INSERT resolves to NULL and fails.';
COMMENT ON COLUMN model_performance.tenant_id IS
    'Tenant owner. Latency and token counts look like platform telemetry and are '
    'not: volume, timing and model choice describe what a customer is doing.';
COMMENT ON COLUMN strategy_predictions.tenant_id IS
    'Tenant owner. `question` is free text a customer wrote.';
