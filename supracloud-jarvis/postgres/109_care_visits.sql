-- =============================================================================
-- 109_care_visits.sql — IRA CARE: scheduled care visits
--
--   care_visits
--     One planned visit by a carer to a person: who, the time window, what is
--     planned, and what happened: arrival, departure, outcome, notes and a
--     handover for the next carer. Statuses: planned -> arrived -> departed;
--     planned -> late (no arrival `visit_late_minutes` after the window opened)
--     -> missed (no arrival by the window's end); cancelled.
--   care_visit_tasks
--     The planned tasks of a visit and whether each was done.
--   care_safety_settings.visit_late_minutes     when a visit counts as late (15)
--   care_alerts.kind + 'visit_late' (a notice to the coordinator) and 'visit_missed'
--     (urgent: escalates through the care plan like any other alert)
--
-- Rollback: rollback/109_care_visits_down.sql
-- =============================================================================

BEGIN;

CREATE TABLE care_visits (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL
                            DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                            REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id        UUID NOT NULL
                            DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    person_id           UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    carer_member_id     UUID REFERENCES care_circle_members(id) ON DELETE SET NULL,
    carer_name          TEXT NOT NULL DEFAULT '' CHECK (length(carer_name) <= 80),
    planned_start       TIMESTAMPTZ NOT NULL,
    planned_end         TIMESTAMPTZ NOT NULL,
    purpose             TEXT NOT NULL DEFAULT '' CHECK (length(purpose) <= 200),
    status              TEXT NOT NULL DEFAULT 'planned'
                            CHECK (status IN ('planned', 'late', 'arrived', 'departed', 'missed', 'cancelled')),
    arrived_at          TIMESTAMPTZ,
    acknowledged_at     TIMESTAMPTZ,
    departed_at         TIMESTAMPTZ,
    late_alerted_at     TIMESTAMPTZ,
    missed_alerted_at   TIMESTAMPTZ,
    outcome             TEXT NOT NULL DEFAULT ''
                            CHECK (outcome IN ('', 'all_well', 'concern_noted', 'needed_help', 'not_at_home', 'declined', 'other')),
    notes               TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 4000),
    handover            TEXT NOT NULL DEFAULT '' CHECK (length(handover) <= 4000),
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_visits_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT care_visits_window CHECK (planned_end > planned_start)
);
CREATE INDEX idx_care_visits_person_time ON care_visits (tenant_id, workspace_id, person_id, planned_start DESC);
CREATE INDEX idx_care_visits_open ON care_visits (tenant_id, workspace_id, planned_start)
    WHERE status IN ('planned', 'late', 'arrived');

ALTER TABLE care_visits ENABLE ROW LEVEL SECURITY;
ALTER TABLE care_visits FORCE ROW LEVEL SECURITY;
CREATE POLICY care_visits_select ON care_visits FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_visits_insert ON care_visits FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_visits_update ON care_visits FOR UPDATE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_visits_delete ON care_visits FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
REVOKE ALL ON care_visits FROM PUBLIC;
REVOKE ALL ON care_visits FROM ira_app, ira_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON care_visits TO ira_app, ira_worker;

CREATE TABLE care_visit_tasks (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    visit_id        UUID NOT NULL REFERENCES care_visits(id) ON DELETE CASCADE,
    title           TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    position        SMALLINT NOT NULL DEFAULT 0,
    done            BOOLEAN NOT NULL DEFAULT FALSE,
    done_at         TIMESTAMPTZ,
    note            TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 400),
    CONSTRAINT care_visit_tasks_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_visit_tasks_visit ON care_visit_tasks (tenant_id, workspace_id, visit_id, position);

ALTER TABLE care_visit_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE care_visit_tasks FORCE ROW LEVEL SECURITY;
CREATE POLICY care_visit_tasks_select ON care_visit_tasks FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_visit_tasks_insert ON care_visit_tasks FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_visit_tasks_update ON care_visit_tasks FOR UPDATE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_visit_tasks_delete ON care_visit_tasks FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
REVOKE ALL ON care_visit_tasks FROM PUBLIC;
REVOKE ALL ON care_visit_tasks FROM ira_app, ira_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON care_visit_tasks TO ira_app, ira_worker;

ALTER TABLE care_safety_settings
    ADD COLUMN IF NOT EXISTS visit_late_minutes INTEGER NOT NULL DEFAULT 15
        CHECK (visit_late_minutes BETWEEN 5 AND 120);

ALTER TABLE care_alerts DROP CONSTRAINT care_alerts_kind_check;
ALTER TABLE care_alerts ADD CONSTRAINT care_alerts_kind_check
    CHECK (kind IN ('help_requested', 'no_answer_no_movement', 'heart_rate_outside_range',
        'off_wrist_during_check', 'missed_checkins', 'off_wrist', 'low_battery',
        'sensor_permission_off', 'no_sync', 'manual', 'off_wrist_night', 'suspected_fall',
        'concerning_phrase', 'visit_late', 'visit_missed'));

COMMIT;
