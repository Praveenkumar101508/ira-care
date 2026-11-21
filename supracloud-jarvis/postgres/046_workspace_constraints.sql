-- =============================================================================
-- 046_workspace_constraints.sql — Phase 3B Batch 5B (part 2 of 2)
--
-- The irreversible half, kept in its own file so it stays separately revertable.
-- `045` adds columns; dropping a column undoes that. This moves unique
-- constraints and adds composite foreign keys, and neither is undone by a
-- column drop — the same reason Batch 4 split `043` from `044`.
--
-- ── R-65: three global unique constraints on names customers choose ──────────
--
--     agents           UNIQUE (name)
--     calendar_events  UNIQUE (external_id)
--     projects         UNIQUE (slug)
--
-- Batch 4's rule is what found these: **read the write path, not the
-- constraint.** All three are upserted:
--
--     agents/creator.py:115     ON CONFLICT (name)        DO UPDATE
--     tasks/calendar.py:73      ON CONFLICT (external_id) DO UPDATE
--     tasks/calendar.py:176     ON CONFLICT (external_id) DO NOTHING
--     commands/executor.py:84   ON CONFLICT (slug)        DO UPDATE
--     commands/executor.py:166  ON CONFLICT (slug)        DO UPDATE
--
-- So a second tenant does not get a unique violation. `DO UPDATE` **overwrites**
-- the first tenant's row — for `agents`, that is one customer's executable
-- `code` and `docker_config` replaced by another's. `DO NOTHING` fails the
-- other way and is easier to miss: the second tenant's calendar event simply
-- never appears, with no error and no log line.
--
-- Same defect class as `totp_secrets PRIMARY KEY (username)` (R-26) and
-- `strategy_calibration PRIMARY KEY (domain)` (Batch 4).
--
-- ── Why the workspace is in the key, not just the tenant ─────────────────────
--
-- `agents.name` and `projects.slug` are names a user types. Two workspaces in
-- one tenant — "Engineering" and "Marketing" — must both be able to hold a
-- project called `launch`. Scoping only to the tenant would make the first
-- workspace to use a name take it from the rest of the tenant, which is R-42
-- one level down rather than fixed.
--
-- `calendar_events.external_id` is the strongest case: it is issued by the
-- customer's calendar provider and is unique only within that provider account.
-- Two workspaces connecting two different Google accounts can legitimately
-- produce the same `external_id`.
--
-- ── NULLS NOT DISTINCT, and why it matters right now ─────────────────────────
--
-- `workspace_id` is nullable until Batch 5C. PostgreSQL treats NULLs as
-- distinct in a unique index by default, so `UNIQUE (tenant_id, workspace_id,
-- name)` would let a tenant create *unlimited* agents called `research` as long
-- as their workspace is NULL — which is every row the application writes today,
-- because nothing binds `app.workspace_id` yet.
--
-- That would be strictly worse than the global constraint this replaces: it
-- would remove the old protection and supply none. `NULLS NOT DISTINCT`
-- (PostgreSQL 15+) treats the NULLs as equal, so uniqueness holds within the
-- tenant throughout the transitional window and tightens to the workspace once
-- 5C populates the column.
--
-- Rollback: rollback/046_workspace_constraints_down.sql — which REFUSES rather
-- than silently destroying data. See that file.
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Rescope the three global unique constraints
--
--    Guarded on the CURRENT definition rather than the constraint name, so a
--    re-run finds the work already done and does nothing. Matching on the name
--    alone would drop and recreate a unique index on every apply, which on a
--    large table is a rebuild.
--
--    The old constraint must be DROPPED, not supplemented. Adding
--    `UNIQUE (tenant_id, workspace_id, name)` alongside `UNIQUE (name)` changes
--    nothing: the old one still refuses the second tenant. There is no additive
--    version of this fix.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    spec RECORD;
BEGIN
    FOR spec IN
        SELECT * FROM (VALUES
            ('agents',          'agents_name_key',                  'name'),
            ('calendar_events', 'calendar_events_external_id_key',  'external_id'),
            ('projects',        'projects_slug_key',                'slug')
        ) AS t(tbl, oldname, col)
    LOOP
        IF EXISTS (
            SELECT 1 FROM pg_constraint c
              JOIN pg_class r ON r.oid = c.conrelid
             WHERE r.relname = spec.tbl AND c.contype = 'u'
               AND pg_get_constraintdef(c.oid) = format('UNIQUE (%s)', spec.col)
        ) THEN
            EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', spec.tbl, spec.oldname);
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint c
              JOIN pg_class r ON r.oid = c.conrelid
             WHERE r.relname = spec.tbl AND c.contype = 'u'
               AND c.conname = spec.tbl || '_tenant_workspace_' || spec.col || '_key'
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I ADD CONSTRAINT %I '
                'UNIQUE NULLS NOT DISTINCT (tenant_id, workspace_id, %I)',
                spec.tbl, spec.tbl || '_tenant_workspace_' || spec.col || '_key', spec.col);
        END IF;
    END LOOP;
