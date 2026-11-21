-- =============================================================================
-- rollback/047_graph_scope_constraints_down.sql
--
-- Restores the GLOBAL uniqueness on `entities` and `edges`.
--
-- ── This rollback REFUSES rather than destroying data ────────────────────────
--
-- Going forward is safe: a global key is stricter than a scoped one, so every
-- row that satisfied `UNIQUE (type, name)` still satisfies
-- `UNIQUE (tenant_id, workspace_id, type, name)`.
--
-- Coming back is not. Once two workspaces each hold ("person", "Alice") —
-- which is the entire point of 047 — restoring the global index would require
-- deleting or renaming one of them. That is a decision about whose row
-- survives, and a rollback script is the worst possible place to make it
-- silently.
--
-- So this reports the collisions and stops. The database stays on the scoped
-- constraints, which is the safe direction: strictly more permissive than the
-- global one and never a cross-tenant overwrite.
--
-- Same shape as rollback/046, and for the same reason.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    dupes text;
BEGIN
    SELECT string_agg(
               format('type=%L name=%L held by %s workspace(s)', type, name, n),
               E'\n  ')
      INTO dupes
      FROM (
        SELECT type, name, count(*) AS n
          FROM entities
         GROUP BY type, name
        HAVING count(*) > 1
         LIMIT 20) d;

    IF dupes IS NOT NULL THEN
        RAISE EXCEPTION
            E'Refusing to restore the global UNIQUE (type, name) on entities: these values are held by more than one workspace:\n  %', dupes
            USING HINT =
                'Restoring a global key would require deleting or renaming '
                'somebody''s entities. 047 rescoped this constraint because a '
                'global key on a tenant-chosen name let one workspace''s upsert '
                'overwrite another''s row (R-68) — not because it was untidy. '
                'Decide what happens to these rows by hand first, or stay on '
                'the scoped constraint, which is the safe direction.';
    END IF;
END
$$;

DO $$
DECLARE
    dupes text;
BEGIN
    SELECT string_agg(
               format('%s->%s rel=%L held by %s workspace(s)',
                      src_entity_id, dst_entity_id, relation, n),
               E'\n  ')
      INTO dupes
      FROM (
        SELECT src_entity_id, dst_entity_id, relation, count(*) AS n
          FROM edges
         GROUP BY src_entity_id, dst_entity_id, relation
        HAVING count(*) > 1
         LIMIT 20) d;

    IF dupes IS NOT NULL THEN
        RAISE EXCEPTION
            E'Refusing to restore the global UNIQUE on edges: these (src, dst, relation) triples are held by more than one workspace:\n  %', dupes
            USING HINT = 'Same reasoning as the entities guard above.';
    END IF;
END
$$;

-- Global index first, scoped index dropped after — no unconstrained window,
-- mirroring the forward migration.
CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_type_name
    ON entities (type, name);
DROP INDEX IF EXISTS idx_entities_scope_type_name;

CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_src_dst_relation
    ON edges (src_entity_id, dst_entity_id, relation);
DROP INDEX IF EXISTS idx_edges_scope_src_dst_relation;

COMMIT;
