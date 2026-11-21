-- =============================================================================
-- rollback/049_workspace_policies_down.sql
--
-- Restores the tenant-only policies, exactly as they were before 049.
--
-- ── This one really does reverse, and that is worth being explicit about ────
--
-- 046, 047 and 048 all refuse, because their backward direction would require
-- deciding something about somebody's data. This does not: dropping the
-- workspace arm from a predicate deletes nothing, moves nothing and merges
-- nothing. Every row stays exactly where it is.
--
-- **What it does do is widen access**, and a reader should see that stated
-- rather than inferred. After this runs, one of a customer's workspaces can
-- read another's again — which is the pre-5C state, and the reason 5C exists.
-- It is a real reversal of a real change, so it belongs here; it is not a step
-- to take casually.
--
-- ── Why the two shapes are restored rather than unified ─────────────────────
--
-- Before 049, ten tables carried a single `tenant_isolation FOR ALL` policy
-- (created by their original migrations) and seven carried four per-command
-- policies (created by 045). 049 unified them on the per-command shape.
--
-- This restores the split exactly, rather than leaving all seventeen on the
-- tidier shape, because a rollback's job is to return the database to the state
-- the previous migration produced. CI re-applies the chain after rolling it
-- back and asserts the recomposed schema matches a fresh one; a rollback that
-- "improved" things on the way past would make that comparison fail — or worse,
-- pass while the two databases genuinely differed.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    t         text;
    policy    text;
    scope     text := 'tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid';
    -- The ten that carried a single FOR ALL policy before 049.
    for_all   text[] := ARRAY[
        'commitments', 'companion_versions', 'companions', 'conversations',
        'files', 'lifeos_events', 'memory_embeddings', 'messages', 'people',
        'tasks'];
    -- The seven 045 gave four per-command policies.
    per_cmd   text[] := ARRAY[
        'agents', 'calendar_events', 'decisions', 'edges', 'entities',
        'projects', 'reminders'];
BEGIN
    FOREACH t IN ARRAY for_all || per_cmd
    LOOP
        FOR policy IN
            SELECT policyname FROM pg_policies
             WHERE schemaname = 'public' AND tablename = t
        LOOP
            EXECUTE format('DROP POLICY %I ON %I', policy, t);
        END LOOP;
    END LOOP;

    FOREACH t IN ARRAY for_all
    LOOP
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I FOR ALL USING (%s) WITH CHECK (%s)',
            t, scope, scope);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    END LOOP;

    FOREACH t IN ARRAY per_cmd
    LOOP
        EXECUTE format(
            'CREATE POLICY tenant_select ON %I FOR SELECT USING (%s)', t, scope);
        EXECUTE format(
            'CREATE POLICY tenant_insert ON %I FOR INSERT WITH CHECK (%s)', t, scope);
        EXECUTE format(
            'CREATE POLICY tenant_update ON %I FOR UPDATE USING (%s) WITH CHECK (%s)',
            t, scope, scope);
        EXECUTE format(
            'CREATE POLICY tenant_delete ON %I FOR DELETE USING (%s)', t, scope);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    END LOOP;
END
$$;

-- Forced RLS must survive a rollback as surely as a migration. Dropping and
-- recreating every policy on a table is exactly the operation during which
-- somebody turns the protections off "just to get it applied".
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'agents', 'calendar_events', 'commitments', 'companion_versions',
        'companions', 'conversations', 'decisions', 'edges', 'entities',
        'files', 'lifeos_events', 'memory_embeddings', 'messages', 'people',
        'projects', 'reminders', 'tasks']
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_class
             WHERE oid = t::regclass AND relrowsecurity AND relforcerowsecurity)
        THEN
            RAISE EXCEPTION 'Table % lost ENABLE/FORCE ROW LEVEL SECURITY during the 049 rollback', t;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_policies
             WHERE schemaname = 'public' AND tablename = t
               AND (qual LIKE '%app.workspace_id%' OR with_check LIKE '%app.workspace_id%'))
        THEN
            RAISE EXCEPTION 'Table % still carries a workspace predicate after the 049 rollback', t;
        END IF;
    END LOOP;
END
$$;

COMMIT;