END
$$;

-- The application upserts on these keys, so `ON CONFLICT (name)` must become
-- `ON CONFLICT (tenant_id, workspace_id, name)` — otherwise PostgreSQL cannot
-- infer an arbiter index and the statement fails outright. That is the GOOD
-- failure: a loud error at the call site rather than a silent cross-tenant
-- overwrite. `agents/creator.py`, `tasks/calendar.py` and
-- `commands/executor.py` are updated in the same batch.

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Composite foreign keys — a relationship must not span workspaces
--
--    Giving `edges` and `entities` a workspace each is not sufficient. Nothing
--    yet stops an edge whose `src_entity_id` lives in one workspace and whose
--    `dst_entity_id` lives in another: that is a cross-workspace join expressed
--    as data, and it would survive any policy written on the edge row alone,
--    because the edge row itself looks perfectly well scoped.
--
--    The fix is the shape `040` used for
--    `workspace_memberships_requires_tenant_membership`: carry the scope
--    columns into the foreign key so the mismatch is unrepresentable rather
--    than merely discouraged.
--
--    **Honest limit, stated because it would otherwise be assumed away.** These
--    are MATCH SIMPLE (the default), so while `workspace_id` is NULL the
--    constraint is not enforced for that row. Every row the backfill touched
--    has a workspace and is covered; rows written before 5C binds the GUC have
--    NULL and are not. The constraint becomes fully enforcing when 5C makes the
--    column NOT NULL — it is being built now so the shape is reviewed and
--    tested here rather than bolted on during the enforcement batch.
-- ─────────────────────────────────────────────────────────────────────────────

-- A composite FK needs a matching unique constraint on the referenced side.
-- `(id)` is already the primary key, so these add nothing semantically — they
-- exist purely as FK targets.
ALTER TABLE entities ADD CONSTRAINT entities_scope_key UNIQUE (tenant_id, workspace_id, id);
ALTER TABLE tasks    ADD CONSTRAINT tasks_scope_key    UNIQUE (tenant_id, workspace_id, id);

ALTER TABLE edges
    ADD CONSTRAINT edges_src_within_workspace
    FOREIGN KEY (tenant_id, workspace_id, src_entity_id)
    REFERENCES entities (tenant_id, workspace_id, id) ON DELETE CASCADE;

ALTER TABLE edges
    ADD CONSTRAINT edges_dst_within_workspace
    FOREIGN KEY (tenant_id, workspace_id, dst_entity_id)
    REFERENCES entities (tenant_id, workspace_id, id) ON DELETE CASCADE;

ALTER TABLE reminders
    ADD CONSTRAINT reminders_task_within_workspace
    FOREIGN KEY (tenant_id, workspace_id, task_id)
    REFERENCES tasks (tenant_id, workspace_id, id) ON DELETE CASCADE;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Verification view
--
--    `security_invoker`, for the reason `036`'s status view had to learn: a view
--    runs as its OWNER, and the owner is subject to FORCE, so a default view
--    reports what the schema owner can see — nothing — to every caller,
--    including a superuser.
--
--    `contract_ready` is deliberately NOT a bare `count = 0`. On an empty table
--    that reads true and says nothing. Emptiness is its own column so a reader
--    cannot mistake "nothing to do" for "done".
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW batch5b_workspace_scope_status
WITH (security_invoker = true) AS
SELECT s.table_name, s.total_rows, s.no_tenant, s.no_workspace,
       s.no_tenant = 0    AS tenant_ready,
       s.no_workspace = 0 AS workspace_backfilled,
       s.total_rows = 0   AS vacuous
