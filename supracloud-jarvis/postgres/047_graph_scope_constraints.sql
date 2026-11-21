-- =============================================================================
-- 047_graph_scope_constraints.sql — Phase 3B Batch 5C, R-68
--
-- `entities` and `edges` still carry GLOBAL uniqueness. `046` rescoped
-- `agents.name`, `calendar_events.external_id` and `projects.slug` and did not
-- reach these two, so the Life Graph is the last place where one tenant's
-- choice of a name constrains another's.
--
-- ── Why this is a security defect and not a modelling preference ─────────────
--
-- `idx_entities_type_name UNIQUE (type, name)` means tenant A creating
-- ("person", "Alice") stops tenant B creating theirs. That alone is an
-- availability and enumeration problem. The sharper edge is `upsert_entity`:
--
--     INSERT INTO entities (type, name, ...) VALUES (...)
--     ON CONFLICT (type, name) DO UPDATE SET description = EXCLUDED.description
--
-- Under a global constraint, B's upsert of ("person", "Alice") conflicts with
-- A's row and UPDATES IT. B overwrites A's description — a cross-tenant WRITE,
-- through a constraint rather than through a policy.
--
-- **RLS cannot prevent this.** A unique index is checked against the physical
-- heap, not against the rows a policy makes visible. PostgreSQL detects the
-- conflict on a row the writer cannot see, and `DO UPDATE` then modifies it.
-- The policy is not consulted for the conflict detection at all. So the fix has
-- to be the constraint; there is no arrangement of policies that closes it.
--
-- Once workspace RLS lands, the same defect changes shape rather than going
-- away: B's INSERT fails with a unique violation naming a row B cannot see or
-- diagnose. Either way it has to be rescoped BEFORE the policy stage.
--
-- ── NULLS NOT DISTINCT, and why it is load-bearing here ─────────────────────
--
-- `workspace_id` is still nullable during the transition. Under the default
-- NULLS DISTINCT, every row with a NULL workspace would be unique regardless
-- of type and name — the constraint would stop constraining exactly the rows
-- that have not been backfilled yet, which is the population most likely to
-- collide. `046` made the same choice for the same reason.
--
-- Rollback: rollback/047_graph_scope_constraints_down.sql
-- =============================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Preflight — refuse rather than resolve
--
--    A duplicate under the NEW key means two rows in ONE workspace claim the
--    same identity. That is not something a migration may quietly merge: the
--    descriptions differ, the edges pointing at each differ, and choosing a
--    survivor is a decision about somebody's data.
--
--    So this reports the tenant, the workspace and the colliding key, and
--    stops. Nothing is deleted, merged or renamed.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    dupes text;
BEGIN
    SELECT string_agg(
               format('tenant=%s workspace=%s type=%L name=%L (%s rows)',
                      COALESCE(tenant_id::text, 'NULL'),
                      COALESCE(workspace_id::text, 'NULL'),
                      type, name, n),
               E'\n  ')
      INTO dupes
      FROM (
        SELECT tenant_id, workspace_id, type, name, count(*) AS n
          FROM entities
         GROUP BY tenant_id, workspace_id, type, name
        HAVING count(*) > 1
         LIMIT 20) d;

    IF dupes IS NOT NULL THEN
        RAISE EXCEPTION
            E'Refusing to create the scoped UNIQUE on entities: these (tenant, workspace, type, name) keys are held more than once:\n  %', dupes
            USING HINT =
                'Two rows in ONE workspace claim the same entity identity. '
                'Merging them is a decision about which description, which '
                'edges and which history survive — that is yours to make, by '
                'hand, before this migration can run. Nothing has been '
                'changed.';
    END IF;
END
$$;

DO $$
DECLARE
    dupes text;
