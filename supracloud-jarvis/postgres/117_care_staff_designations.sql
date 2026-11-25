-- =============================================================================
-- 117_care_staff_designations.sql — IRA CARE: the care coordinator
--
--   care_staff_designations
--     A care manager names a member of the care team as a COORDINATOR (D-081).
--     A coordinator plans visits for everyone the organisation cares for and sees
--     every carer's caseload; nothing else changes. The workspace role is still
--     `member` (a designation on a viewer or a manager has no effect), so a
--     coordinator still cannot add or remove people, carers or watches, change
--     safety settings or write care plans: those stay with the care manager. Words
--     under consent and sealed access notes still follow the coordinator's own
--     circle membership, not the designation.
--     One row per person and designation; removing the designation deletes the row.
--     Rows go with the account (ON DELETE CASCADE from users, and the care step of
--     account erasure) and with the tenant.
--
-- Rollback: rollback/117_care_staff_designations_down.sql
-- =============================================================================

BEGIN;

CREATE TABLE care_staff_designations (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    designation     TEXT NOT NULL CHECK (designation IN ('coordinator')),
    granted_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_staff_designations_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT care_staff_designations_once UNIQUE (tenant_id, workspace_id, user_id, designation)
);
CREATE INDEX idx_care_staff_designations_user ON care_staff_designations (tenant_id, workspace_id, user_id);

ALTER TABLE care_staff_designations ENABLE ROW LEVEL SECURITY;
ALTER TABLE care_staff_designations FORCE ROW LEVEL SECURITY;
CREATE POLICY care_staff_designations_select ON care_staff_designations FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_staff_designations_insert ON care_staff_designations FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_staff_designations_delete ON care_staff_designations FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
REVOKE ALL ON care_staff_designations FROM PUBLIC;
REVOKE ALL ON care_staff_designations FROM ira_app, ira_worker;
GRANT SELECT, INSERT, DELETE ON care_staff_designations TO ira_app;

COMMIT;
