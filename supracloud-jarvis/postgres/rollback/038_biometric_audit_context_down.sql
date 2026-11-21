-- Rollback for 038_biometric_audit_context.sql (Phase 3B Batch 1C).
--
-- Safe: the columns dropped here were added by 038 and backfilled to NULL for
-- pre-existing rows, so nothing is lost that 038 did not add. The append-only
-- trigger must go first — otherwise the ALTERs below would themselves be
-- refused.
--
-- After this, biometric_audit is mutable again. That is a regression, and it is
-- the reason a rollback of this migration should be paired with re-application
-- rather than left in place.

DROP TRIGGER IF EXISTS biometric_audit_append_only ON biometric_audit;
DROP FUNCTION IF EXISTS biometric_audit_refuse_mutation();

-- All five names: `tenant_isolation` was the single ALL policy an earlier
-- version of 038 created, and a database that ran that version still has it.
-- Leaving it behind would keep row-level security in force on a table this
-- script has just stripped the tenant column from, which denies every row.
DROP POLICY IF EXISTS tenant_isolation         ON biometric_audit;
DROP POLICY IF EXISTS subject_or_tenant_select ON biometric_audit;
DROP POLICY IF EXISTS subject_or_tenant_insert ON biometric_audit;
DROP POLICY IF EXISTS redaction_only_update    ON biometric_audit;
DROP POLICY IF EXISTS subject_or_tenant_delete ON biometric_audit;
ALTER TABLE biometric_audit NO FORCE ROW LEVEL SECURITY;
ALTER TABLE biometric_audit DISABLE ROW LEVEL SECURITY;

DROP INDEX IF EXISTS idx_biometric_audit_user;
DROP INDEX IF EXISTS idx_biometric_audit_tenant;

ALTER TABLE biometric_audit DROP CONSTRAINT IF EXISTS biometric_audit_event_type_check;
ALTER TABLE biometric_audit DROP CONSTRAINT IF EXISTS biometric_audit_workspace_implies_tenant;
-- Before the columns go: the attribution constraint references both of them, so
-- dropping `tenant_id` while it stands would fail.
ALTER TABLE biometric_audit DROP CONSTRAINT IF EXISTS biometric_audit_attribution_implies_tenant;

ALTER TABLE biometric_audit DROP COLUMN IF EXISTS enrollment_version;
ALTER TABLE biometric_audit DROP COLUMN IF EXISTS event_type;
ALTER TABLE biometric_audit DROP COLUMN IF EXISTS workspace_id;
ALTER TABLE biometric_audit DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE biometric_audit DROP COLUMN IF EXISTS user_id;