BEGIN
    SELECT string_agg(
               format('tenant=%s workspace=%s %s->%s rel=%L (%s rows)',
                      COALESCE(tenant_id::text, 'NULL'),
                      COALESCE(workspace_id::text, 'NULL'),
                      src_entity_id, dst_entity_id, relation, n),
               E'\n  ')
      INTO dupes
      FROM (
        SELECT tenant_id, workspace_id, src_entity_id, dst_entity_id,
               relation, count(*) AS n
          FROM edges
         GROUP BY tenant_id, workspace_id, src_entity_id, dst_entity_id, relation
        HAVING count(*) > 1
         LIMIT 20) d;

    IF dupes IS NOT NULL THEN
        RAISE EXCEPTION
            E'Refusing to create the scoped UNIQUE on edges: these (tenant, workspace, src, dst, relation) keys are held more than once:\n  %', dupes
            USING HINT =
                'Duplicate edges differ only in weight and id. Deciding which '
                'weight is correct is a data decision, not a migration one. '
                'Nothing has been changed.';
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Replace the indexes
--
--    NOT `CREATE INDEX CONCURRENTLY`: that cannot run inside a transaction
--    block, and this whole migration is one transaction on purpose. If the
--    scoped index cannot be built, the global one must still be there when the
--    dust settles — a table with neither would accept the duplicates both
--    exist to prevent, and the window would be invisible.
--
--    Order matters: the new index is created BEFORE the old one is dropped, so
--    there is no instant at which the table is unconstrained.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_scope_type_name
    ON entities (tenant_id, workspace_id, type, name) NULLS NOT DISTINCT;

DROP INDEX IF EXISTS idx_entities_type_name;

CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_scope_src_dst_relation
    ON edges (tenant_id, workspace_id, src_entity_id, dst_entity_id, relation)
    NULLS NOT DISTINCT;

DROP INDEX IF EXISTS idx_edges_src_dst_relation;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Assert the result rather than assuming it
--
--    `CREATE INDEX IF NOT EXISTS` is silent when an index of that NAME already
--    exists, whatever its definition. A previous partial run could therefore
--    leave a differently-shaped index wearing the right name, and the migration
--    would report success. This checks the columns.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    missing text := '';
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE indexname = 'idx_entities_scope_type_name'
           AND indexdef LIKE '%tenant_id, workspace_id, type, name%'
           AND indexdef LIKE '%UNIQUE%') THEN
        missing := missing || ' idx_entities_scope_type_name';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE indexname = 'idx_edges_scope_src_dst_relation'
           AND indexdef LIKE '%src_entity_id, dst_entity_id, relation%'
           AND indexdef LIKE '%UNIQUE%') THEN
        missing := missing || ' idx_edges_scope_src_dst_relation';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_entities_type_name') THEN
        missing := missing || ' (global entities index still present)';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_edges_src_dst_relation') THEN
        missing := missing || ' (global edges index still present)';
    END IF;

    IF missing <> '' THEN
        RAISE EXCEPTION 'Migration 047 did not reach its intended state:%', missing;
    END IF;
END
$$;

-- Forced RLS is untouched by this migration and must stay that way; asserted
-- rather than trusted, because dropping and recreating indexes on a table is
-- exactly the kind of operation that gets done with the protections off.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['entities', 'edges']
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_class
             WHERE oid = t::regclass AND relrowsecurity AND relforcerowsecurity) THEN
            RAISE EXCEPTION
                'Table % lost ENABLE/FORCE ROW LEVEL SECURITY during 047', t;
        END IF;
    END LOOP;
END
$$;

COMMENT ON INDEX idx_entities_scope_type_name IS
    'R-68. Entity identity is unique WITHIN a workspace, not globally. A global '
    'key here let one tenant''s ON CONFLICT DO UPDATE overwrite another''s row, '
    'which RLS cannot prevent: a unique index is checked against the heap, not '
    'against the rows a policy makes visible.';

COMMENT ON INDEX idx_edges_scope_src_dst_relation IS
    'R-68. Edge identity is unique WITHIN a workspace. NULLS NOT DISTINCT while '
    'workspace_id is nullable, so un-backfilled rows are still constrained.';

COMMIT;
