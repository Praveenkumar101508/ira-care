-- =============================================================================
-- rollback/045_workspace_columns_down.sql
--
-- Removes the workspace columns, and removes Group B's tenant boundary.
--
-- ── What going back actually costs ───────────────────────────────────────────
--
-- Dropping `workspace_id` is cheap: no policy reads it and no constraint
-- outside `046` depends on it, which is why `046` comes off first.
--
-- Dropping Group B's `tenant_id` is NOT cheap in the same way. It re-opens
-- R-64: `agents`, `calendar_events`, `decisions`, `edges`, `entities`,
-- `projects` and `reminders` return to having no tenant boundary, no RLS and no
-- policies at all. That is the state they were in for the whole of batches 0–5A
-- — survivable only because the install has one owner. An operator running this
-- should know they are removing protection, not just a column.
--
-- Run `046`'s rollback first. Run as `ira_schema_owner` (R-59).
-- =============================================================================

BEGIN;

DO $$
DECLARE
    t text;
    group_a text[] := ARRAY['commitments','companion_versions','companions','conversations',
                            'files','lifeos_events','memory_embeddings','messages','people','tasks'];
    group_b text[] := ARRAY['agents','calendar_events','decisions','edges',
                            'entities','projects','reminders'];
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'edges_src_within_workspace') THEN
        RAISE EXCEPTION 'Run rollback/046_workspace_constraints_down.sql first — '
                        'its composite foreign keys reference workspace_id.';
    END IF;

    -- Group B: policies before RLS, always. Disabling RLS while policies remain
    -- leaves rules attached to a table that is not consulting them, which reads
    -- as protection in `pg_policy` and is none.
    FOREACH t IN ARRAY group_b
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS tenant_select ON %I', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_insert ON %I', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_update ON %I', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_delete ON %I', t);
        EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I DISABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP INDEX IF EXISTS idx_%s_workspace', t);
        EXECUTE format('ALTER TABLE %I DROP COLUMN IF EXISTS workspace_id', t);
        EXECUTE format('ALTER TABLE %I DROP COLUMN IF EXISTS created_by', t);
        EXECUTE format('ALTER TABLE %I DROP COLUMN IF EXISTS tenant_id', t);
    END LOOP;

    -- Group A keeps its tenant boundary — 045 did not create it and this must
    -- not remove it.
    FOREACH t IN ARRAY group_a
    LOOP
        EXECUTE format('DROP INDEX IF EXISTS idx_%s_workspace', t);
        EXECUTE format('ALTER TABLE %I DROP COLUMN IF EXISTS workspace_id', t);
    END LOOP;
END
$$;

COMMIT;
