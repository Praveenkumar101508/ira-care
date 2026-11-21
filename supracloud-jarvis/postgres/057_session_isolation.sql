-- =============================================================================
-- 057_session_isolation.sql — Phase 2 batch 8, sessions and devices
--
-- `user_sessions` and `user_devices` have existed since `023` with a single
-- `tenant_isolation` policy, FOR ALL, keyed on `app.tenant_id` alone. That is
-- the `036` defect one level up, and it has been latent for thirty-four
-- migrations because nothing has ever written a row to either table.
--
-- ── What the tenant-only policy actually permits ────────────────────────────
--
-- Every member of a tenant can read every other member's sessions and devices:
-- `ip_hash`, `user_agent`, `last_seen_at`, and a device label that is
-- deliberately human ("Praveen's iPhone"). That is not a session list, it is a
-- movement log for a colleague. `token_hash` is a hash of the refresh jti and
-- not directly presentable, which is the one thing keeping this out of
-- account-takeover territory.
--
-- The policy is FOR ALL, so the same predicate governs UPDATE and DELETE. Any
-- member could therefore revoke any other member's sessions — sign them out of
-- everything, repeatedly, with no audit trail and no capability check, because
-- there is no capability involved. It is just a row they are allowed to write.
--
-- ── And it makes erasure destroy other people's rows ────────────────────────
--
-- `erase_postgres` issues `DELETE FROM <table> WHERE tenant_id = $1` and relies
-- on RLS to narrow that to the erased user — which is exactly what happens for
-- `totp_secrets` and `user_mfa_keys`, whose policies name `app.user_id`. On
-- these two tables RLS narrows nothing, so the SQL predicate stands alone and
-- one member's erasure deletes EVERY member's sessions and devices.
--
-- Both defects are the same defect. Both are fixed here, in the policy, rather
-- than by adding a WHERE clause in Python that the next caller has to remember:
-- the reason `erase_postgres` is correct for the second-factor tables is that
-- the database refuses to widen the statement, and that is the property these
-- two tables were missing.
--
-- ── Why replace rather than add ─────────────────────────────────────────────
--
-- PostgreSQL combines permissive policies with OR. Adding a user-scoped policy
-- beside `tenant_isolation` would widen access to exactly what it already is —
-- the new policy could only ever grant more. The tenant-only policy has to GO,
-- and it is replaced by four per-command policies each requiring BOTH GUCs, the
-- shape `052`, `053` and `056` all use for material a tenant administrator must
-- not reach.
--
-- ── What this deliberately does NOT do ──────────────────────────────────────
--
-- It does not carve out an administrator path. "An admin should be able to sign
-- a compromised employee out" is a real need and a real product decision, and
-- the answer to it is an explicit, audited capability — not a policy that
-- silently lets every member of a tenant do it to every other. `036` made the
-- same call for the second factor and recorded it; this follows that, and Batch
-- 11's audit surface is where such a path would be argued.
--
-- Rollback: rollback/057_session_isolation_down.sql
-- =============================================================================

BEGIN;

-- Sessions gain a revocation reason. Without it "this session ended" and "this
-- session was revoked because the device was lost" are the same row, and the
-- second is the one anybody ever asks about.
ALTER TABLE user_sessions ADD COLUMN IF NOT EXISTS revoked_reason text;

-- Generated, so "is this session still live" is the database's answer rather
-- than each caller's — the reasoning `040` recorded for
-- `membership_grants_access`. Expiry is NOT part of it: `NOW()` is not
-- immutable and cannot appear in a generated column, so expiry stays a
-- predicate the queries apply. This column answers only the question a row can
-- answer about itself.
ALTER TABLE user_sessions DROP COLUMN IF EXISTS session_is_revoked;
ALTER TABLE user_sessions
    ADD COLUMN session_is_revoked boolean
    GENERATED ALWAYS AS (revoked_at IS NOT NULL) STORED;

ALTER TABLE user_devices DROP COLUMN IF EXISTS device_is_revoked;
ALTER TABLE user_devices
    ADD COLUMN device_is_revoked boolean
    GENERATED ALWAYS AS (revoked_at IS NOT NULL) STORED;

