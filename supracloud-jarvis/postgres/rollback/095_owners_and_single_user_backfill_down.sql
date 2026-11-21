-- =============================================================================
-- rollback/095_owners_and_single_user_backfill_down.sql
--
-- Undoes 095 exactly:
--
--   * every Group B author 095 attributed, listed in `attribution_backfill_095`,
--     goes back to NULL, but only where it still holds the value 095 wrote. An
--     author a writer recorded, or one changed since, is left alone;
--   * the record table is dropped;
--   * the five `owner_user_id` columns are dropped, and the Group C backfill
--     with them.
--
-- What it loses: owners recorded on Group C rows after 095, by the writers.
-- Nothing else is deleted. A database that never ran 095 has no record table
-- and no columns; every step is IF EXISTS, so this is a no-op there.
--
-- Same RLS window as 095, for the same reason.
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.attribution_backfill_095') IS NULL THEN
        RETURN;
    END IF;

    ALTER TABLE projects             NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE agents               NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE decisions            NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE calendar_events      NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE reminders            NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE entities             NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE edges                NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE business_events      NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE strategy_predictions NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE strategy_calibration NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE model_performance    NO FORCE ROW LEVEL SECURITY;

    UPDATE projects t SET created_by = NULL FROM attribution_backfill_095 b
     WHERE b.table_name = 'projects' AND b.row_key = t.id::text AND t.created_by = b.attributed_to;
    UPDATE agents t SET created_by = NULL FROM attribution_backfill_095 b
     WHERE b.table_name = 'agents' AND b.row_key = t.id::text AND t.created_by = b.attributed_to;
    UPDATE decisions t SET created_by = NULL FROM attribution_backfill_095 b
     WHERE b.table_name = 'decisions' AND b.row_key = t.id::text AND t.created_by = b.attributed_to;
    UPDATE calendar_events t SET created_by = NULL FROM attribution_backfill_095 b
     WHERE b.table_name = 'calendar_events' AND b.row_key = t.id::text AND t.created_by = b.attributed_to;
    UPDATE reminders t SET created_by = NULL FROM attribution_backfill_095 b
     WHERE b.table_name = 'reminders' AND b.row_key = t.id::text AND t.created_by = b.attributed_to;
    UPDATE entities t SET created_by = NULL FROM attribution_backfill_095 b
     WHERE b.table_name = 'entities' AND b.row_key = t.id::text AND t.created_by = b.attributed_to;
    UPDATE edges t SET created_by = NULL FROM attribution_backfill_095 b
     WHERE b.table_name = 'edges' AND b.row_key = t.id::text AND t.created_by = b.attributed_to;
    UPDATE business_events t SET created_by = NULL FROM attribution_backfill_095 b
     WHERE b.table_name = 'business_events' AND b.row_key = t.id::text AND t.created_by = b.attributed_to;
    UPDATE strategy_predictions t SET created_by = NULL FROM attribution_backfill_095 b
     WHERE b.table_name = 'strategy_predictions' AND b.row_key = t.id::text AND t.created_by = b.attributed_to;
    UPDATE strategy_calibration t SET created_by = NULL FROM attribution_backfill_095 b
     WHERE b.table_name = 'strategy_calibration'
       AND b.row_key = t.tenant_id::text || '/' || t.domain AND t.created_by = b.attributed_to;
    UPDATE model_performance t SET created_by = NULL FROM attribution_backfill_095 b
     WHERE b.table_name = 'model_performance' AND b.row_key = t.id::text AND t.created_by = b.attributed_to;

    ALTER TABLE projects             FORCE ROW LEVEL SECURITY;
    ALTER TABLE agents               FORCE ROW LEVEL SECURITY;
    ALTER TABLE decisions            FORCE ROW LEVEL SECURITY;
    ALTER TABLE calendar_events      FORCE ROW LEVEL SECURITY;
    ALTER TABLE reminders            FORCE ROW LEVEL SECURITY;
    ALTER TABLE entities             FORCE ROW LEVEL SECURITY;
    ALTER TABLE edges                FORCE ROW LEVEL SECURITY;
    ALTER TABLE business_events      FORCE ROW LEVEL SECURITY;
    ALTER TABLE strategy_predictions FORCE ROW LEVEL SECURITY;
    ALTER TABLE strategy_calibration FORCE ROW LEVEL SECURITY;
    ALTER TABLE model_performance    FORCE ROW LEVEL SECURITY;
END $$;

DROP TABLE IF EXISTS attribution_backfill_095;

ALTER TABLE tasks              DROP COLUMN IF EXISTS owner_user_id;
ALTER TABLE people             DROP COLUMN IF EXISTS owner_user_id;
ALTER TABLE notifications      DROP COLUMN IF EXISTS owner_user_id;
ALTER TABLE briefings          DROP COLUMN IF EXISTS owner_user_id;
ALTER TABLE heartbeat_surfaced DROP COLUMN IF EXISTS owner_user_id;

COMMIT;
