-- =============================================================================
-- 099_care_foundation.sql — IRA CARE data model (phase 2)
--
-- The tables IRA CARE needs that the foundation did not have: the person who is
-- cared for, the circle of people around them, the watch, the readings it
-- sends, the check-ins, the alerts and who acknowledged them, routines,
-- medication, transcripts, consents and the record of who looked at what.
--
-- ── Boundary ───────────────────────────────────────────────────────────────
--
-- Every table is scoped to a tenant AND a workspace, like the seventeen content
-- tables `049` ratcheted: a family or a care organisation is a tenant, and its
-- workspace is where its people live. Both columns default from the session
-- settings the connection helpers set, so a writer that forgot them does not
-- write into nobody's workspace: the NOT NULL fails instead.
--
-- Row-level security is FORCED on all fifteen tables, so the schema owner is
-- bound too. The runtime role gets exactly the statements each table needs:
--
--   * readings, attempts, transcripts and the access log are never UPDATEd;
--   * the access log is append-only: a trigger refuses UPDATE and DELETE even
--     from the owner, the same shape as the other audit tables;
--   * alert attempts are never deleted (they are the record of what was tried).
--
-- ── Erasure ────────────────────────────────────────────────────────────────
--
-- These rows belong to the PERSON CARED FOR, who is not usually an account
-- holder. A family member deleting their own account must not take the
-- person's health history with them, so none of these tables is in the
-- account erasure scope. Every content table carries `tenant_id REFERENCES
-- tenants ON DELETE CASCADE`, so deleting the tenant removes them, and the
-- care package owns a per-person erasure of its own. The access log is the
-- exception: audit evidence is retained and redacted, never cascade-deleted,
-- so its keys are RESTRICT. The sweep in
-- tests/readiness/test_erasure_coverage.py records each table as exempt with
-- these reasons.
--
-- ── What is deliberately absent ────────────────────────────────────────────
--
-- No medical thresholds. `care_safety_settings` holds the range a carer or
-- clinician set for one person, nullable: an unset range means no heart-rate
-- rule, never a built-in one. No audio: `care_transcripts` holds text only.
--
-- Rollback: rollback/099_care_foundation_down.sql
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.workspaces') IS NULL OR to_regclass('public.tenants') IS NULL THEN
        RAISE EXCEPTION '099 REFUSING: tenants/workspaces do not exist. Run the earlier migrations first.';
    END IF;
    IF to_regclass('public.care_people') IS NOT NULL THEN
        RAISE EXCEPTION '099 REFUSING: care_people already exists; this migration will not redefine a table it did not create.';
    END IF;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. The person cared for
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE care_people (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    display_name    TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 80),
    preferred_name  TEXT NOT NULL DEFAULT '' CHECK (length(preferred_name) <= 40),
    date_of_birth   DATE,
    timezone        TEXT NOT NULL DEFAULT 'Europe/London' CHECK (length(timezone) BETWEEN 1 AND 64),
    language        TEXT NOT NULL DEFAULT 'en' CHECK (language ~ '^[a-z]{2}(-[A-Za-z]{2,8})?$'),
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'archived')),
    notes           TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 4000),
    created_by      UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_people_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_people_scope ON care_people (tenant_id, workspace_id, status);

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. The care circle: who is around the person, and in what order they are
--    called. `tier` is the escalation tier (1 family and carer, 2 the care
--    organisation, 3 emergency); `priority` orders members within a tier.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE care_circle_members (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    person_id       UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    user_id         UUID REFERENCES users(id) ON DELETE SET NULL,
    display_name    TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 80),
    relationship    TEXT NOT NULL DEFAULT '' CHECK (length(relationship) <= 40),
    role            TEXT NOT NULL CHECK (role IN ('family', 'carer', 'clinician', 'care_organisation')),
    tier            SMALLINT NOT NULL DEFAULT 1 CHECK (tier BETWEEN 1 AND 3),
    priority        INTEGER NOT NULL DEFAULT 100,
    phone           TEXT NOT NULL DEFAULT '' CHECK (phone = '' OR phone ~ '^\+[1-9][0-9]{6,14}$'),
    email           TEXT NOT NULL DEFAULT '' CHECK (length(email) <= 254),
    push_enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    sms_enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    call_enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_circle_members_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_circle_members_person ON care_circle_members (tenant_id, workspace_id, person_id, tier, priority);
