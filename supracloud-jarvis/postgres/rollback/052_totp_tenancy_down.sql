-- =============================================================================
-- rollback/052_totp_tenancy_down.sql
--
-- Reverses 052: restores the single `FOR ALL user_private` policy 036 created,
-- and drops the tenant column, its index and its foreign key.
--
-- ── Read this before running it ────────────────────────────────────────────
--
-- This is a real reversal rather than a refusal, because 052 is additive: the
-- policies it replaces were strictly wider, so restoring them cannot expose a
-- row to anyone who could not already reach it under the user predicate. The
-- second factor stays protected from other users either way.
--
-- What it DOES give up is the tenant boundary. On a deployment where more than
-- one tenant holds a TOTP enrolment, that boundary is carrying real traffic and
-- removing it is not a rollback — it is the finding, restored. So this script
-- REFUSES in exactly that case, and the refusal is atomic.
--
-- The guard counts tenants that hold **TOTP rows**, not tenants in the
-- deployment. That is the precise question: a thousand tenants where only one
-- has ever enrolled a second factor lose nothing here, and blocking them would
-- be a guard that fires on the wrong fact. (051's guard counts deployment
-- tenants, which is why it can never run in CI. This one asks what it means.)
--
-- Counted with FORCE lifted, because this script binds no `app.user_id` and
-- would otherwise count zero rows under the very policy it is removing — and a
-- guard that always sees zero is not a guard.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_tenants bigint;
BEGIN
    IF to_regclass('public.totp_secrets') IS NULL THEN
        RAISE EXCEPTION '052-down REFUSING: table totp_secrets does not exist.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute
         WHERE attrelid = 'public.totp_secrets'::regclass
           AND attname = 'tenant_id' AND NOT attisdropped
    ) THEN
        -- Already reversed. Nothing to do, and saying so beats failing.
        RETURN;
    END IF;

    EXECUTE 'ALTER TABLE totp_secrets NO FORCE ROW LEVEL SECURITY';
    SELECT count(DISTINCT tenant_id) INTO n_tenants FROM totp_secrets;

    IF n_tenants > 1 THEN
        RAISE EXCEPTION
            '052-down REFUSING: % tenants hold TOTP enrolments.', n_tenants
            USING HINT =
                'Dropping tenant_id removes a boundary that is currently '
                'separating real second factors. The remaining user predicate '
                'still protects a secret from other users, but the tenant '
                'boundary and the ON DELETE CASCADE that erases a tenant''s '
                'enrolments both go with the column. If you genuinely intend '
                'this, drop the policies and the column by hand and record why.';
    END IF;
END
$$;

-- Restore 036's policy exactly as it was: one FOR ALL, user predicate only.
DROP POLICY IF EXISTS user_private_select ON totp_secrets;
DROP POLICY IF EXISTS user_private_insert ON totp_secrets;
DROP POLICY IF EXISTS user_private_update ON totp_secrets;
DROP POLICY IF EXISTS user_private_delete ON totp_secrets;

DROP POLICY IF EXISTS user_private ON totp_secrets;
CREATE POLICY user_private ON totp_secrets
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

DROP INDEX IF EXISTS idx_totp_secrets_tenant;

ALTER TABLE totp_secrets DROP CONSTRAINT IF EXISTS totp_secrets_tenant_id_fkey;
ALTER TABLE totp_secrets DROP COLUMN IF EXISTS tenant_id;

-- 036 left the table forced; restore that rather than the state section 1
-- borrowed for counting. A table left ENABLE-but-not-FORCE would let the schema
-- owner read every second factor in the deployment.
ALTER TABLE totp_secrets ENABLE ROW LEVEL SECURITY;
ALTER TABLE totp_secrets FORCE  ROW LEVEL SECURITY;

DO $$
DECLARE
    forced boolean;
    n      bigint;
BEGIN
    SELECT relforcerowsecurity INTO forced
      FROM pg_class WHERE oid = 'public.totp_secrets'::regclass;
    IF NOT forced THEN
        RAISE EXCEPTION '052-down FAILED: FORCE ROW LEVEL SECURITY was not restored.';
    END IF;

    SELECT count(*) INTO n FROM pg_policy
     WHERE polrelid = 'public.totp_secrets'::regclass;
    IF n <> 1 THEN
        RAISE EXCEPTION
            '052-down FAILED: expected exactly 1 policy after the reversal, found %.', n;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_attribute
         WHERE attrelid = 'public.totp_secrets'::regclass
           AND attname = 'tenant_id' AND NOT attisdropped
    ) THEN
        RAISE EXCEPTION '052-down FAILED: tenant_id remains on totp_secrets.';
    END IF;
END
$$;

COMMIT;
