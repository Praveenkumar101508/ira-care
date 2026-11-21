-- =============================================================================
-- 082_heartbeat_surfaced_scope_down.sql — reversing P12-03b finding H
--
-- Restores `UNIQUE (item_key)` across the whole deployment, which is finding H
-- exactly as it was found: one tenant surfacing an item suppresses it for every
-- other tenant whose key collides, silently, with nothing logged anywhere.
--
-- So it refuses on a multi-tenant deployment, on the same reasoning as `051`
-- and `081`. On a single-tenant deployment the global constraint and the
-- scoped one are equivalent, and the reversal is a legitimate operation.
--
-- The de-duplication keys themselves are dropped on the way back, because the
-- restored constraint is narrower than the one it replaces and rows that were
-- distinct under `(tenant_id, workspace_id, item_key)` can collide under
-- `(item_key)` alone. Dropping them costs one possible repeat announcement,
-- which is what `082` says about the same rows in the other direction.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_tenants bigint;
BEGIN
    IF to_regclass('public.heartbeat_surfaced') IS NULL THEN
        RAISE EXCEPTION '082-down REFUSING: table heartbeat_surfaced does not exist.';
    END IF;

    SELECT count(*) INTO n_tenants FROM tenants;

    IF n_tenants > 1 THEN
        RAISE EXCEPTION
            '082-down REFUSING: this deployment has % tenants. Restoring '
            'UNIQUE (item_key) makes the de-duplication ledger global again, '
            'so one tenant surfacing an item suppresses it for every other '
            'tenant whose key collides. That is finding H, re-opened, and it '
            'fails silently — a missing heartbeat logs nothing.',
            n_tenants
            USING HINT =
                'On a single-tenant deployment the two constraints are '
                'equivalent and this runs. If you genuinely intend it on a '
                'multi-tenant one, drop the constraint by hand and record why.';
    END IF;
END $$;

DROP POLICY IF EXISTS workspace_select ON heartbeat_surfaced;
DROP POLICY IF EXISTS workspace_insert ON heartbeat_surfaced;
DROP POLICY IF EXISTS workspace_update ON heartbeat_surfaced;
DROP POLICY IF EXISTS workspace_delete ON heartbeat_surfaced;

ALTER TABLE heartbeat_surfaced NO FORCE ROW LEVEL SECURITY;
ALTER TABLE heartbeat_surfaced DISABLE ROW LEVEL SECURITY;

DELETE FROM heartbeat_surfaced;

ALTER TABLE heartbeat_surfaced
    DROP CONSTRAINT IF EXISTS heartbeat_surfaced_scope_key;
ALTER TABLE heartbeat_surfaced
    ADD CONSTRAINT heartbeat_surfaced_item_key_key UNIQUE (item_key);

ALTER TABLE heartbeat_surfaced
    DROP CONSTRAINT IF EXISTS heartbeat_surfaced_workspace_in_tenant_fkey,
    DROP CONSTRAINT IF EXISTS heartbeat_surfaced_tenant_id_fkey;

ALTER TABLE heartbeat_surfaced
    DROP COLUMN IF EXISTS workspace_id,
    DROP COLUMN IF EXISTS tenant_id;

COMMIT;
