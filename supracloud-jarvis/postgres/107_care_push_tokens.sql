-- =============================================================================
-- 107_care_push_tokens.sql — IRA CARE: push goes only to the people it is for
--
-- Care alerts used to be pushed through the server's general notifier, whose
-- mobile channel sends to EVERY phone registered with the deployment. A care
-- alert is for the members of one person's care circle, so:
--
--   care_push_tokens
--     One row per phone per circle membership: the token (an Expo push token),
--     the account it belongs to (user_id), the membership it serves (member_id)
--     and the person (person_id), under the tenant and the workspace with forced
--     row-level security. Revoked when the member leaves the circle, when the
--     push service says the device is gone, or when the phone unregisters.
--     Removed with the account (ON DELETE CASCADE from users), the member and
--     the person.
--
--   care_push_tickets
--     The push service's ticket for one message to one token, attached to the
--     alert attempt it belongs to, with the delivery receipt once fetched. This
--     is what "delivered", "failed" or "not yet confirmed" on an alert means.
--
-- Rollback: rollback/107_care_push_tokens_down.sql
-- =============================================================================

BEGIN;

CREATE TABLE care_push_tokens (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    member_id       UUID NOT NULL REFERENCES care_circle_members(id) ON DELETE CASCADE,
    person_id       UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    token           TEXT NOT NULL CHECK (length(token) BETWEEN 8 AND 200),
    platform        TEXT NOT NULL DEFAULT '' CHECK (platform IN ('', 'ios', 'android', 'web')),
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at      TIMESTAMPTZ,
    revoked_reason  TEXT NOT NULL DEFAULT '' CHECK (length(revoked_reason) <= 120),
    CONSTRAINT care_push_tokens_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT care_push_tokens_once_per_member UNIQUE (member_id, token)
);
CREATE INDEX idx_care_push_tokens_member ON care_push_tokens (tenant_id, workspace_id, member_id) WHERE status = 'active';
CREATE INDEX idx_care_push_tokens_user ON care_push_tokens (user_id);

ALTER TABLE care_push_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE care_push_tokens FORCE ROW LEVEL SECURITY;
CREATE POLICY care_push_tokens_select ON care_push_tokens FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_push_tokens_insert ON care_push_tokens FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_push_tokens_update ON care_push_tokens FOR UPDATE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_push_tokens_delete ON care_push_tokens FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
REVOKE ALL ON care_push_tokens FROM PUBLIC;
REVOKE ALL ON care_push_tokens FROM ira_app, ira_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON care_push_tokens TO ira_app, ira_worker;

CREATE TABLE care_push_tickets (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    alert_id        UUID NOT NULL REFERENCES care_alerts(id) ON DELETE CASCADE,
    attempt_id      UUID REFERENCES care_alert_attempts(id) ON DELETE SET NULL,
    token_id        UUID REFERENCES care_push_tokens(id) ON DELETE SET NULL,
    ticket_id       TEXT NOT NULL DEFAULT '' CHECK (length(ticket_id) <= 200),
    status          TEXT NOT NULL DEFAULT 'sent'
                        CHECK (status IN ('logged', 'sent', 'delivered', 'failed')),
    detail          TEXT NOT NULL DEFAULT '' CHECK (length(detail) <= 400),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    receipt_at      TIMESTAMPTZ,
    CONSTRAINT care_push_tickets_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_push_tickets_alert ON care_push_tickets (tenant_id, workspace_id, alert_id);
CREATE INDEX idx_care_push_tickets_pending ON care_push_tickets (tenant_id, workspace_id, created_at)
    WHERE status = 'sent' AND receipt_at IS NULL;

ALTER TABLE care_push_tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE care_push_tickets FORCE ROW LEVEL SECURITY;
CREATE POLICY care_push_tickets_select ON care_push_tickets FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_push_tickets_insert ON care_push_tickets FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_push_tickets_update ON care_push_tickets FOR UPDATE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_push_tickets_delete ON care_push_tickets FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
REVOKE ALL ON care_push_tickets FROM PUBLIC;
REVOKE ALL ON care_push_tickets FROM ira_app, ira_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON care_push_tickets TO ira_app, ira_worker;

COMMIT;
