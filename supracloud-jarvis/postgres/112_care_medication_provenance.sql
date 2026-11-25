-- =============================================================================
-- 112_care_medication_provenance.sql — IRA CARE: a dose is bound, attributed and
-- corrected in the open (safety remediation A3d, A3e)
--
--   care_checkins.medication_id, .dose_due_at
--       A medication check-in names the ONE medicine and the ONE scheduled dose
--       it asks about, at the moment it is asked. An answer is bound to that,
--       never re-derived from words or the nearest schedule. Both nullable: a
--       check-in about anything else, and every row before this migration,
--       leaves them empty, and the code then falls back to the conservative
--       re-derivation (which writes nothing when in doubt).
--
--   care_medication_events.source, .recorded_by
--       Who said the dose happened, from the verified actor: the person
--       themself ('self_report'), family ('family_report'), a carer
--       ('carer_report'), a carer who watched it being taken
--       ('carer_observed'), a device ('device'), the system ('system', e.g. a
--       reminder nobody answered). Rows from before this migration are
--       'legacy': their provenance was never recorded and is not guessed.
--       `confirmed_via` stays, unchanged, for every existing reader.
--
--   care_medication_event_corrections
--       Append-only history: every change to a dose already recorded keeps what
--       it was, what it became, who changed it, and why. A correction is never a
--       silent overwrite. Removed with its event (retention, person erasure,
--       tenant deletion) by ON DELETE CASCADE.
--
-- Additive only: two nullable columns, two columns with defaults, one new
-- table. No existing row or constraint changes meaning.
-- Rollback: rollback/112_care_medication_provenance_down.sql
-- =============================================================================

BEGIN;

ALTER TABLE care_checkins
    ADD COLUMN IF NOT EXISTS medication_id UUID REFERENCES care_medications(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS dose_due_at   TIMESTAMPTZ;
ALTER TABLE care_checkins ADD CONSTRAINT care_checkins_dose_binding_whole
    CHECK ((medication_id IS NULL) = (dose_due_at IS NULL));

ALTER TABLE care_medication_events
    ADD COLUMN IF NOT EXISTS source      TEXT NOT NULL DEFAULT 'legacy',
    ADD COLUMN IF NOT EXISTS recorded_by UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE care_medication_events ADD CONSTRAINT care_medication_events_source_check
    CHECK (source IN ('self_report', 'family_report', 'carer_report', 'carer_observed',
                      'device', 'system', 'legacy'));

CREATE TABLE care_medication_event_corrections (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL
                            DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                            REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id        UUID NOT NULL
                            DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    event_id            UUID NOT NULL REFERENCES care_medication_events(id) ON DELETE CASCADE,
    person_id           UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    previous_status     TEXT NOT NULL,
    previous_source     TEXT NOT NULL,
    previous_confirmed_via TEXT NOT NULL,
    new_status          TEXT NOT NULL CHECK (new_status IN ('due', 'taken', 'missed', 'skipped', 'unknown')),
    new_source          TEXT NOT NULL CHECK (new_source IN ('self_report', 'family_report', 'carer_report',
                                                            'carer_observed', 'device', 'system', 'legacy')),
    reason              TEXT NOT NULL CHECK (length(btrim(reason)) BETWEEN 3 AND 400),
    corrected_by        UUID REFERENCES users(id) ON DELETE SET NULL,
    corrected_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_medication_event_corrections_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_med_corrections_event ON care_medication_event_corrections (event_id, corrected_at);
CREATE INDEX idx_care_med_corrections_person ON care_medication_event_corrections
    (tenant_id, workspace_id, person_id, corrected_at DESC);

ALTER TABLE care_medication_event_corrections ENABLE ROW LEVEL SECURITY;
ALTER TABLE care_medication_event_corrections FORCE ROW LEVEL SECURITY;
CREATE POLICY care_medication_event_corrections_select ON care_medication_event_corrections FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_medication_event_corrections_insert ON care_medication_event_corrections FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
-- Append-only to the application: no UPDATE or DELETE policy, and no grant for
-- them. Rows leave only with their event (ON DELETE CASCADE).
REVOKE ALL ON care_medication_event_corrections FROM PUBLIC;
REVOKE ALL ON care_medication_event_corrections FROM ira_app, ira_worker;
GRANT SELECT, INSERT ON care_medication_event_corrections TO ira_app, ira_worker;

COMMIT;
