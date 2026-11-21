-- =============================================================================
-- rollback/050_workspace_not_null_down.sql
--
-- Drops the NOT NULL from `workspace_id` on all seventeen tables.
--
-- ── This one reverses, and it is worth saying what it gives back ────────────
--
-- Unlike 046, 047 and 048, this is a clean reversal: dropping a NOT NULL
-- deletes nothing, moves nothing and makes no decision about anybody's data.
-- Every row keeps the workspace it has.
--
-- What it restores is the *possibility* of a NULL, and that has a consequence
-- worth stating rather than leaving a reader to work out:
--
--     046's composite foreign keys are MATCH SIMPLE, so a NULL in any
--     referencing column skips the check entirely. While `workspace_id` can be
--     NULL, an edge with a NULL workspace could join entities in two different
--     workspaces and no constraint would object.
--
-- In practice 049's policies still refuse to write such a row — `WITH CHECK`
-- requires the bound workspace and NULL never equals anything — so the gap is
-- covered as long as 049 is applied. Rolling 049 back as well uncovers it. The
-- two are ordered for that reason: 050 comes off first, 049 second, and the
-- database is never left with the constraint gone AND the policy gone in the
-- wrong order.
-- =============================================================================

BEGIN;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'agents', 'calendar_events', 'commitments', 'companion_versions',
        'companions', 'conversations', 'decisions', 'edges', 'entities',
        'files', 'lifeos_events', 'memory_embeddings', 'messages', 'people',
        'projects', 'reminders', 'tasks']
    LOOP
        EXECUTE format('ALTER TABLE %I ALTER COLUMN workspace_id DROP NOT NULL', t);
    END LOOP;
END
$$;

-- Forced RLS and 049's policies are not this script's to touch, and an
-- ALTER COLUMN cannot remove them — which is exactly why it is worth checking.
-- A rollback that quietly took a protection with it would be discovered by the
-- next person to wonder why the boundary stopped working.
DO $$
DECLARE
    t        text;
    problems text := '';
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'agents', 'calendar_events', 'commitments', 'companion_versions',
        'companions', 'conversations', 'decisions', 'edges', 'entities',
        'files', 'lifeos_events', 'memory_embeddings', 'messages', 'people',
        'projects', 'reminders', 'tasks']
    LOOP
        IF (SELECT attnotnull FROM pg_attribute
             WHERE attrelid = t::regclass AND attname = 'workspace_id')
        THEN
            problems := problems || format(E'\n  %s.workspace_id is still NOT NULL', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_class
                        WHERE oid = t::regclass AND relrowsecurity AND relforcerowsecurity)
        THEN
            problems := problems || format(E'\n  %s lost forced RLS during the 050 rollback', t);
        END IF;
    END LOOP;

    IF problems <> '' THEN
        RAISE EXCEPTION E'The 050 rollback did not reach its intended state:%', problems;
    END IF;
END
$$;

COMMIT;
