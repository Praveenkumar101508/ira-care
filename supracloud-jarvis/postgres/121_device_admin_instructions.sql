-- =============================================================================
-- 121_device_admin_instructions.sql — IRA CARE OS: deployment administrators and
-- the signed instructions they issue to one watch (E2.m, E2.o, D-082)
--
--   deployment_administrators
--     Who may issue device-policy instructions to a watch: an account (users.id),
--     granted and revoked by the operator with the migrator's credentials
--     (scripts/deployment_admin.py), exactly as OS releases are published
--     (scripts/os_release.py, 120). Deployment-wide, no tenant column: an IRA
--     administrator looks after the fleet, not one care organisation. No care
--     workspace role, tenant role or deployment-owner flag implies a row here.
--     Row-level security is a WRITE LOCK and a READ LIMIT. The runtime writes
--     nothing here, whatever grants a provisioning step hands it (R-47); only
--     ira_schema_owner, which the migrator assumes with SET ROLE, may grant or
--     revoke. And the runtime reads a LIVE grant only on a connection bound to
--     that account (app.user_id): it can ask "am I a deployment administrator?"
--     and cannot list who is. Revoked grants are readable by every connection,
--     so a watch's connection can tell that an instruction's issuer was revoked
--     after issuing it (care/deviceadmin.py).
--
--   care_device_admin_instructions
--     One instruction for one watch, signed by the server (care/devicepolicy.py):
--     an action from a closed list, a nonce used once, issued and expiry times at
--     most 15 minutes apart, who issued it and why, and what became of it
--     (delivered, revoked, applied, refused, restored). The signed columns cannot
--     change once written (trigger below). Workspace-scoped under the same strict
--     predicate as every care table: a watch reads only its own workspace's rows.
--
--   care_device_admin_events
--     The audit trail: one row per thing that happened to an instruction, who did
--     it (a deployment administrator or the watch) and when. Insert-only for the
--     runtime.
--
--   Both care tables go with the watch (ON DELETE CASCADE from care_devices, which
--   goes with the person cared for) and with the tenant.
--
-- Rollback: rollback/121_device_admin_instructions_down.sql
-- =============================================================================

BEGIN;

-- ── 1. Deployment administrators ─────────────────────────────────────────────

CREATE TABLE deployment_administrators (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- An operator's label for the grant ("fleet on-call"), never a name or address.
    label           TEXT NOT NULL CHECK (label ~ '^[A-Za-z0-9 ._-]{1,60}$'),
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at      TIMESTAMPTZ,
    CONSTRAINT deployment_administrators_revoked_after_granted
        CHECK (revoked_at IS NULL OR revoked_at >= granted_at)
);
-- At most one live grant per account; a revoked grant stays as history.
CREATE UNIQUE INDEX deployment_administrators_live
    ON deployment_administrators (user_id) WHERE revoked_at IS NULL;

ALTER TABLE deployment_administrators ENABLE ROW LEVEL SECURITY;
ALTER TABLE deployment_administrators FORCE ROW LEVEL SECURITY;
CREATE POLICY deployment_administrators_read ON deployment_administrators
    FOR SELECT USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                      OR revoked_at IS NOT NULL);
-- The operator's write path, and the only one: the schema owner, reached by the
-- migrator's SET ROLE. ira_app and ira_worker match no write policy.
CREATE POLICY deployment_administrators_operator ON deployment_administrators
    FOR ALL TO ira_schema_owner USING (TRUE) WITH CHECK (TRUE);
REVOKE ALL ON deployment_administrators FROM PUBLIC;
REVOKE ALL ON deployment_administrators FROM ira_app, ira_worker;
GRANT SELECT ON deployment_administrators TO ira_app;

-- ── 2. Instructions ──────────────────────────────────────────────────────────

