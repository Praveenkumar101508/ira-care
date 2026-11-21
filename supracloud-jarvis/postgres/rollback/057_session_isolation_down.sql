-- =============================================================================
-- rollback/057_session_isolation_down.sql — reverse 057_session_isolation.sql
--
-- Refuses while any LIVE session exists.
--
-- ── Why a live session is the line ──────────────────────────────────────────
--
-- Reversing this restores `023`'s tenant-only policy, and that policy is the
-- defect: every member of a tenant could then read and revoke every other
-- member's sessions and devices. Doing that while people are signed in hands
-- out a capability over sessions that exist right now.
--
-- Revoked and expired sessions are NOT a reason to refuse. They grant nothing,
-- and their `ip_hash` and `user_agent` are already covered by erasure — so
-- refusing on them would make this script unusable on any deployment that has
-- ever issued a token, for no gain.
--
-- Counted with FORCE lifted, because this script binds no GUCs and a forced
-- read on an unbound connection matches ZERO rows silently. That is the P2-05
-- shape, and here it would make the guard report "no live sessions" on a
-- deployment full of them — the one reading that turns this refusal into
-- theatre.
--
-- ── What is NOT reversed, and why ───────────────────────────────────────────
--
-- `revoked_reason` is dropped with the rest. `session_is_revoked` and
-- `device_is_revoked` are generated from `revoked_at`, which survives, so
-- nothing is lost that re-applying cannot recompute.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_live bigint;
BEGIN
    IF to_regclass('public.user_sessions') IS NULL THEN
        RETURN;   -- nothing to reverse
    END IF;

    EXECUTE 'ALTER TABLE user_sessions NO FORCE ROW LEVEL SECURITY';
    SELECT count(*) INTO n_live
      FROM user_sessions
     WHERE revoked_at IS NULL
       AND (expires_at IS NULL OR expires_at > NOW());
    EXECUTE 'ALTER TABLE user_sessions FORCE ROW LEVEL SECURITY';

    IF n_live > 0 THEN
        RAISE EXCEPTION
            '057-down REFUSING: % live session(s) exist.', n_live
            USING HINT =
                'Reversing this restores the tenant-only policy from 023, '
                'under which every member of a tenant can read and revoke '
                'every other member''s sessions and devices — and under which '
                'erasing one account deletes them all. Doing that while people '
                'are signed in grants that capability over sessions that exist '
                'right now. Revoked and expired sessions do not block this; '
                'live ones do. If you genuinely intend it, revoke the live '
                'sessions first and record why.';
    END IF;
END $$;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['user_sessions', 'user_devices'] LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_delete', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_update', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_insert', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_select', t);

        -- Restore 023's policy verbatim. A rollback that left the table with NO
        -- policy under FORCE RLS would deny everything silently, which reads as
        -- a working deployment with an empty session list.
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format($f$
            CREATE POLICY tenant_isolation ON %I
                USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        $f$, t);
    END LOOP;
END $$;

DROP INDEX IF EXISTS user_devices_live_idx;
DROP INDEX IF EXISTS user_sessions_live_idx;
DROP INDEX IF EXISTS user_sessions_token_hash_key;

ALTER TABLE user_devices  DROP COLUMN IF EXISTS device_is_revoked;
ALTER TABLE user_sessions DROP COLUMN IF EXISTS session_is_revoked;
ALTER TABLE user_sessions DROP COLUMN IF EXISTS revoked_reason;

-- 057's grants are restored to what `023` and the default privileges left:
-- the worker's access came from ALTER DEFAULT PRIVILEGES, so putting it back is
-- part of reversing, not a widening this script chose.
GRANT SELECT, INSERT, UPDATE, DELETE ON user_sessions TO ira_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON user_devices  TO ira_worker;

DO $$
DECLARE
    t text;
    n integer;
BEGIN
    FOREACH t IN ARRAY ARRAY['user_sessions', 'user_devices'] LOOP
        IF to_regclass('public.' || t) IS NULL THEN
            RAISE EXCEPTION '057-down destroyed %', t;
        END IF;
        SELECT count(*) INTO n FROM pg_policy
         WHERE polrelid = ('public.' || t)::regclass;
        IF n <> 1 THEN
            RAISE EXCEPTION
                '057-down left % with % policies, expected exactly 1 '
                '(tenant_isolation). A table under FORCE RLS with no policy '
                'denies everything silently.', t, n;
        END IF;
        IF NOT (SELECT relforcerowsecurity FROM pg_class
                 WHERE oid = ('public.' || t)::regclass) THEN
            RAISE EXCEPTION '057-down left % without FORCE ROW LEVEL SECURITY', t;
        END IF;
    END LOOP;

    -- 057 creates no table, so none may have gone.
    IF to_regclass('public.users') IS NULL THEN
        RAISE EXCEPTION '057-down destroyed users';
    END IF;
    IF to_regclass('public.mfa_recovery_codes') IS NULL THEN
        RAISE EXCEPTION '057-down destroyed mfa_recovery_codes';
    END IF;
END $$;

COMMIT;
