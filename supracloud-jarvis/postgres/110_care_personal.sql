-- =============================================================================
-- 110_care_personal.sql — IRA CARE: IRA, personal to the person
--
--   care_communication_profiles  how IRA speaks to this person: pace, how much
--                                prompting, which channels (voice, touch,
--                                vibration); with consent, topics to avoid,
--                                familiar topics, and who to offer when a worry
--                                repeats. One row per person; edited by the
--                                person or their representative.
--   care_reminders               real reminders: from conversation ("remind me
--                                to ring Sravani at three"), a carer, or the
--                                hydration routine; each asked on the watch as
--                                a check-in, then confirmed, snoozed or skipped.
--                                A medication reminder confirms in two steps.
--   care_checkins.reminder_id    the reminder a check-in asks about
--   care_checkins.question_kind  + 'reminder', 'hydration'
--   care_consents.kind           + 'topics' (familiar topics and topics to avoid may be used)
--   care_conversation_memory     + edited_at, edited_by (the person's own hand on what IRA remembers)
--   care_safety_settings         + hydration_every_minutes (0 = off)
--
-- Rollback: rollback/110_care_personal_down.sql
-- =============================================================================

BEGIN;

CREATE TABLE care_communication_profiles (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL
                            DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                            REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id        UUID NOT NULL
                            DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    person_id           UUID NOT NULL UNIQUE REFERENCES care_people(id) ON DELETE CASCADE,
    pace                TEXT NOT NULL DEFAULT 'normal' CHECK (pace IN ('slow', 'normal')),
    prompting_level     TEXT NOT NULL DEFAULT 'standard' CHECK (prompting_level IN ('light', 'standard', 'full')),
    voice_on            BOOLEAN NOT NULL DEFAULT TRUE,
    touch_on            BOOLEAN NOT NULL DEFAULT TRUE,
    vibration_on        BOOLEAN NOT NULL DEFAULT TRUE,
    topics_avoid        TEXT[] NOT NULL DEFAULT '{}',
    familiar_topics     TEXT[] NOT NULL DEFAULT '{}',
    redirect_member_id  UUID REFERENCES care_circle_members(id) ON DELETE SET NULL,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by_role     TEXT NOT NULL DEFAULT '' CHECK (updated_by_role IN ('', 'person', 'representative', 'carer', 'admin')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_communication_profiles_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT care_communication_profiles_topic_lists CHECK (
        cardinality(topics_avoid) <= 20 AND cardinality(familiar_topics) <= 20)
);
CREATE INDEX idx_care_communication_profiles_scope ON care_communication_profiles (tenant_id, workspace_id, person_id);

ALTER TABLE care_communication_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE care_communication_profiles FORCE ROW LEVEL SECURITY;
CREATE POLICY care_communication_profiles_select ON care_communication_profiles FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_communication_profiles_insert ON care_communication_profiles FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_communication_profiles_update ON care_communication_profiles FOR UPDATE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_communication_profiles_delete ON care_communication_profiles FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
REVOKE ALL ON care_communication_profiles FROM PUBLIC;
REVOKE ALL ON care_communication_profiles FROM ira_app, ira_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON care_communication_profiles TO ira_app, ira_worker;

CREATE TABLE care_reminders (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    person_id       UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL DEFAULT 'reminder' CHECK (kind IN ('reminder', 'medication', 'hydration')),
    text            TEXT NOT NULL DEFAULT '' CHECK (length(text) <= 200),
    medication_id   UUID REFERENCES care_medications(id) ON DELETE CASCADE,
    due_at          TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'asked', 'confirming', 'confirmed', 'snoozed', 'skipped', 'missed', 'cancelled')),
    snoozed_until   TIMESTAMPTZ,
    source          TEXT NOT NULL DEFAULT 'conversation' CHECK (source IN ('conversation', 'carer', 'family', 'routine')),
    asked_count     SMALLINT NOT NULL DEFAULT 0,
    done_at         TIMESTAMPTZ,
    created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_reminders_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_reminders_due ON care_reminders (tenant_id, workspace_id, person_id, due_at)
    WHERE status IN ('pending', 'snoozed', 'asked', 'confirming');
CREATE INDEX idx_care_reminders_person ON care_reminders (tenant_id, workspace_id, person_id, created_at DESC);

ALTER TABLE care_reminders ENABLE ROW LEVEL SECURITY;
ALTER TABLE care_reminders FORCE ROW LEVEL SECURITY;
CREATE POLICY care_reminders_select ON care_reminders FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_reminders_insert ON care_reminders FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_reminders_update ON care_reminders FOR UPDATE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_reminders_delete ON care_reminders FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
REVOKE ALL ON care_reminders FROM PUBLIC;
REVOKE ALL ON care_reminders FROM ira_app, ira_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON care_reminders TO ira_app, ira_worker;

ALTER TABLE care_checkins ADD COLUMN IF NOT EXISTS reminder_id UUID REFERENCES care_reminders(id) ON DELETE SET NULL;
ALTER TABLE care_checkins DROP CONSTRAINT care_checkins_question_kind_check;
ALTER TABLE care_checkins ADD CONSTRAINT care_checkins_question_kind_check
    CHECK (question_kind IN ('meal', 'medication', 'mood', 'pain', 'plans', 'wellbeing', 'custom',
                             'safety', 'morning', 'reminder', 'hydration'));

ALTER TABLE care_consents DROP CONSTRAINT care_consents_kind_check;
ALTER TABLE care_consents ADD CONSTRAINT care_consents_kind_check
    CHECK (kind IN ('transcripts_family', 'transcripts_carer', 'transcripts_organisation',
                    'health_data_family', 'data_retention', 'topics'));

ALTER TABLE care_conversation_memory
    ADD COLUMN IF NOT EXISTS edited_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS edited_by TEXT NOT NULL DEFAULT '' CHECK (edited_by IN ('', 'person', 'representative'));

ALTER TABLE care_safety_settings
    ADD COLUMN IF NOT EXISTS hydration_every_minutes INTEGER NOT NULL DEFAULT 0
        CHECK (hydration_every_minutes = 0 OR hydration_every_minutes BETWEEN 60 AND 360);

COMMIT;
