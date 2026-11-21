-- =============================================================================
-- 104_care_conversation.sql — IRA CARE: IRA really talks
--
--   care_conversation_memory   one row per person per day: the rolling summary of
--                              what was said, so IRA remembers earlier today and
--                              this week. Summaries only, never the person's words
--                              verbatim (D-020). Removed with the person, by the
--                              retention purge, and with the tenant.
--   care_alerts.kind + 'concerning_phrase'
--                              "I fell", "I can't breathe", "chest pain" in an answer go
--                              straight to "ask first" and then the escalation chain.
--   care_checkins.question_source
--                              'template' or 'model': which path composed the question.
--
-- Rollback: rollback/104_care_conversation_down.sql
-- =============================================================================

BEGIN;

CREATE TABLE care_conversation_memory (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    person_id       UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    day             DATE NOT NULL,
    summary         TEXT NOT NULL DEFAULT '' CHECK (length(summary) <= 4000),
    exchanges       INTEGER NOT NULL DEFAULT 0 CHECK (exchanges >= 0),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_conversation_memory_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT care_conversation_memory_once_per_day UNIQUE (person_id, day)
);
CREATE INDEX idx_care_conversation_memory_person ON care_conversation_memory (tenant_id, workspace_id, person_id, day DESC);

ALTER TABLE care_conversation_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE care_conversation_memory FORCE ROW LEVEL SECURITY;
CREATE POLICY care_conversation_memory_select ON care_conversation_memory FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_conversation_memory_insert ON care_conversation_memory FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_conversation_memory_update ON care_conversation_memory FOR UPDATE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_conversation_memory_delete ON care_conversation_memory FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
REVOKE ALL ON care_conversation_memory FROM PUBLIC;
REVOKE ALL ON care_conversation_memory FROM ira_app, ira_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON care_conversation_memory TO ira_app, ira_worker;

ALTER TABLE care_checkins
    ADD COLUMN IF NOT EXISTS question_source TEXT NOT NULL DEFAULT 'template'
        CHECK (question_source IN ('template', 'model'));

ALTER TABLE care_alerts DROP CONSTRAINT care_alerts_kind_check;
ALTER TABLE care_alerts ADD CONSTRAINT care_alerts_kind_check
    CHECK (kind IN ('help_requested', 'no_answer_no_movement', 'heart_rate_outside_range',
        'off_wrist_during_check', 'missed_checkins', 'off_wrist', 'low_battery',
        'sensor_permission_off', 'no_sync', 'manual', 'off_wrist_night', 'suspected_fall',
        'concerning_phrase'));

COMMIT;
