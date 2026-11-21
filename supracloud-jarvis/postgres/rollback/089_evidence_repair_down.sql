-- =============================================================================
-- rollback/089_evidence_repair_down.sql — reverse 089_evidence_repair.sql
--
-- Refuses while any evidence version exists. Those rows are the only copy of
-- what a source said at the time work was done from it; dropping them would
-- silently destroy the history F1 exists to keep. An empty schema is removed
-- completely, including the `workspace.approve_changes` capability and 040's
-- original capability CHECK.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    versions BIGINT;
BEGIN
    IF to_regclass('public.evidence_artifact_versions') IS NULL THEN
        RETURN;
    END IF;

    EXECUTE 'SELECT count(*) FROM evidence_artifact_versions' INTO versions;

    IF versions > 0 THEN
        RAISE EXCEPTION
            'Refusing rollback: % evidence version(s) exist. They are the only '
            'record of what each source said when work was done from it. Export '
            'or delete the owning workspaces first.',
            versions;
    END IF;
END $$;

DROP TABLE IF EXISTS evidence_approvals;
DROP TABLE IF EXISTS evidence_patches;
DROP TABLE IF EXISTS evidence_dependencies;
DROP TABLE IF EXISTS evidence_output_spans;
DROP TABLE IF EXISTS evidence_claims;
DROP TABLE IF EXISTS evidence_artifact_access;
DROP TABLE IF EXISTS evidence_artifact_versions;
DROP TABLE IF EXISTS evidence_audit_events;
DROP TABLE IF EXISTS evidence_artifacts;

ALTER TABLE workspace_role_capabilities NO FORCE ROW LEVEL SECURITY;
DELETE FROM workspace_role_capabilities WHERE capability = 'workspace.approve_changes';
ALTER TABLE workspace_role_capabilities FORCE ROW LEVEL SECURITY;

ALTER TABLE workspace_role_capabilities
    DROP CONSTRAINT IF EXISTS workspace_role_capabilities_capability_check;
ALTER TABLE workspace_role_capabilities
    ADD CONSTRAINT workspace_role_capabilities_capability_check
    CHECK (capability IN (
        'workspace.read',
        'workspace.write',
        'workspace.manage_members',
        'workspace.manage_settings',
        'workspace.delete_content'));

COMMIT;
