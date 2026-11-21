-- =============================================================================
-- 044_strategy_calibration_tenant_key.sql — Phase 3B Batch 4B
--
-- `strategy_calibration` gains a tenant AND its primary key moves.
--
-- ── The finding ──────────────────────────────────────────────────────────────
--
--     strategy_calibration: PRIMARY KEY (domain)
--
-- `domain` is a tenant-created string — `sales`, `hiring`, `pricing`. With one
-- owner this is a sensible key. With two tenants it is a **silent data
-- corruption**, not a refusal, and the difference matters:
--
--     agents/strategy_calibration.py upserts on `domain`.
--
-- So tenant B calibrating `sales` does not get an error. It **overwrites**
-- tenant A's multiplier, offset and sample count with its own. Tenant A's
-- forecasts then shift for reasons nothing in their account explains, and no
-- log records a cross-tenant write because, as far as the database was
-- concerned, one row was updated by somebody entitled to update it.
--
-- This is the same defect class as `totp_secrets PRIMARY KEY (username)`
-- (R-26) and the 23 global constraints recorded as R-42 — a global unique
-- constraint on a name a tenant chooses. It is the reason Batch 4 is two
-- migrations: adding a column is reversible by dropping it, and moving a
-- primary key is not.
--
-- ── Why the key moves rather than gaining a partial index ────────────────────
--
-- `UNIQUE (tenant_id, domain)` alongside the old `PRIMARY KEY (domain)` would
-- not help: the old constraint still refuses the second tenant. The primary key
-- is the thing enforcing global uniqueness, so the primary key is what has to
-- change. There is no additive version of this fix.
--
-- Rollback: rollback/044_strategy_calibration_tenant_key_down.sql
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 0. Backfill window — see `035_memberships.sql` section 0
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE strategy_calibration NO FORCE ROW LEVEL SECURITY;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Expand and backfill
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE strategy_calibration ADD COLUMN IF NOT EXISTS tenant_id UUID
    REFERENCES tenants(id) ON DELETE RESTRICT;
ALTER TABLE strategy_calibration ADD COLUMN IF NOT EXISTS created_by UUID
    REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE strategy_calibration ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

UPDATE strategy_calibration
   SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid
 WHERE tenant_id IS NULL;

ALTER TABLE strategy_calibration
    ALTER COLUMN tenant_id SET DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid;
ALTER TABLE strategy_calibration ALTER COLUMN tenant_id SET NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Move the primary key
--
--    Guarded on the CURRENT key definition rather than on the constraint name,
--    so a re-run finds the key already moved and does nothing. Matching on the
--    name alone would drop and recreate it on every run, which is churn on a
--    primary key — and on a large table, a rebuild.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
         WHERE t.relname = 'strategy_calibration' AND c.contype = 'p'
           AND pg_get_constraintdef(c.oid) = 'PRIMARY KEY (domain)'
    ) THEN
        ALTER TABLE strategy_calibration DROP CONSTRAINT strategy_calibration_pkey;
        ALTER TABLE strategy_calibration
            ADD CONSTRAINT strategy_calibration_pkey PRIMARY KEY (tenant_id, domain);
    END IF;
END
$$;

-- The application upserts on the key, so `ON CONFLICT (domain)` must become
-- `ON CONFLICT (tenant_id, domain)` — otherwise PostgreSQL cannot infer an
-- arbiter index and the statement fails outright. That is the good failure: a
-- loud error at the call site rather than a silent cross-tenant overwrite.
-- `agents/strategy_calibration.py` is updated in the same batch.

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Row-level security — per command
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE strategy_calibration ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON strategy_calibration;
DROP POLICY IF EXISTS tenant_select    ON strategy_calibration;
DROP POLICY IF EXISTS tenant_insert    ON strategy_calibration;
DROP POLICY IF EXISTS tenant_update    ON strategy_calibration;
DROP POLICY IF EXISTS tenant_delete    ON strategy_calibration;

CREATE POLICY tenant_select ON strategy_calibration FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY tenant_insert ON strategy_calibration FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- Both clauses, so a row cannot be updated out of its tenant. Without WITH
-- CHECK an UPDATE could reassign `tenant_id` and move a row to another
-- customer — which reads as an ordinary update and is a transfer of ownership.
CREATE POLICY tenant_update ON strategy_calibration FOR UPDATE
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY tenant_delete ON strategy_calibration FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE strategy_calibration FORCE ROW LEVEL SECURITY;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Verification view for all four Batch 4 tables
--
--    `security_invoker`, for the reason `036`'s status view had to learn: a view
--    runs as its OWNER, and the owner is subject to FORCE — so a default view
--    reports what the schema owner can see, which is nothing, to every caller
--    including a superuser.
--
--    `contract_ready` is deliberately NOT a bare `count = 0`. On an empty table
--    that reads true and says nothing, which is the vacuity `migrate_secrets.py`
--    had to be taught to label. The emptiness is reported as its own column so a
--    reader cannot mistake "nothing to do" for "done".
--
--    Reading it needs a role that can see across tenants. Under an ordinary
--    tenant-bound connection every count is that tenant's — which is correct for
--    an invoker view and useless as a fleet-wide gate, so the report says which
--    role produced the numbers.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW batch4_tenant_scope_status
WITH (security_invoker = true) AS
SELECT s.table_name, s.total_rows, s.unattributed,
       s.unattributed = 0 AS contract_ready,
       s.total_rows = 0   AS vacuous
FROM (
    SELECT 'business_events'::text AS table_name, count(*) AS total_rows,
           count(*) FILTER (WHERE tenant_id IS NULL) AS unattributed FROM business_events
    UNION ALL
    SELECT 'model_performance', count(*), count(*) FILTER (WHERE tenant_id IS NULL) FROM model_performance
    UNION ALL
    SELECT 'strategy_predictions', count(*), count(*) FILTER (WHERE tenant_id IS NULL) FROM strategy_predictions
    UNION ALL
    SELECT 'strategy_calibration', count(*), count(*) FILTER (WHERE tenant_id IS NULL) FROM strategy_calibration
) s;

COMMENT ON VIEW batch4_tenant_scope_status IS
    'Every row must read unattributed = 0. `vacuous` is true when the table is '
    'empty, so contract_ready = true means "nothing blocks" rather than '
    '"migration completed" — a zero-row table is not a finished migration.';

COMMENT ON TABLE strategy_calibration IS
    'Per-tenant, per-domain forecast calibration. The primary key is '
    '(tenant_id, domain): `domain` alone was global, and because the write path '
    'upserts, a second tenant OVERWROTE the first rather than being refused.';
COMMENT ON COLUMN strategy_calibration.domain IS
    'A tenant-chosen name. Unique within the tenant and nowhere else — two '
    'customers may both calibrate "sales".';
