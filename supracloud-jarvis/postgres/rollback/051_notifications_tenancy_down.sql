-- =============================================================================
-- rollback/051_notifications_tenancy_down.sql
--
-- Reverses 051: drops the policies, RLS, constraints, indexes and columns that
-- gave `notifications` a tenant.
--
-- ── Read this before running it ────────────────────────────────────────────
--
-- This is a real reversal, not a refusal, because 051 is additive and nothing
-- downstream depends on the columns existing. But understand what it restores:
-- a table holding notification `title` and `body` for every tenant, read by
-- `GET /api/v1/notifications` with no predicate, and cleared by
-- `DELETE /api/v1/notifications/read` for the whole deployment at once.
--
-- That is 3C-10 exactly as it was found. Running this on a multi-tenant
-- deployment re-opens a live cross-tenant read and a cross-tenant delete.
--
-- The legitimate use is a private single-tenant deployment where the columns
-- are unwanted overhead, or an operator unwinding a failed upgrade before any
-- second tenant exists. It refuses if a second tenant does exist, because at
-- that point the reversal is not a rollback — it is the vulnerability.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_tenants bigint;
    n_distinct bigint;
BEGIN
    IF to_regclass('public.notifications') IS NULL THEN
        RAISE EXCEPTION '051-down REFUSING: table notifications does not exist.';
    END IF;

    -- More than one tenant in the deployment, or notifications already
    -- belonging to more than one, means undoing this removes a boundary that is
    -- carrying real traffic.
    SELECT count(*) INTO n_tenants FROM tenants;

    IF EXISTS (
        SELECT 1 FROM pg_attribute
         WHERE attrelid = 'public.notifications'::regclass
           AND attname = 'tenant_id' AND NOT attisdropped
    ) THEN
        -- Counted as the schema owner under FORCE RLS, which would see zero
        -- rows. Disable the policy for this statement by reading through a
        -- security-definer-free path: count DISTINCT via the catalog-free
        -- route below only after RLS is dropped. So do the tenant count first
        -- and treat >1 tenant as sufficient grounds to refuse.
        NULL;
    END IF;

    IF n_tenants > 1 THEN
        RAISE EXCEPTION
            '051-down REFUSING: this deployment has % tenants. Dropping the '
            'notifications tenancy columns restores a table that '
            'GET /api/v1/notifications reads with no predicate and '
            'DELETE /api/v1/notifications/read clears for every tenant at '
            'once. That is finding 3C-10, re-opened. If you genuinely intend '
            'this, drop the policies by hand and record why.',
            n_tenants;
    END IF;
END $$;

DROP POLICY IF EXISTS tenant_scoped_select ON notifications;
DROP POLICY IF EXISTS tenant_scoped_insert ON notifications;
DROP POLICY IF EXISTS tenant_scoped_update ON notifications;
DROP POLICY IF EXISTS tenant_scoped_delete ON notifications;

ALTER TABLE notifications NO FORCE ROW LEVEL SECURITY;
ALTER TABLE notifications DISABLE ROW LEVEL SECURITY;

DROP INDEX IF EXISTS idx_notifications_tenant;
DROP INDEX IF EXISTS idx_notifications_tenant_unread;

ALTER TABLE notifications
    DROP CONSTRAINT IF EXISTS notifications_workspace_in_tenant_fkey,
    DROP CONSTRAINT IF EXISTS notifications_workspace_id_fkey,
    DROP CONSTRAINT IF EXISTS notifications_tenant_id_fkey;

ALTER TABLE notifications
    DROP COLUMN IF EXISTS workspace_id,
    DROP COLUMN IF EXISTS tenant_id;

-- Assert the reversal is complete rather than partial: a table left with the
-- columns but no policies is strictly worse than either end state, because it
-- looks scoped to a reader and enforces nothing.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_policy WHERE polrelid = 'public.notifications'::regclass
    ) THEN
        RAISE EXCEPTION
            '051-down FAILED: policies remain on notifications after the drop.';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_attribute
         WHERE attrelid = 'public.notifications'::regclass
           AND attname IN ('tenant_id', 'workspace_id') AND NOT attisdropped
    ) THEN
        RAISE EXCEPTION
            '051-down FAILED: a tenancy column remains on notifications.';
    END IF;
END $$;

COMMIT;
