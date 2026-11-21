-- Rollback for 035_memberships.sql (Phase 3B Batch 0C).
--
-- Refuses while more than one tenant holds membership data, matching the guard
-- convention in 023-032: dropping tenant columns in a multi-tenant database
-- destroys the attribution that says which rows belong to whom, and no rollback
-- can reconstruct it.
--
-- `tenant_memberships` rows created FROM delegated_users are removed; the
-- membership 023 seeded is kept, because 023 owns it.

DO $$
DECLARE tenant_count INTEGER;
BEGIN
    SELECT count(DISTINCT tenant_id) INTO tenant_count FROM tenant_memberships;
    IF tenant_count > 1 THEN
        RAISE EXCEPTION
            'Refusing rollback: % tenants hold memberships. Dropping the '
            'lifecycle columns would lose suspension and removal state.', tenant_count
        USING HINT = 'Roll forward instead.';
    END IF;
END
$$;

DELETE FROM tenant_memberships m
 USING delegated_users d
 WHERE m.user_id = d.user_id
   AND m.role <> 'primary_owner';

DROP POLICY IF EXISTS tenant_isolation ON delegated_users;
ALTER TABLE delegated_users NO FORCE ROW LEVEL SECURITY;
ALTER TABLE delegated_users DISABLE ROW LEVEL SECURITY;
ALTER TABLE delegated_users ALTER COLUMN tenant_id DROP DEFAULT;
ALTER TABLE delegated_users DROP COLUMN IF EXISTS deprecated_at;
ALTER TABLE delegated_users DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE delegated_users DROP COLUMN IF EXISTS user_id;

DROP POLICY IF EXISTS tenant_isolation ON workspace_memberships;
DROP TRIGGER IF EXISTS trg_workspace_memberships_updated_at ON workspace_memberships;
DROP TABLE IF EXISTS workspace_memberships;

DROP TRIGGER IF EXISTS trg_tenant_memberships_updated_at ON tenant_memberships;
DROP INDEX IF EXISTS tenant_memberships_one_primary_owner;
DROP INDEX IF EXISTS idx_tenant_memberships_user_active;

ALTER TABLE tenant_memberships DROP COLUMN IF EXISTS membership_grants_access;
ALTER TABLE tenant_memberships DROP CONSTRAINT IF EXISTS tenant_memberships_status_check;
ALTER TABLE tenant_memberships DROP CONSTRAINT IF EXISTS tenant_memberships_role_check;
ALTER TABLE tenant_memberships DROP CONSTRAINT IF EXISTS tenant_memberships_status_revoked_agree;
ALTER TABLE tenant_memberships DROP CONSTRAINT IF EXISTS tenant_memberships_invited_by_fk;
ALTER TABLE tenant_memberships DROP COLUMN IF EXISTS updated_at;
ALTER TABLE tenant_memberships DROP COLUMN IF EXISTS removed_at;
ALTER TABLE tenant_memberships DROP COLUMN IF EXISTS suspended_at;
ALTER TABLE tenant_memberships DROP COLUMN IF EXISTS joined_at;
ALTER TABLE tenant_memberships DROP COLUMN IF EXISTS invited_by;
ALTER TABLE tenant_memberships DROP COLUMN IF EXISTS status;
