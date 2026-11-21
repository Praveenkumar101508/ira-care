-- =============================================================================
-- rollback/046_workspace_constraints_down.sql
--
-- Restores the three GLOBAL unique constraints and drops the composite foreign
-- keys.
--
-- ── This rollback REFUSES rather than destroying data ────────────────────────
--
-- `UNIQUE (tenant_id, workspace_id, name)` permits two tenants to hold an agent
-- called `research`. `UNIQUE (name)` does not. So if two rows now share a name
-- across tenants or workspaces, restoring the global constraint cannot succeed
-- — and the only ways to make it succeed are to delete somebody's row or to
-- rename it. Both are silent data loss performed by a script nobody asked to
-- edit their data.
--
-- So this refuses, and names the collisions. An operator who genuinely wants to
-- go back decides what happens to those rows, deliberately and by hand. Same
-- shape as `044`'s rollback, which refuses when two tenants share a
-- calibration domain.
--
-- Run as `ira_schema_owner`. As `ira_migrator` without `SET ROLE` this fails
-- with `must be owner of table` (R-59).
-- =============================================================================

BEGIN;

DO $$
DECLARE
    spec RECORD;
    dupes text;
BEGIN
    FOR spec IN
        SELECT * FROM (VALUES
            ('agents', 'name'), ('calendar_events', 'external_id'), ('projects', 'slug')
        ) AS t(tbl, col)
    LOOP
        EXECUTE format(
            'SELECT string_agg(v || '' ('' || n || '' rows)'', '', '') FROM ('
            '  SELECT %I::text AS v, count(*) AS n FROM %I'
            '   GROUP BY %I HAVING count(*) > 1 LIMIT 20) d',
            spec.col, spec.tbl, spec.col)
        INTO dupes;

        IF dupes IS NOT NULL THEN
            RAISE EXCEPTION
                'Refusing to restore the global UNIQUE (%) on %: these values are '
                'held more than once and a global constraint cannot express that: %',
                spec.col, spec.tbl, dupes
                USING HINT = 'Restoring UNIQUE (' || spec.col || ') would require '
                             'deleting or renaming somebody''s rows. Decide what '
                             'happens to them first, by hand, then re-run this '
                             'rollback. Batch 5B rescoped this constraint because '
                             'a global name on a tenant-chosen string is a silent '
                             'cross-tenant overwrite (R-65), not because it was '
                             'merely untidy.';
        END IF;
    END LOOP;
END
$$;

-- Composite foreign keys first: they depend on the scope unique constraints.
ALTER TABLE edges     DROP CONSTRAINT IF EXISTS edges_src_within_workspace;
ALTER TABLE edges     DROP CONSTRAINT IF EXISTS edges_dst_within_workspace;
ALTER TABLE reminders DROP CONSTRAINT IF EXISTS reminders_task_within_workspace;

ALTER TABLE entities DROP CONSTRAINT IF EXISTS entities_scope_key;
ALTER TABLE tasks    DROP CONSTRAINT IF EXISTS tasks_scope_key;

DROP VIEW IF EXISTS batch5b_workspace_scope_status;

ALTER TABLE agents          DROP CONSTRAINT IF EXISTS agents_tenant_workspace_name_key;
ALTER TABLE calendar_events DROP CONSTRAINT IF EXISTS calendar_events_tenant_workspace_external_id_key;
ALTER TABLE projects        DROP CONSTRAINT IF EXISTS projects_tenant_workspace_slug_key;

ALTER TABLE agents          ADD CONSTRAINT agents_name_key                 UNIQUE (name);
ALTER TABLE calendar_events ADD CONSTRAINT calendar_events_external_id_key UNIQUE (external_id);
ALTER TABLE projects        ADD CONSTRAINT projects_slug_key               UNIQUE (slug);

COMMIT;
