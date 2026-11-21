-- =============================================================================
-- 093_conversation_ownership.sql
--
-- Conversations belong to one user. ADR-0009, accepted by the owner on
-- 2026-10-19 (docs/cloud-transformation/adr/ADR-0009-CONVERSATIONS-BELONG-TO-ONE-USER.md).
--
-- ── What was wrong ─────────────────────────────────────────────────────────
--
-- `conversations` and `messages` recorded a tenant and a workspace and nothing
-- about who they belong to, and their four policies (049) checked only tenant
-- and workspace. Reproduced on `main` at c8f262f, as ira_app with RLS forced:
-- in a workspace two people share, either of them could read the other's chat
-- history given its session id, received the other's conversations in their
-- own data export, and deleted them with "delete my conversations".
--
-- ── What this does ─────────────────────────────────────────────────────────
--
--   1. Both tables gain `owner_user_id`, defaulting from `app.user_id` — the
--      same shape as `workspace_id`'s default, so the writers need no change.
--      NO FOREIGN KEY TO users: background job principals (uuid5('job:…')) have
--      no users row, and a legacy delegated account's runtime id
--      (private_user_id(username)) is not guaranteed one either.
--   2. Existing rows are attributed by the five rules of ADR-0009 §3 (below).
--      A row nobody can be attributed keeps owner NULL: it is then hidden from
--      every per-user read and never deleted by a personal window or erasure
--      (ADR-0009 Q2).
--   3. A message's owner is held equal to its conversation's by a composite
--      foreign key.
--   4. All four policies on both tables gain `owner_user_id = app.user_id`, the
--      shape 057 uses for user-private tables. Every reader and writer —
--      chat context, /chat/history, retention, erasure, export — goes through
--      `workspace_connection`, which sets `app.user_id` from the verified
--      principal, so each becomes correct without its SQL changing.
--
-- The policy NAMES stay `workspace_*`: they still carry the workspace
-- predicate, and the erasure inventory's neighbours assert the 049 shape by
-- name across all seventeen tables.
--
-- Rollback: rollback/093_conversation_ownership_down.sql restores 049's
-- policies and drops the column. That is exactly the behaviour before this.
-- =============================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 0. The owner exemption window, as in 049
--
--    Migrations run as ira_schema_owner, and FORCE RLS applies to the owner:
--    the backfill below would match no rows and report success. Forcing is
--    lifted for this transaction only and restored in §5; a failure anywhere
--    rolls the window shut with everything else.
--
--    That includes the four tables the backfill only READS. Under forced RLS,
--    with no app.tenant_id bound, memory_embeddings, tenant_memberships,
--    delegated_users and workspace_memberships each look empty to the owner,
--    so rules 2 and 4 would find no evidence and no members, and every
--    conversation but the self-reflection one would be left unowned. A
--    superuser bypasses RLS and sees none of this, which is why the chain test
--    applies 093 as ira_schema_owner, the role the runner uses.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE conversations         NO FORCE ROW LEVEL SECURITY;
ALTER TABLE messages              NO FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_embeddings     NO FORCE ROW LEVEL SECURITY;
ALTER TABLE tenant_memberships    NO FORCE ROW LEVEL SECURITY;
ALTER TABLE delegated_users       NO FORCE ROW LEVEL SECURITY;
ALTER TABLE workspace_memberships NO FORCE ROW LEVEL SECURITY;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. The column
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS owner_user_id uuid
    DEFAULT NULLIF(current_setting('app.user_id', true), '')::uuid;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS owner_user_id uuid
    DEFAULT NULLIF(current_setting('app.user_id', true), '')::uuid;

COMMENT ON COLUMN conversations.owner_user_id IS
    'The account this conversation belongs to (ADR-0009). Set from app.user_id '
    'on insert; RLS admits only that account. NULL = legacy, unattributable, '
    'visible to no one. No FK to users: job and legacy delegated principals '
    'have no users row.';
COMMENT ON COLUMN messages.owner_user_id IS
    'Equal to the owning conversation''s owner (composite FK). See '
    'conversations.owner_user_id.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Backfill — ADR-0009 §3, first matching rule wins
