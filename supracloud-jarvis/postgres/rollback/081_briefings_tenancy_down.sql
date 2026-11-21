-- =============================================================================
-- 081_briefings_tenancy_down.sql — reversing P12-03 finding G
--
-- Understand what this restores before running it: a table holding a full
-- summary of one tenant's revenue, security posture, tasks and calendar, with
-- no tenancy columns and no row-level security, read by
-- `GET /api/v1/briefing/latest` behind `require_auth` alone and no predicate.
--
-- That is finding G exactly as it was found. On a deployment with more than one
-- tenant this is not a rollback, it is the disclosure — so it refuses there, on
-- the same reasoning as `051_notifications_tenancy_down.sql`.
--
-- The legitimate use is a private single-tenant deployment where the columns
-- are unwanted, or an operator unwinding a failed upgrade before a second
-- tenant exists.
--
-- Note what this CANNOT restore: `081` deleted the legacy rows, because none of
-- them carried provable ownership. Reversing the schema does not bring them
-- back, and it should not — they were deleted precisely so that no workspace
-- would be handed data it might not own.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_tenants bigint;
BEGIN
    IF to_regclass('public.briefings') IS NULL THEN
        RAISE EXCEPTION '081-down REFUSING: table briefings does not exist.';
    END IF;

    -- Said at RUN TIME, not only in the header. A rollback that quietly
    -- restores a schema invites the reading that it restored the data with it,
    -- and this one cannot: `081` deleted the legacy rows and archived nothing.
    -- An operator reversing a migration is usually trying to get something
    -- back, so the one thing they cannot get back is stated where they will
    -- see it.
    RAISE WARNING
        '081-down restores the SCHEMA ONLY. The legacy briefing rows 081 '
        'deleted are gone and cannot be recovered by this script or any '
        'other — they were not archived, deliberately, because none of them '
        'carried provable tenant or workspace ownership.';

    SELECT count(*) INTO n_tenants FROM tenants;

    IF n_tenants > 1 THEN
        RAISE EXCEPTION
            '081-down REFUSING: this deployment has % tenants. Dropping the '
            'briefings tenancy columns restores a table that '
            'GET /api/v1/briefing/latest reads with no predicate, serving one '
            'tenant''s business and security summary to every authenticated '
            'user in the deployment. That is finding G, re-opened. If you '
            'genuinely intend this, drop the policies by hand and record why.',
            n_tenants
            USING HINT =
                'A briefing is an aggregate of the most protected tables in '
                'the schema. Reversing its boundary is not symmetrical with '
                'adding it.';
    END IF;
END $$;

DROP POLICY IF EXISTS workspace_select ON briefings;
DROP POLICY IF EXISTS workspace_insert ON briefings;
DROP POLICY IF EXISTS workspace_update ON briefings;
DROP POLICY IF EXISTS workspace_delete ON briefings;

ALTER TABLE briefings NO FORCE ROW LEVEL SECURITY;
ALTER TABLE briefings DISABLE ROW LEVEL SECURITY;

DROP INDEX IF EXISTS idx_briefings_scope;

ALTER TABLE briefings
    DROP CONSTRAINT IF EXISTS briefings_workspace_in_tenant_fkey,
    DROP CONSTRAINT IF EXISTS briefings_tenant_id_fkey;

ALTER TABLE briefings
    DROP COLUMN IF EXISTS workspace_id,
    DROP COLUMN IF EXISTS tenant_id;

COMMIT;
