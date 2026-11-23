-- =============================================================================
-- 106_care_sos_state.sql — IRA CARE: a trustworthy SOS
--
-- The watch must show the true state of a Help request or a fall: sending,
-- waiting for a connection, received by the server, "[Name] is on the way",
-- "[Name] has arrived". That needs three things from the database:
--
--   care_device_events
--     One row per urgent event the watch sends (Help, a fall, "I'm OK", an
--     answer), keyed by the watch's own idempotency key. The watch keeps a
--     durable outbox and re-sends until the server confirms; the server stores
--     the first answer under the key and hands the same answer back for every
--     repeat, so a retry never opens a second alert or records a second answer.
--     Belongs to the person cared for (ON DELETE CASCADE from care_people).
--
--   care_acknowledgements.kind + 'arrived'
--     The responder who said "I'm on my way" has arrived. Distinct from
--     checked_in_person, which records the visit's outcome.
--
--   care_safety_settings.reading_upload_interval_s
--     How often the watch uploads ordinary readings while its service runs
--     (default 60 s). An out-of-range heart-rate sample is uploaded at once,
--     whatever this says; the watch holds the carer's range for that.
--
-- Rollback: rollback/106_care_sos_state_down.sql
-- =============================================================================

BEGIN;

CREATE TABLE care_device_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    person_id       UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    device_id       UUID NOT NULL REFERENCES care_devices(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 120),
    kind            TEXT NOT NULL CHECK (kind IN ('help', 'fall_started', 'fall_ok', 'checkin_answer', 'checkin_missed')),
    status_code     SMALLINT NOT NULL DEFAULT 200 CHECK (status_code BETWEEN 100 AND 599),
    response        JSONB NOT NULL DEFAULT '{}'::jsonb,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_device_events_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT care_device_events_once_per_key UNIQUE (device_id, idempotency_key)
);
CREATE INDEX idx_care_device_events_person ON care_device_events (tenant_id, workspace_id, person_id, received_at DESC);

ALTER TABLE care_device_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE care_device_events FORCE ROW LEVEL SECURITY;
CREATE POLICY care_device_events_select ON care_device_events FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_device_events_insert ON care_device_events FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_device_events_update ON care_device_events FOR UPDATE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_device_events_delete ON care_device_events FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
REVOKE ALL ON care_device_events FROM PUBLIC;
REVOKE ALL ON care_device_events FROM ira_app, ira_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON care_device_events TO ira_app, ira_worker;

ALTER TABLE care_acknowledgements DROP CONSTRAINT care_acknowledgements_kind_check;
ALTER TABLE care_acknowledgements ADD CONSTRAINT care_acknowledgements_kind_check
    CHECK (kind IN ('on_my_way', 'arrived', 'checked_in_person', 'spoke_to_person', 'false_alarm', 'handed_over'));

ALTER TABLE care_safety_settings
    ADD COLUMN IF NOT EXISTS reading_upload_interval_s INTEGER NOT NULL DEFAULT 60
        CHECK (reading_upload_interval_s BETWEEN 15 AND 900);

COMMIT;