--
--    1 system       'ira-self-reflection' → the self-reflection job principal,
--                   uuid5(IRA namespace, 'job:self_reflection'), the id
--                   core/identity/background.py gives it. 'system' (the row
--                   init.sql seeds) stays NULL.
--    2 evidence     every surviving message embedding that names an account of
--                   the conversation's tenant names the same one.
--    3 conflict     they name two or more accounts → NULL.
--    4 sole member  no evidence, and the workspace has exactly one active
--                   member → that member.
--    5 legacy       anything else → NULL.
--
--    Names resolve as core/privacy/retention.py::_own_memory_namespaces does
--    (#30): the account UUID, normalised_email, legacy_username, a password
--    account's external_subject; and legacy delegated_users rows, attributed to
--    their RUNTIME id, uuid5(IRA namespace, 'user:private:' || username) — what
--    app.user_id will hold for them.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TEMP TABLE _093_accounts ON COMMIT DROP AS
WITH msg_names AS (
    SELECT m.conversation_id, m.tenant_id, e.user_id AS name
      FROM messages m
      JOIN memory_embeddings e
        ON e.source_type  = 'message'
       AND e.source_id    = m.id
       AND e.tenant_id    = m.tenant_id
       AND e.workspace_id = m.workspace_id
),
name_owner AS (
    SELECT DISTINCT mn.conversation_id, tm.user_id
      FROM msg_names mn
      JOIN users u
        ON mn.name IN (u.id::text, u.normalised_email, u.legacy_username,
                       CASE WHEN u.auth_provider = 'password' THEN u.external_subject END)
      JOIN tenant_memberships tm
        ON tm.user_id = u.id AND tm.tenant_id = mn.tenant_id
    UNION
    SELECT DISTINCT mn.conversation_id,
           uuid_generate_v5('6f3a1c2e-0b4d-5e8a-9c17-2d4b8e6f0a35'::uuid,
                            'user:private:' || d.username)
      FROM msg_names mn
      JOIN delegated_users d
        ON d.username = mn.name AND d.tenant_id = mn.tenant_id
)
SELECT conversation_id,
       count(DISTINCT user_id)    AS accounts,
       (array_agg(user_id))[1]    AS only_account
  FROM name_owner
 GROUP BY conversation_id;

-- Rule 1
UPDATE conversations
   SET owner_user_id = uuid_generate_v5('6f3a1c2e-0b4d-5e8a-9c17-2d4b8e6f0a35'::uuid,
                                        'job:self_reflection')
 WHERE owner_user_id IS NULL
   AND session_id = 'ira-self-reflection';

-- Rule 2 (rule 3, conflict, is the rows this deliberately leaves alone)
UPDATE conversations c
   SET owner_user_id = a.only_account
  FROM _093_accounts a
 WHERE a.conversation_id = c.id
   AND a.accounts = 1
   AND c.owner_user_id IS NULL
   AND c.session_id NOT IN ('system', 'ira-self-reflection');

-- Rule 4
UPDATE conversations c
   SET owner_user_id = m.only_member
  FROM (SELECT workspace_id,
               count(*)               AS members,
               (array_agg(user_id))[1] AS only_member
          FROM workspace_memberships
         WHERE removed_at IS NULL
         GROUP BY workspace_id) m
 WHERE m.workspace_id = c.workspace_id
   AND m.members = 1
   AND c.owner_user_id IS NULL
   AND c.session_id NOT IN ('system', 'ira-self-reflection')
   AND NOT EXISTS (SELECT 1 FROM _093_accounts a
                    WHERE a.conversation_id = c.id AND a.accounts > 1);

-- Messages follow their conversation.
UPDATE messages m
   SET owner_user_id = c.owner_user_id
  FROM conversations c
 WHERE m.conversation_id = c.id
   AND m.owner_user_id IS NULL
   AND c.owner_user_id IS NOT NULL;

-- What the backfill did, as counts only: no content, names or identifiers.
DO $$
DECLARE
    total      bigint;
    owned      bigint;
    conflicts  bigint;
BEGIN
    SELECT count(*), count(owner_user_id) INTO total, owned FROM conversations;
    SELECT count(*) INTO conflicts FROM _093_accounts WHERE accounts > 1;
    RAISE NOTICE '093: % conversation(s); % attributed; % left unowned (legacy or system), % of them with conflicting evidence',
        total, owned, total - owned, conflicts;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. A message belongs to its conversation's owner
--
--    MATCH SIMPLE: a legacy message (owner NULL) is not checked, which is right
--    — its conversation is unowned too. Any owned message must name a
--    conversation with the same owner.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE conversations
    ADD CONSTRAINT conversations_id_owner_key UNIQUE (id, owner_user_id);
ALTER TABLE messages
    ADD CONSTRAINT messages_conversation_owner_fkey
    FOREIGN KEY (conversation_id, owner_user_id)
    REFERENCES conversations (id, owner_user_id) ON DELETE CASCADE;

-- The chat path looks a conversation up by session on every turn; the context
-- read is by conversation.
CREATE INDEX IF NOT EXISTS conversations_owner_session_idx
    ON conversations (tenant_id, workspace_id, owner_user_id, session_id);
CREATE INDEX IF NOT EXISTS messages_conversation_owner_idx
    ON messages (conversation_id, owner_user_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. The policies: 049's predicate AND the owner
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t      text;
    policy text;
    scope  text :=
        'tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid'
        ' AND workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid'
        ' AND owner_user_id = NULLIF(current_setting(''app.user_id'', true), '''')::uuid';
BEGIN
    FOREACH t IN ARRAY ARRAY['conversations', 'messages']
    LOOP
        -- Every existing policy goes, by name from the catalogue: a leftover
        -- permissive policy would be OR-ed with these and defeat them.
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

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Close the window
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE conversations         ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations         FORCE  ROW LEVEL SECURITY;
ALTER TABLE messages              ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages              FORCE  ROW LEVEL SECURITY;
ALTER TABLE memory_embeddings     FORCE  ROW LEVEL SECURITY;
ALTER TABLE tenant_memberships    FORCE  ROW LEVEL SECURITY;
ALTER TABLE delegated_users       FORCE  ROW LEVEL SECURITY;
ALTER TABLE workspace_memberships FORCE  ROW LEVEL SECURITY;

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. Assert the intended state
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t        text;
    n        int;
    problems text := '';
BEGIN
    FOREACH t IN ARRAY ARRAY['conversations', 'messages']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = t
                          AND column_name = 'owner_user_id') THEN
            problems := problems || format('%s has no owner_user_id; ', t);
        END IF;

        SELECT count(*) INTO n FROM pg_policies
         WHERE schemaname = 'public' AND tablename = t;
        IF n <> 4 THEN
            problems := problems || format('%s has %s policies, not 4; ', t, n);
        END IF;

        SELECT count(*) INTO n FROM pg_policies
         WHERE schemaname = 'public' AND tablename = t
           AND coalesce(qual, with_check) LIKE '%app.user_id%'
           AND coalesce(qual, with_check) LIKE '%app.workspace_id%'
           AND coalesce(qual, with_check) LIKE '%app.tenant_id%';
        IF n <> 4 THEN
            problems := problems || format(
                '%s: only %s of 4 policies check tenant, workspace and owner; ', t, n);
        END IF;

        IF NOT EXISTS (SELECT 1 FROM pg_class
                        WHERE oid = format('public.%I', t)::regclass
                          AND relrowsecurity AND relforcerowsecurity) THEN
            problems := problems || format('%s does not FORCE row-level security; ', t);
        END IF;
    END LOOP;

    -- The window in §0 is shut on every table it opened, not only the two
    -- this migration changes.
    FOREACH t IN ARRAY ARRAY['memory_embeddings', 'tenant_memberships',
                             'delegated_users', 'workspace_memberships']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_class
                        WHERE oid = format('public.%I', t)::regclass
                          AND relrowsecurity AND relforcerowsecurity) THEN
            problems := problems || format('%s was left without FORCE row-level security; ', t);
        END IF;
    END LOOP;

    IF EXISTS (SELECT 1 FROM messages m JOIN conversations c ON c.id = m.conversation_id
                WHERE m.owner_user_id IS DISTINCT FROM c.owner_user_id) THEN
        problems := problems || 'a message''s owner differs from its conversation''s; ';
    END IF;

    IF problems <> '' THEN
        RAISE EXCEPTION '093 FAILED: %', problems;
    END IF;
END
$$;

COMMIT;
