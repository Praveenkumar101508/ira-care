-- =============================================================================
-- rollback/060_user_scoped_credentials_down.sql — reverse 060
--
-- Refuses while any LIVE OAuth credential exists.
--
-- ── Why a live credential is the line ───────────────────────────────────────
--
-- Reversing this restores the tenant-only policy, under which every member of a
-- tenant can read every other member's `credential_ciphertext` — a long-lived
-- OAuth refresh token for whatever they connected. Doing that while credentials
-- exist hands out access to accounts at Google, Slack and anywhere else, on
-- systems this deployment does not control and cannot audit.
--
-- REVOKED credentials do not block it: `revoked_at IS NOT NULL` means the grant
-- is already dead at the provider, so the ciphertext opens nothing. Rows with no
-- ciphertext do not block it either.
--
-- `companion_preferences` never blocks: settings are personal but they are not
-- credentials, and refusing on them would make this script unusable for no
-- proportionate gain.
--
-- ── And the rollback is not enough on its own ───────────────────────────────
--
-- `keyring.rotate_tenant` was reworked alongside `060` to enumerate members and
-- bind each. That code is correct under BOTH policies — a per-user pass reads
-- the same rows a tenant-wide pass would, just in several transactions — so
-- reversing the policy does not strand it. The reverse is not true, which is
-- why the migration and the rework had to land together.
--
-- Counted with FORCE lifted, because this script binds no GUCs and these tables
-- are now user-scoped: an unbound connection matches ZERO rows. That is the
-- P2-05 shape, and here it would report "no live credentials" on a deployment
-- full of them.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_live bigint;
BEGIN
    IF to_regclass('public.connector_accounts') IS NULL THEN
        RETURN;
    END IF;

    EXECUTE 'ALTER TABLE connector_accounts NO FORCE ROW LEVEL SECURITY';
    SELECT count(*) INTO n_live
      FROM connector_accounts
     WHERE credential_ciphertext IS NOT NULL
       AND revoked_at IS NULL;
    EXECUTE 'ALTER TABLE connector_accounts FORCE ROW LEVEL SECURITY';

    IF n_live > 0 THEN
        RAISE EXCEPTION
            '060-down REFUSING: % live OAuth credential(s) exist.', n_live
            USING HINT =
                'Reversing this restores the tenant-only policy from 023, under '
                'which every member of a tenant can read every other member''s '
                'refresh token — access to accounts at providers this '
                'deployment does not control and cannot audit. Revoked '
                'credentials and rows without ciphertext do not block this; '
                'live ones do. Revoke them at the provider first, and record '
                'why.';
    END IF;
END $$;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['connector_accounts', 'companion_preferences'] LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_delete', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_update', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_insert', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_select', t);

        -- Restore 023's policy. A table left under FORCE RLS with NO policy
        -- denies everything silently, which reads as a working deployment whose
        -- connectors have all quietly stopped.
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format($f$
            CREATE POLICY tenant_isolation ON %I
                USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        $f$, t);
    END LOOP;
END $$;

-- The worker's access came from ALTER DEFAULT PRIVILEGES, so restoring it is
-- part of reversing rather than a widening this script chose.
GRANT SELECT, INSERT, UPDATE, DELETE ON connector_accounts    TO ira_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON companion_preferences TO ira_worker;

DO $$
DECLARE
    t text;
    n integer;
BEGIN
    FOREACH t IN ARRAY ARRAY['connector_accounts', 'companion_preferences'] LOOP
        IF to_regclass('public.' || t) IS NULL THEN
            RAISE EXCEPTION '060-down destroyed %', t;
        END IF;
        SELECT count(*) INTO n FROM pg_policy
         WHERE polrelid = ('public.' || t)::regclass;
        IF n <> 1 THEN
            RAISE EXCEPTION
                '060-down left % with % policies, expected exactly 1 '
                '(tenant_isolation).', t, n;
        END IF;
    END LOOP;

    IF to_regclass('public.tenant_data_keys') IS NULL THEN
        RAISE EXCEPTION
            '060-down destroyed tenant_data_keys. Every credential surviving '
            'this rollback is sealed under a key in that table.';
    END IF;
    IF to_regclass('public.external_identities') IS NULL THEN
        RAISE EXCEPTION '060-down destroyed external_identities';
    END IF;
END $$;

COMMIT;
