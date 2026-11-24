-- =============================================================================
-- 111_care_user_preferences.sql — IRA CARE: one app, each person's own appearance
--
--   care_user_preferences   how the app looks for one account (wallpaper, light or
--                           dark, text size, contrast, the order of the home cards),
--                           kept on the server so every device the person signs in
--                           on looks the same. One row per account per workspace.
--                           A photo wallpaper travels in the JSON as a data URL
--                           (at most 500 KB of text).
--
-- Rollback: rollback/111_care_user_preferences_down.sql
-- =============================================================================

BEGIN;

CREATE TABLE care_user_preferences (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    appearance      JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (length(appearance::text) <= 500000),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_user_preferences_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT care_user_preferences_once UNIQUE (workspace_id, user_id)
);
CREATE INDEX idx_care_user_preferences_user ON care_user_preferences (user_id);

ALTER TABLE care_user_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE care_user_preferences FORCE ROW LEVEL SECURITY;
CREATE POLICY care_user_preferences_select ON care_user_preferences FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_user_preferences_insert ON care_user_preferences FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_user_preferences_update ON care_user_preferences FOR UPDATE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_user_preferences_delete ON care_user_preferences FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
REVOKE ALL ON care_user_preferences FROM PUBLIC;
REVOKE ALL ON care_user_preferences FROM ira_app, ira_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON care_user_preferences TO ira_app, ira_worker;

COMMIT;