CREATE TABLE care_device_admin_instructions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    device_id       UUID NOT NULL REFERENCES care_devices(id) ON DELETE CASCADE,
    -- The closed list. Nothing else can be stored, so nothing else can be signed.
    action          TEXT NOT NULL CHECK (action IN ('MAINTENANCE_WINDOW', 'RESTORE_RESTRICTIONS', 'FACTORY_RESET')),
    nonce           TEXT NOT NULL UNIQUE CHECK (nonce ~ '^[0-9A-Za-z_-]{16,64}$'),
    issued_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    -- The deployment administrator's account id. Kept without a foreign key so the
    -- record of who acted outlives the account.
    issued_by       UUID NOT NULL,
    reason          TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 10 AND 300),
    payload         TEXT NOT NULL CHECK (length(payload) BETWEEN 1 AND 400),
    signature       TEXT NOT NULL CHECK (length(signature) BETWEEN 1 AND 200),
    key_id          TEXT NOT NULL CHECK (length(key_id) BETWEEN 1 AND 64),
    delivered_at    TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,
    revoked_by      UUID,
    outcome         TEXT CHECK (outcome IS NULL OR outcome IN ('applied', 'refused', 'restored')),
    outcome_reason  TEXT NOT NULL DEFAULT '' CHECK (length(outcome_reason) <= 200),
    outcome_at      TIMESTAMPTZ,
    CONSTRAINT care_device_admin_instructions_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE,
    -- Short and bounded: an administrator's window is at most 15 minutes, and an
    -- instruction not used by its expiry is dead.
    CONSTRAINT care_device_admin_instructions_window
        CHECK (expires_at > issued_at AND expires_at <= issued_at + INTERVAL '15 minutes'),
    CONSTRAINT care_device_admin_instructions_revoker
        CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
);
CREATE INDEX idx_care_device_admin_instructions_device
    ON care_device_admin_instructions (tenant_id, workspace_id, device_id, expires_at DESC);

-- What was signed cannot be rewritten, and a revocation cannot be undone. Raised
-- as insufficient_privilege, the same refusal a missing grant gives.
CREATE FUNCTION care_device_admin_instructions_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.tenant_id, NEW.workspace_id, NEW.device_id, NEW.action, NEW.nonce, NEW.issued_at,
        NEW.expires_at, NEW.issued_by, NEW.reason, NEW.payload, NEW.signature, NEW.key_id)
       IS DISTINCT FROM
       (OLD.tenant_id, OLD.workspace_id, OLD.device_id, OLD.action, OLD.nonce, OLD.issued_at,
        OLD.expires_at, OLD.issued_by, OLD.reason, OLD.payload, OLD.signature, OLD.key_id) THEN
        RAISE EXCEPTION 'a signed device instruction cannot be changed' USING ERRCODE = '42501';
    END IF;
    IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
        RAISE EXCEPTION 'a revoked device instruction stays revoked' USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER care_device_admin_instructions_guard
    BEFORE UPDATE ON care_device_admin_instructions
    FOR EACH ROW EXECUTE FUNCTION care_device_admin_instructions_guard();

ALTER TABLE care_device_admin_instructions ENABLE ROW LEVEL SECURITY;
ALTER TABLE care_device_admin_instructions FORCE ROW LEVEL SECURITY;
CREATE POLICY care_device_admin_instructions_select ON care_device_admin_instructions FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_device_admin_instructions_insert ON care_device_admin_instructions FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_device_admin_instructions_update ON care_device_admin_instructions FOR UPDATE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
REVOKE ALL ON care_device_admin_instructions FROM PUBLIC;
REVOKE ALL ON care_device_admin_instructions FROM ira_app, ira_worker;
GRANT SELECT, INSERT, UPDATE ON care_device_admin_instructions TO ira_app;

-- ── 3. The audit trail ───────────────────────────────────────────────────────

CREATE TABLE care_device_admin_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    instruction_id  UUID NOT NULL REFERENCES care_device_admin_instructions(id) ON DELETE CASCADE,
    event           TEXT NOT NULL CHECK (event IN ('issued', 'delivered', 'revoked', 'applied', 'refused', 'restored')),
    actor_kind      TEXT NOT NULL CHECK (actor_kind IN ('deployment_admin', 'device')),
    -- The administrator's account id, or the watch's device id.
    actor_id        UUID NOT NULL,
    detail          TEXT NOT NULL DEFAULT '' CHECK (length(detail) <= 200),
    at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_device_admin_events_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_device_admin_events_instruction
    ON care_device_admin_events (tenant_id, workspace_id, instruction_id, at);

ALTER TABLE care_device_admin_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE care_device_admin_events FORCE ROW LEVEL SECURITY;
CREATE POLICY care_device_admin_events_select ON care_device_admin_events FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_device_admin_events_insert ON care_device_admin_events FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
REVOKE ALL ON care_device_admin_events FROM PUBLIC;
REVOKE ALL ON care_device_admin_events FROM ira_app, ira_worker;
GRANT SELECT, INSERT ON care_device_admin_events TO ira_app;

COMMIT;
