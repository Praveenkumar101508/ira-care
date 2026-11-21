-- Rollback for 043_tenant_scope_operational_content.sql (Phase 3B Batch 4A).
--
-- Removes the tenant columns and their policies from business_events,
-- model_performance and strategy_predictions.
--
-- **This restores a schema with no tenant isolation on three tables holding
-- customer data.** Rows written since the migration keep their content; the
-- column recording who they belong to is dropped, and that attribution is not
-- recoverable afterwards. Run it only to unblock a failed deployment, and
-- re-apply immediately.
--
-- Policies are dropped BEFORE row-level security is disabled. The other order
-- leaves a window in which RLS is off and the policies still exist, which reads
-- as protected in `pg_policies` while protecting nothing.

DROP VIEW IF EXISTS batch4_tenant_scope_status;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['business_events', 'model_performance', 'strategy_predictions']
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS tenant_select ON %I', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_insert ON %I', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_update ON %I', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_delete ON %I', t);
        EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I DISABLE ROW LEVEL SECURITY', t);
    END LOOP;
END
$$;

DROP INDEX IF EXISTS idx_business_events_tenant;
DROP INDEX IF EXISTS idx_model_performance_tenant;
DROP INDEX IF EXISTS idx_strategy_predictions_tenant;

ALTER TABLE business_events      DROP COLUMN IF EXISTS created_by;
ALTER TABLE model_performance    DROP COLUMN IF EXISTS created_by;
ALTER TABLE strategy_predictions DROP COLUMN IF EXISTS created_by;

ALTER TABLE business_events      DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE model_performance    DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE strategy_predictions DROP COLUMN IF EXISTS tenant_id;
