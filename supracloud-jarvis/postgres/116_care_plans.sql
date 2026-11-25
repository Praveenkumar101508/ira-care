-- =============================================================================
-- 116_care_plans.sql — IRA CARE: the care plan, versioned
--
--   care_plans
--     What a person's care is for and how it is given: goals, the support they
--     need, the daily routine (what, when, and who does it: a carer, family or
--     the person), the risks to watch for, what to do in an emergency, and when
--     the plan is next reviewed. Each change is a NEW row with the next version
--     number and a reason; nothing is ever rewritten, so the history of the plan
--     is the table. Insert-only for the runtime roles (no UPDATE or DELETE grant)
--     and UPDATE refused for everyone by trigger (except the author link being cleared
--     when that account is deleted); rows go only with the person
--     (ON DELETE CASCADE from care_people: the per-person erasure) or the tenant.
--   care_access_log.resource_kind + 'care_plan'   reading a plan is written down
--   care_access_log.action + 'change'             and so is saving a new version
--
-- Rollback: rollback/116_care_plans_down.sql (refuses while any plan exists)
-- =============================================================================

BEGIN;

CREATE TABLE care_plans (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    person_id       UUID NOT NULL REFERENCES care_people(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL CHECK (version >= 1),
    goals           TEXT NOT NULL DEFAULT '' CHECK (length(goals) <= 2000),
    support         TEXT NOT NULL DEFAULT '' CHECK (length(support) <= 4000),
    routine         JSONB NOT NULL DEFAULT '[]'::jsonb
                        CHECK (jsonb_typeof(routine) = 'array' AND jsonb_array_length(routine) <= 40),
    risks           TEXT NOT NULL DEFAULT '' CHECK (length(risks) <= 4000),
    emergency       TEXT NOT NULL DEFAULT '' CHECK (length(emergency) <= 2000),
    review_on       DATE,
    reason          TEXT NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 500),
    author_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
    author_name     TEXT NOT NULL DEFAULT '' CHECK (length(author_name) <= 80),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_plans_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE,
    -- Two managers saving at once: the second gets a conflict, never a silent overwrite.
    CONSTRAINT care_plans_person_version UNIQUE (person_id, version)
);
CREATE INDEX idx_care_plans_person_version ON care_plans (tenant_id, workspace_id, person_id, version DESC);

CREATE OR REPLACE FUNCTION care_plans_insert_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    -- The one change allowed: the author's account was deleted (ON DELETE SET NULL), so
    -- account erasure works; the name they wrote under and every word of the plan stay.
    IF OLD.author_user_id IS NOT NULL AND NEW.author_user_id IS NULL
       AND (to_jsonb(NEW) - 'author_user_id') = (to_jsonb(OLD) - 'author_user_id') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'care_plans is insert-only: a change is a new version'
        USING ERRCODE = 'insufficient_privilege';
END $$;
REVOKE EXECUTE ON FUNCTION care_plans_insert_only() FROM PUBLIC;
CREATE TRIGGER care_plans_no_update
    BEFORE UPDATE ON care_plans FOR EACH ROW EXECUTE FUNCTION care_plans_insert_only();

ALTER TABLE care_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE care_plans FORCE ROW LEVEL SECURITY;
CREATE POLICY care_plans_select ON care_plans FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY care_plans_insert ON care_plans FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
REVOKE ALL ON care_plans FROM PUBLIC;
REVOKE ALL ON care_plans FROM ira_app, ira_worker;
GRANT SELECT, INSERT ON care_plans TO ira_app;

ALTER TABLE care_access_log DROP CONSTRAINT care_access_log_resource_kind_check;
ALTER TABLE care_access_log ADD CONSTRAINT care_access_log_resource_kind_check
    CHECK (resource_kind IN ('vitals', 'transcript', 'checkins', 'alerts', 'medication', 'report',
                             'person', 'consent', 'access_notes', 'care_plan'));
ALTER TABLE care_access_log DROP CONSTRAINT care_access_log_action_check;
ALTER TABLE care_access_log ADD CONSTRAINT care_access_log_action_check
    CHECK (action IN ('view', 'export', 'search', 'change'));

COMMIT;
