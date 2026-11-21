-- =============================================================================
-- 100_care_reports.sql — IRA CARE reports (phase 8)
--
-- The daily carer report and the weekly family report, generated from the
-- care tables and kept so they can be read back in the dashboard and the
-- family app, and so a report is generated and delivered once per person
-- and period (the unique key is what makes the delivery job idempotent).
--
-- Same boundary as the other care tables (099): tenant AND workspace,
-- forced row-level security, belongs to the person cared for, removed with
-- the person (ON DELETE CASCADE) and with the tenant. A report holds
-- summaries only: never the person's own words, whatever the consents say,
-- because a report travels (push, e-mail) in a way a screen does not.
--
-- Rollback: rollback/100_care_reports_down.sql
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.care_people') IS NULL THEN
        RAISE EXCEPTION '100 REFUSING: care_people does not exist. Run 099 first.';
    END IF;
    IF to_regclass('public.care_reports') IS NOT NULL THEN
        RAISE EXCEPTION '100 REFUSING: care_reports already exists.';
    END IF;
END $$;

CREATE TABLE care_reports (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL
                            DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                            REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id        UUID NOT NULL
                            DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    person_id           UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL CHECK (kind IN ('daily_carer', 'weekly_family')),
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    title               TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    body_text           TEXT NOT NULL CHECK (length(body_text) BETWEEN 1 AND 20000),
    body                JSONB NOT NULL DEFAULT '{}'::jsonb,
    delivered_channels  TEXT[] NOT NULL DEFAULT '{}',
    CONSTRAINT care_reports_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT care_reports_period CHECK (period_end >= period_start),
    CONSTRAINT care_reports_once_per_period UNIQUE (person_id, kind, period_start)
);
CREATE INDEX idx_care_reports_person ON care_reports (tenant_id, workspace_id, person_id, kind, period_start DESC);

ALTER TABLE care_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE care_reports FORCE ROW LEVEL SECURITY;
CREATE POLICY care_reports_select ON care_reports FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_reports_insert ON care_reports FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_reports_update ON care_reports FOR UPDATE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_reports_delete ON care_reports FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
           AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);

REVOKE ALL ON care_reports FROM PUBLIC;
REVOKE ALL ON care_reports FROM ira_app, ira_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON care_reports TO ira_app, ira_worker;

COMMIT;
