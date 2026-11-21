-- =============================================================================
-- 060_user_scoped_credentials.sql — Phase 2 batch 11
--
-- Two tables the manifest has always described as isolated by `app.user_id`,
-- and which the live schema isolated by tenant alone. Batch 8 added the drift
-- guard that compares the manifest's PREDICATE against the database; these are
-- two of the three tables it found on the day it was written.
--
--     connector_accounts     Phase 7 OAuth refresh tokens
--     companion_preferences  one person's settings
--
-- The third, `notifications`, is not here and is not a defect: it has no
-- `user_id` column at all, so the manifest was describing a column that does
-- not exist. That entry is corrected rather than the schema.
--
-- ── connector_accounts is the serious one ───────────────────────────────────
--
-- `credential_ciphertext` is a long-lived OAuth refresh token. Under a
-- tenant-only policy every member of a tenant could read every other member's —
-- their grant to Google, Slack, or whatever else they connected. The
-- application never relied on that: `core/connectors/store.py` has always
-- filtered `tenant_id AND user_id`. Only the policy was weak, so this migration
-- makes the database enforce what the application already assumed.
--
-- ── The reason this took a batch, and not a line ────────────────────────────
--
-- DEK rotation re-seals credentials TENANT-WIDE. `keyring.rotate_tenant` reads
-- every row in the tenant, rewraps each under a new key, and — this is the
-- dangerous part — counts rows still pointing at the old key version before
-- destroying it.
--
-- Under a user-scoped policy, an operator connection bound to one tenant and a
-- derived user id matches ZERO rows. Rotation would rewrap nothing, the
-- straggler count would return zero, and the old key would be destroyed while
-- every credential still needed it. Not an error: silent, and unrecoverable.
--
-- So `rotate_tenant` was reworked in the same change to enumerate the tenant's
-- members and bind each in turn. The policy below is safe only because that
-- rework landed with it, which is why they are one batch and not two.
--
-- Rollback: rollback/060_user_scoped_credentials_down.sql
-- =============================================================================

BEGIN;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['connector_accounts', 'companion_preferences'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', t);

        -- The tenant-only policy GOES. Permissive policies combine with OR, so
        -- leaving it beside the new four would make every one of them
        -- decorative — the same reasoning `057` records for sessions.
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

GRANT SELECT, INSERT, UPDATE, DELETE ON connector_accounts    TO ira_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON companion_preferences TO ira_app;

-- REQUIRED, and opt-OUT: default privileges grant every table to ira_worker.
-- A background job holding OAuth refresh tokens is a background job that can
-- act as any user at any connected provider.
REVOKE ALL ON connector_accounts    FROM ira_worker;
REVOKE ALL ON companion_preferences FROM ira_worker;

DO $$
DECLARE
    t text;
    n integer;
BEGIN
    FOREACH t IN ARRAY ARRAY['connector_accounts', 'companion_preferences'] LOOP
        IF EXISTS (SELECT 1 FROM pg_policy
                    WHERE polrelid = ('public.' || t)::regclass
                      AND polname = 'tenant_isolation') THEN
            RAISE EXCEPTION
                '060: % still carries the tenant_isolation policy. Permissive '
                'policies combine with OR, so it re-opens exactly what this '
                'migration closes.', t;
        END IF;

        SELECT count(*) INTO n FROM pg_policy
         WHERE polrelid = ('public.' || t)::regclass;
        IF n <> 4 THEN
            RAISE EXCEPTION '060: % has % policies, expected exactly 4', t, n;
        END IF;

        SELECT count(*) INTO n FROM pg_policy
         WHERE polrelid = ('public.' || t)::regclass
           AND (coalesce(pg_get_expr(polqual, polrelid), '')
             || coalesce(pg_get_expr(polwithcheck, polrelid), '') NOT LIKE '%app.user_id%'
            OR  coalesce(pg_get_expr(polqual, polrelid), '')
             || coalesce(pg_get_expr(polwithcheck, polrelid), '') NOT LIKE '%app.tenant_id%');
        IF n <> 0 THEN
            RAISE EXCEPTION
                '060: % policy(ies) on % do not constrain BOTH app.user_id and '
                'app.tenant_id.', n, t;
        END IF;

        IF NOT (SELECT relforcerowsecurity FROM pg_class
                 WHERE oid = ('public.' || t)::regclass) THEN
            RAISE EXCEPTION '060: % is not FORCE ROW LEVEL SECURITY', t;
        END IF;

        IF has_table_privilege('ira_worker', t, 'SELECT') THEN
            RAISE EXCEPTION '060: ira_worker can read %', t;
        END IF;
        IF NOT has_table_privilege('ira_app', t, 'SELECT') THEN
            RAISE EXCEPTION '060: ira_app cannot read %', t;
        END IF;
    END LOOP;
END $$;

COMMIT;