FROM (
    SELECT 'agents'::text AS table_name, count(*) AS total_rows,
           count(*) FILTER (WHERE tenant_id IS NULL) AS no_tenant,
           count(*) FILTER (WHERE workspace_id IS NULL) AS no_workspace FROM agents
    UNION ALL SELECT 'calendar_events', count(*), count(*) FILTER (WHERE tenant_id IS NULL), count(*) FILTER (WHERE workspace_id IS NULL) FROM calendar_events
    UNION ALL SELECT 'commitments', count(*), count(*) FILTER (WHERE tenant_id IS NULL), count(*) FILTER (WHERE workspace_id IS NULL) FROM commitments
    UNION ALL SELECT 'companion_versions', count(*), count(*) FILTER (WHERE tenant_id IS NULL), count(*) FILTER (WHERE workspace_id IS NULL) FROM companion_versions
    UNION ALL SELECT 'companions', count(*), count(*) FILTER (WHERE tenant_id IS NULL), count(*) FILTER (WHERE workspace_id IS NULL) FROM companions
    UNION ALL SELECT 'conversations', count(*), count(*) FILTER (WHERE tenant_id IS NULL), count(*) FILTER (WHERE workspace_id IS NULL) FROM conversations
    UNION ALL SELECT 'decisions', count(*), count(*) FILTER (WHERE tenant_id IS NULL), count(*) FILTER (WHERE workspace_id IS NULL) FROM decisions
    UNION ALL SELECT 'edges', count(*), count(*) FILTER (WHERE tenant_id IS NULL), count(*) FILTER (WHERE workspace_id IS NULL) FROM edges
    UNION ALL SELECT 'entities', count(*), count(*) FILTER (WHERE tenant_id IS NULL), count(*) FILTER (WHERE workspace_id IS NULL) FROM entities
    UNION ALL SELECT 'files', count(*), count(*) FILTER (WHERE tenant_id IS NULL), count(*) FILTER (WHERE workspace_id IS NULL) FROM files
    UNION ALL SELECT 'lifeos_events', count(*), count(*) FILTER (WHERE tenant_id IS NULL), count(*) FILTER (WHERE workspace_id IS NULL) FROM lifeos_events
    UNION ALL SELECT 'memory_embeddings', count(*), count(*) FILTER (WHERE tenant_id IS NULL), count(*) FILTER (WHERE workspace_id IS NULL) FROM memory_embeddings
    UNION ALL SELECT 'messages', count(*), count(*) FILTER (WHERE tenant_id IS NULL), count(*) FILTER (WHERE workspace_id IS NULL) FROM messages
    UNION ALL SELECT 'people', count(*), count(*) FILTER (WHERE tenant_id IS NULL), count(*) FILTER (WHERE workspace_id IS NULL) FROM people
    UNION ALL SELECT 'projects', count(*), count(*) FILTER (WHERE tenant_id IS NULL), count(*) FILTER (WHERE workspace_id IS NULL) FROM projects
    UNION ALL SELECT 'reminders', count(*), count(*) FILTER (WHERE tenant_id IS NULL), count(*) FILTER (WHERE workspace_id IS NULL) FROM reminders
    UNION ALL SELECT 'tasks', count(*), count(*) FILTER (WHERE tenant_id IS NULL), count(*) FILTER (WHERE workspace_id IS NULL) FROM tasks
) s;

COMMENT ON VIEW batch5b_workspace_scope_status IS
    'Batch 5B state for the 17 workspace-scoped tables. `tenant_ready` is the '
    'contract Batch 5B owes. `workspace_backfilled` says the column is '
    'populated — it does NOT mean workspace isolation is enforced: no policy '
    'reads app.workspace_id until Batch 5C. `vacuous` is true on an empty '
    'table, so a green row cannot be mistaken for a finished migration.';

COMMENT ON CONSTRAINT edges_src_within_workspace ON edges IS
    'An edge and both its endpoints must share a workspace. MATCH SIMPLE, so it '
    'is not enforced while workspace_id is NULL — fully enforcing once Batch 5C '
    'makes the column NOT NULL.';
COMMENT ON CONSTRAINT agents_tenant_workspace_name_key ON agents IS
    'NULLS NOT DISTINCT: workspace_id is NULL until Batch 5C, and without this '
    'a tenant could create unlimited agents sharing a name. Uniqueness holds '
    'within the tenant now and tightens to the workspace once 5C populates it.';
