-- =============================================================================
-- 092_private_account_memberships.sql — a granted private account is a member
--
-- `040` closed `delegated_users` to new rows and named the replacement in its
-- refusal: "Create a user and a tenant_membership instead." Nothing did.
-- `security/delegated.py` kept inserting into the closed table, so on every
-- database migrated through 040 the owner's grant-access flow raised the
-- trigger's error and no second person could be given an account.
--
-- A `users` row, a `user_credentials` row and a `tenant_memberships` row hold
-- almost everything a private account needs. Two things they cannot hold, and
-- this migration adds a column for each:
--
--   * `private_role` — the account's role in IRA Private's vocabulary
--     (`viewer`, `trusted_user`, `family_admin`, `owner_equivalent`).
--     `tenant_memberships.role` is the CLOUD vocabulary, and
--     `core/identity/cloud_roles.py` records why the two must not be merged:
--     their scopes differ, and they overlap on names that mean different
--     things. Squeezing four private roles into `viewer`/`member`/`admin`
--     would give three of them one tenant role and lose the scope difference.
--
--   * `person_id` — the relationship record the account was granted to. The
--     People panel shows which person holds an account by this link, exactly as
--     it read `delegated_users.person_id`.
--
-- ── Both are private-tenant only, and a private account is never an admin ────
--
-- Three CHECKs, each closing a specific misuse:
--
--   * the private role vocabulary excludes `primary_owner`, so the database
--     refuses the one role `security/roles.py` says is never grantable;
--   * either column on a row outside the private tenant is refused, so the
--     private vocabulary cannot leak into a cloud tenant's membership;
--   * a row carrying a private role holds the tenant role `viewer` (for the
--     private `viewer`) or `member` (for every other private role). Never
--     `admin`, `owner` or `primary_owner`: a delegated account is not a tenant
--     administrator, whatever it may ask IRA to do. The mapping is the one
--     `035` used when it backfilled legacy accounts.
--
-- ── No backfill window ───────────────────────────────────────────────────────
--
-- Nothing here reads or writes rows, so FORCE ROW LEVEL SECURITY is not lifted.
-- Validating a new CHECK scans the table without consulting policies, and every
-- existing row has both columns NULL, which every CHECK accepts.
--
-- Rollback: rollback/092_private_account_memberships_down.sql
-- =============================================================================

ALTER TABLE tenant_memberships ADD COLUMN IF NOT EXISTS private_role TEXT;
ALTER TABLE tenant_memberships ADD COLUMN IF NOT EXISTS person_id UUID
    REFERENCES people(id) ON DELETE SET NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'tenant_memberships_private_role_check') THEN
        ALTER TABLE tenant_memberships ADD CONSTRAINT tenant_memberships_private_role_check
            CHECK (private_role IS NULL
                   OR private_role IN ('viewer', 'trusted_user', 'family_admin',
                                       'owner_equivalent'));
    END IF;

    -- uuid5(IRA_UUID_NAMESPACE, 'tenant:private') — the literal `023` seeds and
    -- `core/identity/principal.py` derives. A literal rather than a lookup,
    -- because a CHECK cannot run a query.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'tenant_memberships_private_fields_private_tenant') THEN
        ALTER TABLE tenant_memberships ADD CONSTRAINT tenant_memberships_private_fields_private_tenant
            CHECK ((private_role IS NULL AND person_id IS NULL)
                   OR tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid);
    END IF;

    -- A person link describes a private account, so it needs one.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'tenant_memberships_person_needs_private_role') THEN
        ALTER TABLE tenant_memberships ADD CONSTRAINT tenant_memberships_person_needs_private_role
            CHECK (person_id IS NULL OR private_role IS NOT NULL);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'tenant_memberships_private_account_is_not_admin') THEN
        ALTER TABLE tenant_memberships ADD CONSTRAINT tenant_memberships_private_account_is_not_admin
            CHECK (private_role IS NULL
                   OR role = CASE WHEN private_role = 'viewer' THEN 'viewer'
                                  ELSE 'member' END);
    END IF;
END
$$;

COMMENT ON COLUMN tenant_memberships.private_role IS
    'IRA Private role of a granted account (security/roles.py vocabulary). '
    'NULL for the owner, for cloud members and for legacy rows. Private tenant '
    'only; never primary_owner. Access is still decided by '
    'membership_grants_access, never by this column.';
COMMENT ON COLUMN tenant_memberships.person_id IS
    'The relationship record a private account was granted to, as '
    'delegated_users.person_id was. Private tenant only.';
