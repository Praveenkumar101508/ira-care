-- =============================================================================
-- rollback/093_conversation_ownership_down.sql
--
-- Reverses 093: `conversations` and `messages` go back to 049's four
-- workspace-only policies, and `owner_user_id` is dropped with its constraint
-- and indexes.
--
-- ── What this gives back, said plainly ───────────────────────────────────────
--
-- Exactly the behaviour before 093, and so exactly the defect ADR-0009 fixed:
-- anyone in a shared workspace can read a colleague's chat history given its
-- session id, receives it in their own export, and deletes it with "delete my
-- conversations". The ownership 093 recorded is lost with the column; applying
-- 093 again re-derives it by the same rules.
--
-- It does not refuse. No row is deleted, nothing becomes unreadable that was
-- readable before 093, and no data outlives its policy.
-- =============================================================================

BEGIN;

ALTER TABLE conversations NO FORCE ROW LEVEL SECURITY;
ALTER TABLE messages      NO FORCE ROW LEVEL SECURITY;

-- 049's policies, exactly: the workspace predicate on all four commands.
DO $$
DECLARE
    t      text;
    policy text;
    scope  text :=
        'tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid'
        ' AND workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid';
BEGIN
    FOREACH t IN ARRAY ARRAY['conversations', 'messages']
    LOOP
        FOR policy IN
            SELECT policyname FROM pg_policies
             WHERE schemaname = 'public' AND tablename = t
        LOOP
            EXECUTE format('DROP POLICY %I ON %I', policy, t);
        END LOOP;

        EXECUTE format(
            'CREATE POLICY workspace_select ON %I FOR SELECT USING (%s)', t, scope);
        EXECUTE format(
            'CREATE POLICY workspace_insert ON %I FOR INSERT WITH CHECK (%s)', t, scope);
        EXECUTE format(
            'CREATE POLICY workspace_update ON %I FOR UPDATE USING (%s) WITH CHECK (%s)',
            t, scope, scope);
        EXECUTE format(
            'CREATE POLICY workspace_delete ON %I FOR DELETE USING (%s)', t, scope);
    END LOOP;
END
$$;

ALTER TABLE messages      DROP CONSTRAINT IF EXISTS messages_conversation_owner_fkey;
ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_id_owner_key;
DROP INDEX IF EXISTS messages_conversation_owner_idx;
DROP INDEX IF EXISTS conversations_owner_session_idx;
ALTER TABLE messages      DROP COLUMN IF EXISTS owner_user_id;
ALTER TABLE conversations DROP COLUMN IF EXISTS owner_user_id;

ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations FORCE  ROW LEVEL SECURITY;
ALTER TABLE messages      ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages      FORCE  ROW LEVEL SECURITY;

DO $$
DECLARE
    t        text;
    n        int;
    problems text := '';
BEGIN
    FOREACH t IN ARRAY ARRAY['conversations', 'messages']
    LOOP
        IF EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = t
                      AND column_name = 'owner_user_id') THEN
            problems := problems || format('%s still has owner_user_id; ', t);
        END IF;
        SELECT count(*) INTO n FROM pg_policies
         WHERE schemaname = 'public' AND tablename = t
           AND coalesce(qual, with_check) LIKE '%app.workspace_id%'
           AND coalesce(qual, with_check) NOT LIKE '%app.user_id%';
        IF n <> 4 THEN
            problems := problems || format('%s: %s of 4 policies are back to 049''s shape; ', t, n);
        END IF;
    END LOOP;
    IF problems <> '' THEN
        RAISE EXCEPTION '093 rollback FAILED: %', problems;
    END IF;
END
$$;

COMMIT;
