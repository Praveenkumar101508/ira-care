-- =============================================================================
-- rollback/029_commercial_down.sql — rollback for ../029_commercial.sql
--
-- ⚠️  THIS FILE LIVES IN rollback/ ON PURPOSE. DO NOT MOVE IT UP A LEVEL.
--     postgres/ is mounted as /docker-entrypoint-initdb.d and every .sql there
--     runs on a fresh install, in order. See tests/tenancy/test_migration_hygiene.py.
--
-- Reverses Phase 8's commercial schema. Run ONLY, and MANUALLY.
--
-- ⚠️  READ BEFORE RUNNING — THIS DESTROYS TWO DIFFERENT KINDS OF EVIDENCE
--
-- `admin_access_log` is the record of every time an administrator read a user's
-- content, and `admin_access_grants` is the authorisation that made each read
-- lawful. They are append-only precisely so they cannot be quietly edited;
-- dropping the tables is the one way to remove them, and afterwards it is
-- indistinguishable from no administrator ever having looked.
--
-- `model_usage` is the billing record AND the record of which provider handled
-- which request at what sensitivity. Dropping it removes the ability to answer
-- "where did my words go?" for every request already made.
--
-- So this script REFUSES while either holds rows for more than one tenant, and
-- refuses outright while any administrator access has been logged — that one is
-- not a warning, because a deployment that has exercised break-glass access has
-- something a regulator or a user may ask about.
-- =============================================================================

BEGIN;

-- ── Guard: administrator access is never silently dropped ────────────────────
DO $$
DECLARE
    accesses INTEGER;
BEGIN
    SELECT COUNT(*) INTO accesses FROM admin_access_log;
    IF accesses > 0 THEN
        RAISE EXCEPTION
            'Refusing to roll back: % administrator access(es) have been logged '
            'against user content. This is the record that those reads were '
            'authorised and time-bound; dropping it is indistinguishable from '
            'no administrator ever having looked. Export it first, and keep the '
            'export — a user or a regulator may ask.',
            accesses;
    END IF;
END
$$;

-- ── Guard: refuse on a deployment that has served more than one tenant ───────
DO $$
DECLARE
    tenant_count INTEGER;
    usage_rows   INTEGER;
BEGIN
    SELECT COUNT(DISTINCT tenant_id), COUNT(*) INTO tenant_count, usage_rows
    FROM model_usage;

    IF tenant_count > 1 THEN
        RAISE EXCEPTION
            'Refusing to roll back: % tenants hold % metering row(s). Besides '
            'being the billing record, this table is how a user is told which '
            'provider handled which request. Export per tenant first.',
            tenant_count, usage_rows;
    END IF;

    IF usage_rows > 0 THEN
        RAISE WARNING
            'Dropping % metering row(s) for a single tenant. The record of '
            'which provider handled which request will be gone.', usage_rows;
    END IF;
END
$$;

-- ── Guard: warn about quotas, which are a commercial commitment ──────────────
DO $$
DECLARE
    quota_rows INTEGER;
BEGIN
    SELECT COUNT(*) INTO quota_rows FROM usage_quotas;
    IF quota_rows > 0 THEN
        RAISE WARNING
            'Dropping % quota row(s). Every tenant reverts to unmetered, which '
            'is more permissive than what was configured — check that is '
            'intended before continuing.', quota_rows;
    END IF;
END
$$;

-- ── Drop in dependency order ─────────────────────────────────────────────────

DROP TRIGGER IF EXISTS admin_access_log_no_rewrite ON admin_access_log;
DROP TRIGGER IF EXISTS admin_access_must_be_granted ON admin_access_log;
DROP TRIGGER IF EXISTS admin_grants_revoke_only ON admin_access_grants;
DROP FUNCTION IF EXISTS admin_access_log_append_only();
DROP FUNCTION IF EXISTS admin_access_within_grant();
DROP FUNCTION IF EXISTS admin_grants_revoke_only();

DROP INDEX IF EXISTS admin_access_log_recent_idx;
DROP INDEX IF EXISTS admin_grants_live_idx;
DROP INDEX IF EXISTS model_usage_billing_idx;
DROP INDEX IF EXISTS model_usage_tenant_period_idx;

DROP TABLE IF EXISTS admin_access_log;
DROP TABLE IF EXISTS admin_access_grants;
DROP TABLE IF EXISTS model_usage;
DROP TABLE IF EXISTS usage_quotas;
DROP TABLE IF EXISTS model_providers;

COMMIT;