CREATE INDEX idx_care_circle_members_user ON care_circle_members (user_id) WHERE user_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Devices. A watch is paired with a short code; once paired it holds a
--    token whose hash is stored here. Neither the code nor the token is ever
--    stored in clear.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE care_devices (
    id                              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id                       UUID NOT NULL
                                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id                    UUID NOT NULL
                                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    person_id                       UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    platform                        TEXT NOT NULL DEFAULT 'wearos' CHECK (platform IN ('wearos', 'watchos', 'other')),
    model                           TEXT NOT NULL DEFAULT '' CHECK (length(model) <= 80),
    status                          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paired', 'revoked')),
    pairing_code_hash               TEXT CHECK (pairing_code_hash IS NULL OR pairing_code_hash ~ '^[0-9a-f]{64}$'),
    pairing_expires_at              TIMESTAMPTZ,
    token_hash                      TEXT CHECK (token_hash IS NULL OR token_hash ~ '^[0-9a-f]{64}$'),
    paired_at                       TIMESTAMPTZ,
    last_seen_at                    TIMESTAMPTZ,
    last_sync_at                    TIMESTAMPTZ,
    battery_percent                 SMALLINT CHECK (battery_percent IS NULL OR battery_percent BETWEEN 0 AND 100),
    on_wrist                        BOOLEAN,
    background_sensor_permission    BOOLEAN,
    expected_report_interval_s      INTEGER NOT NULL DEFAULT 300 CHECK (expected_report_interval_s BETWEEN 30 AND 86400),
    app_version                     TEXT NOT NULL DEFAULT '' CHECK (length(app_version) <= 40),
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_devices_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT care_devices_paired_has_token
        CHECK (status <> 'paired' OR token_hash IS NOT NULL)
);
CREATE UNIQUE INDEX idx_care_devices_token ON care_devices (token_hash) WHERE token_hash IS NOT NULL;
CREATE INDEX idx_care_devices_person ON care_devices (tenant_id, workspace_id, person_id, status);

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Readings: a time series. Never updated; deleted only by retention or by
--    the care package's per-person erasure. A watch that re-sends its offline
--    queue must not double-count, hence the unique index.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE care_vitals (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    person_id       UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    device_id       UUID REFERENCES care_devices(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('heart_rate', 'steps', 'movement', 'on_wrist', 'battery', 'skin_temperature')),
    value           DOUBLE PRECISION NOT NULL,
    unit            TEXT NOT NULL DEFAULT '' CHECK (length(unit) <= 16),
    recorded_at     TIMESTAMPTZ NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source          TEXT NOT NULL DEFAULT 'watch' CHECK (source IN ('watch', 'phone', 'manual', 'simulator')),
    CONSTRAINT care_vitals_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_vitals_series ON care_vitals (tenant_id, workspace_id, person_id, kind, recorded_at DESC);
CREATE UNIQUE INDEX idx_care_vitals_dedupe ON care_vitals (device_id, kind, recorded_at) WHERE device_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Safety settings: one row per person. The heart-rate range is NULL until a
--    carer or clinician sets it; there is no default range anywhere.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE care_safety_settings (
    person_id                           UUID PRIMARY KEY REFERENCES care_people(id) ON DELETE CASCADE,
    tenant_id                           UUID NOT NULL
                                            DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                                            REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id                        UUID NOT NULL
                                            DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    quiet_start                         TIME NOT NULL DEFAULT '22:00',
    quiet_end                           TIME NOT NULL DEFAULT '07:00',
    checkin_min_minutes                 INTEGER NOT NULL DEFAULT 15 CHECK (checkin_min_minutes BETWEEN 5 AND 240),
    checkin_max_minutes                 INTEGER NOT NULL DEFAULT 20 CHECK (checkin_max_minutes BETWEEN 5 AND 240),
    repeat_after_s                      INTEGER NOT NULL DEFAULT 60 CHECK (repeat_after_s BETWEEN 10 AND 600),
    sensor_check_after_s                INTEGER NOT NULL DEFAULT 120 CHECK (sensor_check_after_s BETWEEN 10 AND 900),
    heart_rate_min                      INTEGER CHECK (heart_rate_min IS NULL OR heart_rate_min BETWEEN 20 AND 250),
    heart_rate_max                      INTEGER CHECK (heart_rate_max IS NULL OR heart_rate_max BETWEEN 20 AND 250),
    no_movement_window_s                INTEGER NOT NULL DEFAULT 180 CHECK (no_movement_window_s BETWEEN 30 AND 3600),
    movement_floor_steps                INTEGER NOT NULL DEFAULT 1 CHECK (movement_floor_steps BETWEEN 0 AND 10000),
    off_wrist_alert_minutes             INTEGER NOT NULL DEFAULT 30 CHECK (off_wrist_alert_minutes BETWEEN 1 AND 1440),
    battery_alert_percent               INTEGER NOT NULL DEFAULT 15 CHECK (battery_alert_percent BETWEEN 1 AND 99),
    sync_alert_minutes                  INTEGER NOT NULL DEFAULT 30 CHECK (sync_alert_minutes BETWEEN 1 AND 1440),
    missed_checkins_before_escalation   INTEGER NOT NULL DEFAULT 3 CHECK (missed_checkins_before_escalation BETWEEN 1 AND 20),
    ack_wait_minutes                    INTEGER NOT NULL DEFAULT 5 CHECK (ack_wait_minutes BETWEEN 1 AND 60),
    set_by                              UUID,
    updated_at                          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_safety_settings_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT care_safety_settings_checkin_window CHECK (checkin_max_minutes >= checkin_min_minutes),
    CONSTRAINT care_safety_settings_heart_rate_range
        CHECK (heart_rate_min IS NULL OR heart_rate_max IS NULL OR heart_rate_max > heart_rate_min)
);
CREATE INDEX idx_care_safety_settings_scope ON care_safety_settings (tenant_id, workspace_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. Check-ins: one row per question asked (or scheduled), with the answer.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE care_checkins (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    person_id       UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    scheduled_at    TIMESTAMPTZ NOT NULL,
    asked_at        TIMESTAMPTZ,
    repeated_at     TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'scheduled'
                        CHECK (status IN ('scheduled', 'asked', 'repeated', 'answered', 'missed',
                                          'escalated', 'skipped_quiet', 'cancelled')),
    question_kind   TEXT NOT NULL CHECK (question_kind IN ('meal', 'medication', 'mood', 'pain', 'plans', 'wellbeing', 'custom')),
    question_text   TEXT NOT NULL DEFAULT '' CHECK (length(question_text) <= 400),
    answered_at     TIMESTAMPTZ,
    answer_text     TEXT NOT NULL DEFAULT '' CHECK (length(answer_text) <= 4000),
    answer_summary  TEXT NOT NULL DEFAULT '' CHECK (length(answer_summary) <= 400),
    answered_via    TEXT NOT NULL DEFAULT 'none' CHECK (answered_via IN ('voice', 'button', 'phone', 'none')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_checkins_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_checkins_person_time ON care_checkins (tenant_id, workspace_id, person_id, scheduled_at DESC);
CREATE INDEX idx_care_checkins_due ON care_checkins (tenant_id, workspace_id, status, scheduled_at)
    WHERE status IN ('scheduled', 'asked', 'repeated');

-- ─────────────────────────────────────────────────────────────────────────────
-- 7. Alerts, the attempts made to reach somebody, and the acknowledgements.
--    `kind` names what was OBSERVED, never what it might mean.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE care_alerts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    person_id       UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    checkin_id      UUID REFERENCES care_checkins(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL CHECK (kind IN (
                        'help_requested', 'no_answer_no_movement', 'heart_rate_outside_range',
                        'off_wrist_during_check', 'missed_checkins', 'off_wrist', 'low_battery',
                        'sensor_permission_off', 'no_sync', 'manual')),
    severity        TEXT NOT NULL CHECK (severity IN ('notice', 'urgent', 'sos')),
    status          TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'escalating', 'acknowledged', 'resolved', 'cancelled')),
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    details         JSONB NOT NULL DEFAULT '{}'::jsonb,
    current_tier    SMALLINT NOT NULL DEFAULT 0 CHECK (current_tier BETWEEN 0 AND 3),
    next_action_at  TIMESTAMPTZ,
    acknowledged_at TIMESTAMPTZ,
    resolved_at     TIMESTAMPTZ,
    resolution      TEXT NOT NULL DEFAULT '' CHECK (length(resolution) <= 1000),
    CONSTRAINT care_alerts_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_alerts_person_time ON care_alerts (tenant_id, workspace_id, person_id, opened_at DESC);
CREATE INDEX idx_care_alerts_open ON care_alerts (tenant_id, workspace_id, next_action_at)
    WHERE status IN ('open', 'escalating');

CREATE TABLE care_alert_attempts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    alert_id        UUID NOT NULL REFERENCES care_alerts(id) ON DELETE CASCADE,
    tier            SMALLINT NOT NULL CHECK (tier BETWEEN 1 AND 3),
    member_id       UUID REFERENCES care_circle_members(id) ON DELETE SET NULL,
    channel         TEXT NOT NULL CHECK (channel IN ('push', 'sms', 'call', 'email', 'dashboard', 'monitoring_centre')),
    outcome         TEXT NOT NULL CHECK (outcome IN ('logged', 'sent', 'delivered', 'failed', 'skipped')),
    detail          TEXT NOT NULL DEFAULT '' CHECK (length(detail) <= 1000),
    provider_ref    TEXT NOT NULL DEFAULT '' CHECK (length(provider_ref) <= 200),
    attempted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_alert_attempts_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_alert_attempts_alert ON care_alert_attempts (tenant_id, workspace_id, alert_id, attempted_at);

CREATE TABLE care_acknowledgements (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    alert_id        UUID NOT NULL REFERENCES care_alerts(id) ON DELETE CASCADE,
    member_id       UUID REFERENCES care_circle_members(id) ON DELETE SET NULL,
    user_id         UUID REFERENCES users(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('on_my_way', 'checked_in_person', 'spoke_to_person', 'false_alarm', 'handed_over')),
    note            TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 1000),
    acknowledged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_acknowledgements_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_acknowledgements_alert ON care_acknowledgements (tenant_id, workspace_id, alert_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- 8. Routines and medication
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE care_routines (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    person_id       UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    name            TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    kind            TEXT NOT NULL CHECK (kind IN ('meal', 'medication', 'activity', 'sleep', 'social', 'other')),
    time_of_day     TIME,
    days_of_week    SMALLINT[] NOT NULL DEFAULT '{1,2,3,4,5,6,7}',
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_routines_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_routines_person ON care_routines (tenant_id, workspace_id, person_id, active);

CREATE TABLE care_medications (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    person_id       UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    name            TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    dose            TEXT NOT NULL DEFAULT '' CHECK (length(dose) <= 120),
    times           TIME[] NOT NULL DEFAULT '{}',
    notes           TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 1000),
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_medications_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_medications_person ON care_medications (tenant_id, workspace_id, person_id, active);

CREATE TABLE care_medication_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    medication_id   UUID NOT NULL REFERENCES care_medications(id) ON DELETE CASCADE,
    person_id       UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    checkin_id      UUID REFERENCES care_checkins(id) ON DELETE SET NULL,
    due_at          TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL DEFAULT 'due' CHECK (status IN ('due', 'taken', 'missed', 'skipped', 'unknown')),
    confirmed_via   TEXT NOT NULL DEFAULT 'none' CHECK (confirmed_via IN ('checkin', 'family', 'carer', 'device', 'none')),
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_medication_events_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_medication_events_person ON care_medication_events (tenant_id, workspace_id, person_id, due_at DESC);
CREATE UNIQUE INDEX idx_care_medication_events_due ON care_medication_events (medication_id, due_at);

-- ─────────────────────────────────────────────────────────────────────────────
-- 9. Transcripts (text only, never audio) and consents
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE care_transcripts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    person_id       UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    checkin_id      UUID REFERENCES care_checkins(id) ON DELETE SET NULL,
    speaker         TEXT NOT NULL CHECK (speaker IN ('person', 'companion')),
    text            TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 8000),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_transcripts_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_transcripts_person_time ON care_transcripts (tenant_id, workspace_id, person_id, created_at DESC);

CREATE TABLE care_consents (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL
                            DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                            REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id        UUID NOT NULL
                            DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    person_id           UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL CHECK (kind IN (
                            'transcripts_family', 'transcripts_carer', 'transcripts_organisation',
                            'health_data_family', 'data_retention')),
    granted             BOOLEAN NOT NULL,
    granted_by          TEXT NOT NULL CHECK (granted_by IN ('person', 'legal_representative')),
    granted_by_name     TEXT NOT NULL DEFAULT '' CHECK (length(granted_by_name) <= 80),
    retention_months    INTEGER CHECK (retention_months IS NULL OR retention_months BETWEEN 1 AND 120),
    note                TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 1000),
    recorded_by         UUID,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at          TIMESTAMPTZ,
    CONSTRAINT care_consents_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_consents_person ON care_consents (tenant_id, workspace_id, person_id, kind, recorded_at DESC);

-- ─────────────────────────────────────────────────────────────────────────────
-- 10. The access log: who looked at health data or a transcript. Append-only.
--     No foreign keys: an audit row must outlive what it is about.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE care_access_log (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- RESTRICT, not CASCADE: audit evidence is retained and redacted, never
    -- silently destroyed with the tenant (the same rule as trust_ledger).
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE RESTRICT,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    actor_user_id   UUID,
    actor_label     TEXT NOT NULL DEFAULT '' CHECK (length(actor_label) <= 80),
    person_id       UUID,
    resource_kind   TEXT NOT NULL CHECK (resource_kind IN (
                        'vitals', 'transcript', 'checkins', 'alerts', 'medication', 'report', 'person', 'consent')),
    resource_id     UUID,
    action          TEXT NOT NULL DEFAULT 'view' CHECK (action IN ('view', 'export', 'search')),
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_access_log_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE RESTRICT
);
CREATE INDEX idx_care_access_log_person_time ON care_access_log (tenant_id, workspace_id, person_id, at DESC);
CREATE INDEX idx_care_access_log_actor_time ON care_access_log (tenant_id, workspace_id, actor_user_id, at DESC);

-- UPDATE is refused for everyone: an audit row is never rewritten. DELETE is
-- refused for the runtime roles and allowed only to the schema owner and the
-- migrator, which is how the retention purge (an operator's deliberate act,
-- outside the application) can eventually remove rows older than the audit
-- retention period. The same role test 080 uses for payment events.
CREATE OR REPLACE FUNCTION care_access_log_append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND current_user IN ('ira_migrator', 'ira_schema_owner', 'postgres') THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'care_access_log is append-only: % is refused', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END $$;
REVOKE EXECUTE ON FUNCTION care_access_log_append_only() FROM PUBLIC;

CREATE TRIGGER care_access_log_no_update
    BEFORE UPDATE ON care_access_log FOR EACH ROW EXECUTE FUNCTION care_access_log_append_only();
CREATE TRIGGER care_access_log_no_delete
    BEFORE DELETE ON care_access_log FOR EACH ROW EXECUTE FUNCTION care_access_log_append_only();

-- ─────────────────────────────────────────────────────────────────────────────
-- 11. Row-level security: tenant AND workspace, FORCED, on every table.
--     The runtime role is granted exactly what each table needs.
-- ─────────────────────────────────────────────────────────────────────────────
DO $$
DECLARE
    t TEXT;
    pred CONSTANT TEXT :=
        'tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid '
        'AND workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid';
    -- Tables whose rows may be changed after they are written.
    updatable CONSTANT TEXT[] := ARRAY[
        'care_people', 'care_circle_members', 'care_devices', 'care_safety_settings',
        'care_checkins', 'care_alerts', 'care_routines', 'care_medications',
        'care_medication_events', 'care_consents'];
    -- Tables whose rows may be deleted by the runtime (management, retention,
    -- the care package's per-person erasure).
    deletable CONSTANT TEXT[] := ARRAY[
        'care_people', 'care_circle_members', 'care_devices', 'care_safety_settings',
        'care_vitals', 'care_checkins', 'care_alerts', 'care_acknowledgements',
        'care_routines', 'care_medications', 'care_medication_events',
        'care_transcripts', 'care_consents'];
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'care_people', 'care_circle_members', 'care_devices', 'care_vitals',
        'care_safety_settings', 'care_checkins', 'care_alerts', 'care_alert_attempts',
        'care_acknowledgements', 'care_routines', 'care_medications',
        'care_medication_events', 'care_transcripts', 'care_consents', 'care_access_log']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY %I ON %I FOR SELECT USING (%s)', t || '_select', t, pred);
        EXECUTE format('CREATE POLICY %I ON %I FOR INSERT WITH CHECK (%s)', t || '_insert', t, pred);
        IF t = ANY (updatable) THEN
            EXECUTE format('CREATE POLICY %I ON %I FOR UPDATE USING (%s) WITH CHECK (%s)',
                           t || '_update', t, pred, pred);
        END IF;
        IF t = ANY (deletable) THEN
            EXECUTE format('CREATE POLICY %I ON %I FOR DELETE USING (%s)', t || '_delete', t, pred);
        END IF;

        EXECUTE format('REVOKE ALL ON %I FROM PUBLIC', t);
        EXECUTE format('REVOKE ALL ON %I FROM ira_app, ira_worker', t);
        EXECUTE format('GRANT SELECT, INSERT ON %I TO ira_app, ira_worker', t);
        IF t = ANY (updatable) THEN
            EXECUTE format('GRANT UPDATE ON %I TO ira_app, ira_worker', t);
        END IF;
        IF t = ANY (deletable) THEN
            EXECUTE format('GRANT DELETE ON %I TO ira_app, ira_worker', t);
        END IF;
    END LOOP;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 12. Self-check: fifteen tables, every one forced, every one with a tenant and
--     a workspace column, the access log with no update or delete policy.
-- ─────────────────────────────────────────────────────────────────────────────
DO $$
DECLARE
    n INTEGER;
BEGIN
    SELECT count(*) INTO n
      FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace
     WHERE ns.nspname = 'public' AND c.relkind = 'r' AND c.relname LIKE 'care\_%'
       AND c.relrowsecurity AND c.relforcerowsecurity;
    IF n <> 15 THEN
        RAISE EXCEPTION '099 FAILED: expected 15 forced care tables, found %', n;
    END IF;
    SELECT count(*) INTO n FROM pg_policy
     WHERE polrelid = 'care_access_log'::regclass AND polcmd IN ('w', 'd');
    IF n <> 0 THEN
        RAISE EXCEPTION '099 FAILED: care_access_log carries an UPDATE or DELETE policy';
    END IF;
END $$;

COMMIT;
