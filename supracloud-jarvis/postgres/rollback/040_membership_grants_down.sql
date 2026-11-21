-- Rollback for 040_membership_grants.sql (Phase 3B Batch 3).
--
-- Restores the single tenant-scoped policy on both membership tables, so a
-- rollback does not leave them with no policy at all — which, under FORCE ROW
-- LEVEL SECURITY, would deny every row to the application and look like data
-- loss. That is the specific failure this ordering avoids: policies are
-- replaced, never merely dropped.
--
-- Rows are not deleted. Memberships written since the migration are real
-- memberships, and `workspace_grants_access` is a generated column whose
-- removal loses no information that `status` and `removed_at` do not still hold.

DROP TRIGGER  IF EXISTS trg_tenant_membership_revocation ON tenant_memberships;
DROP FUNCTION IF EXISTS tenant_membership_revocation_cascade();

DROP TRIGGER  IF EXISTS delegated_users_closed ON delegated_users;
DROP FUNCTION IF EXISTS delegated_users_refuse_insert();

DROP VIEW     IF EXISTS delegated_user_migration_status;
DROP VIEW     IF EXISTS tenant_directory;
DROP FUNCTION IF EXISTS workspace_role_allows(TEXT, TEXT);
DROP TABLE    IF EXISTS workspace_role_capabilities;

ALTER TABLE workspace_memberships
    DROP CONSTRAINT IF EXISTS workspace_memberships_requires_tenant_membership;
ALTER TABLE workspace_memberships
    DROP CONSTRAINT IF EXISTS workspace_memberships_status_removed_agree;
ALTER TABLE workspace_memberships DROP COLUMN IF EXISTS workspace_grants_access;
DROP INDEX IF EXISTS idx_workspace_memberships_active;

DROP POLICY IF EXISTS member_or_tenant_select ON tenant_memberships;
DROP POLICY IF EXISTS tenant_scoped_insert    ON tenant_memberships;
DROP POLICY IF EXISTS tenant_scoped_update    ON tenant_memberships;
DROP POLICY IF EXISTS tenant_scoped_delete    ON tenant_memberships;
DROP POLICY IF EXISTS tenant_isolation        ON tenant_memberships;
CREATE POLICY tenant_isolation ON tenant_memberships
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS member_or_tenant_select ON workspace_memberships;
DROP POLICY IF EXISTS tenant_scoped_insert    ON workspace_memberships;
DROP POLICY IF EXISTS tenant_scoped_update    ON workspace_memberships;
DROP POLICY IF EXISTS tenant_scoped_delete    ON workspace_memberships;
DROP POLICY IF EXISTS tenant_isolation        ON workspace_memberships;
CREATE POLICY tenant_isolation ON workspace_memberships
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