-- A refresh token presents its jti; the lookup has to be by hash and has to be
-- fast, because it happens on every refresh. UNIQUE because two sessions
-- sharing a token hash would make "which session is this" unanswerable.
CREATE UNIQUE INDEX IF NOT EXISTS user_sessions_token_hash_key
    ON user_sessions (token_hash) WHERE token_hash IS NOT NULL;

CREATE INDEX IF NOT EXISTS user_sessions_live_idx
    ON user_sessions (user_id, issued_at DESC) WHERE NOT session_is_revoked;

CREATE INDEX IF NOT EXISTS user_devices_live_idx
    ON user_devices (user_id, created_at DESC) WHERE NOT device_is_revoked;

-- ── The policies ────────────────────────────────────────────────────────────

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['user_sessions', 'user_devices'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', t);

        -- The tenant-only policy goes. Permissive policies OR together, so
        -- leaving it in place would make everything below decorative.
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);

        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_select', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_insert', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_update', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_delete', t);

        EXECUTE format($f$
            CREATE POLICY %I ON %I FOR SELECT
                USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                   AND user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid)
        $f$, t || '_select', t);

        EXECUTE format($f$
            CREATE POLICY %I ON %I FOR INSERT
                WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        AND user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid)
        $f$, t || '_insert', t);

        EXECUTE format($f$
            CREATE POLICY %I ON %I FOR UPDATE
                USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                   AND user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid)
                WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        AND user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid)
        $f$, t || '_update', t);

        EXECUTE format($f$
            CREATE POLICY %I ON %I FOR DELETE
                USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                   AND user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid)
        $f$, t || '_delete', t);
    END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON user_sessions TO ira_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON user_devices  TO ira_app;

-- REQUIRED, and opt-OUT: default privileges grant every table to ira_worker.
-- A background job holding a session table can enumerate who is signed in from
-- where. When an expiry purge is eventually built it runs bound to a user, the
-- same way every other scoped sweep in this schema does.
REVOKE ALL ON user_sessions FROM ira_worker;
REVOKE ALL ON user_devices  FROM ira_worker;

DO $$
DECLARE
    t text;
    n integer;
BEGIN
    FOREACH t IN ARRAY ARRAY['user_sessions', 'user_devices'] LOOP
        IF EXISTS (SELECT 1 FROM pg_policy
                    WHERE polrelid = ('public.' || t)::regclass
                      AND polname = 'tenant_isolation') THEN
            RAISE EXCEPTION
                '057: % still carries the tenant_isolation policy. Permissive '
                'policies combine with OR, so it would re-open exactly what '
                'this migration closes.', t;
        END IF;

        SELECT count(*) INTO n FROM pg_policy
         WHERE polrelid = ('public.' || t)::regclass;
        IF n <> 4 THEN
            RAISE EXCEPTION
                '057: % has % policies, expected exactly 4.', t, n;
        END IF;

        SELECT count(*) INTO n FROM pg_policy
         WHERE polrelid = ('public.' || t)::regclass
           AND (coalesce(pg_get_expr(polqual, polrelid), '')
             || coalesce(pg_get_expr(polwithcheck, polrelid), '') NOT LIKE '%app.user_id%'
            OR  coalesce(pg_get_expr(polqual, polrelid), '')
             || coalesce(pg_get_expr(polwithcheck, polrelid), '') NOT LIKE '%app.tenant_id%');
        IF n <> 0 THEN
            RAISE EXCEPTION
                '057: % policy(ies) on % do not constrain BOTH app.user_id and '
                'app.tenant_id. One that named only the tenant would let every '
                'member read and revoke every other member''s sessions.', n, t;
        END IF;

        IF NOT (SELECT relforcerowsecurity FROM pg_class
                 WHERE oid = ('public.' || t)::regclass) THEN
            RAISE EXCEPTION '057: % is not FORCE ROW LEVEL SECURITY', t;
        END IF;

        IF has_table_privilege('ira_worker', t, 'SELECT') THEN
            RAISE EXCEPTION
                '057: ira_worker can read %. Default privileges grant every '
                'table to the worker, so the REVOKE is required.', t;
        END IF;
        IF NOT has_table_privilege('ira_app', t, 'SELECT') THEN
            RAISE EXCEPTION '057: ira_app cannot read %', t;
        END IF;
    END LOOP;
END $$;

COMMIT;
