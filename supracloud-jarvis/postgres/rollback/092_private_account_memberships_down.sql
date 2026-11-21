-- =============================================================================
-- rollback/092_private_account_memberships_down.sql
--
-- Removes the two private-account columns `092` added to `tenant_memberships`.
--
-- ── It refuses while any account holds its role there ───────────────────────
--
-- After 092, granting access writes a `users` row, a credential and a
-- membership whose `private_role` says what the person may do. Dropping that
-- column does not return those accounts to `delegated_users`, which `040`
-- closed to new rows. It leaves each one a member with no role and no person
-- link, and its name stays taken because the `users` row remains, so the owner
-- cannot grant that person access again. That cannot be repaired from what is
-- left, so this refuses rather than proceeding.
--
-- ── Why FORCE is lifted for the count ───────────────────────────────────────
--
-- `tenant_memberships` is FORCE ROW LEVEL SECURITY keyed on `app.tenant_id`,
-- and a rollback binds no tenant. Run by the table's owner rather than a
-- superuser, the count would see zero rows and the guard would pass with
-- accounts present: the failure `035` section 0 records. FORCE is lifted for
-- the count and restored before anything is dropped, inside one transaction.
-- =============================================================================

BEGIN;

ALTER TABLE tenant_memberships NO FORCE ROW LEVEL SECURITY;

DO $$
DECLARE
    accounts INTEGER;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'tenant_memberships'
                      AND column_name = 'private_role') THEN
        RETURN;
    END IF;
    SELECT count(*) INTO accounts FROM tenant_memberships WHERE private_role IS NOT NULL;
    IF accounts > 0 THEN
        RAISE EXCEPTION
            '092-down REFUSING: % private account(s) hold their role in '
            'tenant_memberships.private_role. Dropping the column would leave '
            'each a member with no role and a name nobody can grant again.',
            accounts
        USING HINT =
            'Revoke those accounts and delete their tenant_memberships, '
            'user_credentials and users rows deliberately first, or leave 092 '
            'in place.';
    END IF;
END $$;

ALTER TABLE tenant_memberships FORCE ROW LEVEL SECURITY;

ALTER TABLE tenant_memberships DROP CONSTRAINT IF EXISTS tenant_memberships_private_account_is_not_admin;
ALTER TABLE tenant_memberships DROP CONSTRAINT IF EXISTS tenant_memberships_person_needs_private_role;
ALTER TABLE tenant_memberships DROP CONSTRAINT IF EXISTS tenant_memberships_private_fields_private_tenant;
ALTER TABLE tenant_memberships DROP CONSTRAINT IF EXISTS tenant_memberships_private_role_check;
ALTER TABLE tenant_memberships DROP COLUMN IF EXISTS person_id;
ALTER TABLE tenant_memberships DROP COLUMN IF EXISTS private_role;

COMMIT;
