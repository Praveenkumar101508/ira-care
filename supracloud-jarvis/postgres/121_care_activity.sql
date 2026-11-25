-- =============================================================================
-- 121_care_activity.sql — IRA CARE: hourly activity estimates from the watch (F7, D-084)
--
--   care_activity_periods
--     One row per watch per hour: seconds per label from IRA Care Activity (the
--     model's own classes, 'unknown' and 'unavailable'), seconds per reason, how
--     many 10-second windows, the model's version and the SHA-256 of the exact
--     model file, and whether the input was the watch's own sensors or a
--     simulated-sensor build. Summaries only: no window, sample or probability.
--     Sent only under the person's 'activity_sharing' consent, checked again
--     here on arrival. Kept apart from care_vitals on purpose: no safety rule,
--     escalation or alert reads this table (D-084). Insert-only for the runtime
--     (a re-sent hour is ignored); removed by the retention purge (by
--     period_start), with the person (ON DELETE CASCADE), and with the tenant.
--
--   care_consents.kind  + 'activity_sharing' (the watch may send hourly
--                         activity estimates and the care circle may see them)
--
-- Rollback: rollback/121_care_activity_down.sql
-- =============================================================================

BEGIN;

CREATE TABLE care_activity_periods (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    person_id       UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    device_id       UUID REFERENCES care_devices(id) ON DELETE SET NULL,
    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    source          TEXT NOT NULL DEFAULT 'device' CHECK (source IN ('device', 'simulated')),
    model_version   TEXT NOT NULL CHECK (length(model_version) BETWEEN 1 AND 80),
    model_sha256    TEXT NOT NULL CHECK (model_sha256 ~ '^[0-9a-f]{64}$'),
    seconds         JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(seconds) = 'object'),
    reasons         JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(reasons) = 'object'),
    windows         INTEGER NOT NULL DEFAULT 0 CHECK (windows BETWEEN 0 AND 360),
    mean_confidence DOUBLE PRECISION CHECK (mean_confidence IS NULL OR mean_confidence BETWEEN 0 AND 1),
    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_activity_periods_span CHECK (
        period_end > period_start AND period_end - period_start <= interval '1 hour'),
    CONSTRAINT care_activity_periods_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_activity_periods_person_time
    ON care_activity_periods (tenant_id, workspace_id, person_id, period_start DESC);
CREATE UNIQUE INDEX idx_care_activity_periods_dedupe
    ON care_activity_periods (device_id, period_start) WHERE device_id IS NOT NULL;

ALTER TABLE care_activity_periods ENABLE ROW LEVEL SECURITY;
ALTER TABLE care_activity_periods FORCE ROW LEVEL SECURITY;
CREATE POLICY care_activity_periods_select ON care_activity_periods FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_activity_periods_insert ON care_activity_periods FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_activity_periods_delete ON care_activity_periods FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
REVOKE ALL ON care_activity_periods FROM PUBLIC;
REVOKE ALL ON care_activity_periods FROM ira_app, ira_worker;
-- No UPDATE: an hour, once stored, is what the watch said. DELETE for retention and erasure.
GRANT SELECT, INSERT, DELETE ON care_activity_periods TO ira_app, ira_worker;

ALTER TABLE care_consents DROP CONSTRAINT care_consents_kind_check;
ALTER TABLE care_consents ADD CONSTRAINT care_consents_kind_check
    CHECK (kind IN ('transcripts_family', 'transcripts_carer', 'transcripts_organisation',
                    'health_data_family', 'data_retention', 'topics', 'activity_sharing'));

COMMIT;
