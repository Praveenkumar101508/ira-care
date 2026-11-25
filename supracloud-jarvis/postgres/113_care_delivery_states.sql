-- =============================================================================
-- 113_care_delivery_states.sql — IRA CARE: an alert message is queued durably,
-- and its state is what the provider said, never more (safety remediation A2)
--
-- `care_alert_attempts` stays what 099 made it: a row the application may
-- write once and never change (`test_readings_and_attempts_cannot_be_rewritten`).
-- So the two things that DO change about a message live beside it:
--
--   care_alert_dispatch          the outbox: one row per message still to be
--                                sent, with its tries and its lease. The only
--                                updatable part, and it holds no content: who
--                                and what come from the attempt and the alert.
--   care_alert_attempt_events    every state the provider reported, as a new
--                                row: accepted, delivered, failed, no answer...
--                                Insert-only like the attempts. A message's
--                                state is its furthest event (`status_rank`), so
--                                a callback that comes twice, late, or before
--                                the send was recorded changes nothing it
--                                should not, and the whole history is kept.
--
--   care_alert_attempts.outcome gains 'queued' (written in the same
--   transaction that moved the alert on; the dispatcher sends it after) and,
--   for the events, accepted / undelivered / no_answer / busy / unknown.
--   'sent' stays valid for rows written before 113: it meant "accepted".
--   care_alert_attempts.wording_tier: whose words to use (tier 3 adds
--   "call 999 now"), fixed when the message is queued.
--
-- Additive: one constraint widened, one nullable column, two new tables.
-- Rollback: rollback/113_care_delivery_states_down.sql
-- =============================================================================

BEGIN;

ALTER TABLE care_alert_attempts DROP CONSTRAINT care_alert_attempts_outcome_check;
ALTER TABLE care_alert_attempts ADD CONSTRAINT care_alert_attempts_outcome_check
    CHECK (outcome IN ('logged', 'sent', 'delivered', 'failed', 'skipped',
                       'queued', 'accepted', 'undelivered', 'no_answer', 'busy', 'unknown'));
ALTER TABLE care_alert_attempts
    ADD COLUMN IF NOT EXISTS wording_tier SMALLINT CHECK (wording_tier BETWEEN 1 AND 3);

CREATE TABLE care_alert_dispatch (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    attempt_id      UUID NOT NULL UNIQUE REFERENCES care_alert_attempts(id) ON DELETE CASCADE,
    tries           SMALLINT NOT NULL DEFAULT 0 CHECK (tries BETWEEN 0 AND 20),
    next_try_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_alert_dispatch_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_care_alert_dispatch_due ON care_alert_dispatch (tenant_id, workspace_id, next_try_at)
    WHERE finished_at IS NULL;

CREATE TABLE care_alert_attempt_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    attempt_id      UUID NOT NULL REFERENCES care_alert_attempts(id) ON DELETE CASCADE,
    outcome         TEXT NOT NULL CHECK (outcome IN ('logged', 'delivered', 'failed', 'skipped', 'queued',
                                                     'accepted', 'undelivered', 'no_answer', 'busy', 'unknown')),
    status_rank     SMALLINT NOT NULL CHECK (status_rank BETWEEN 0 AND 100),
    provider_status TEXT NOT NULL DEFAULT '' CHECK (length(provider_status) <= 40),
    provider_ref    TEXT NOT NULL DEFAULT '' CHECK (length(provider_ref) <= 200),
    detail          TEXT NOT NULL DEFAULT '' CHECK (length(detail) <= 1000),
    source          TEXT NOT NULL CHECK (source IN ('dispatcher', 'callback', 'receipt')),
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT care_alert_attempt_events_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE,
    -- The same report twice is one row: a repeated callback is a no-op.
    CONSTRAINT care_alert_attempt_events_once UNIQUE (attempt_id, status_rank, provider_status)
);
CREATE INDEX idx_care_alert_attempt_events_attempt ON care_alert_attempt_events (attempt_id, status_rank DESC);

DO $$
DECLARE
    t TEXT;
    pred TEXT := 'tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid '
                 'AND workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid';
BEGIN
    FOREACH t IN ARRAY ARRAY['care_alert_dispatch', 'care_alert_attempt_events'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY %I ON %I FOR SELECT USING (%s)', t || '_select', t, pred);
        EXECUTE format('CREATE POLICY %I ON %I FOR INSERT WITH CHECK (%s)', t || '_insert', t, pred);
        EXECUTE format('REVOKE ALL ON %I FROM PUBLIC', t);
        EXECUTE format('REVOKE ALL ON %I FROM ira_app, ira_worker', t);
        EXECUTE format('GRANT SELECT, INSERT ON %I TO ira_app, ira_worker', t);
    END LOOP;
    -- The outbox alone may change: tries, lease, finished. Nothing else is in it.
    EXECUTE format('CREATE POLICY care_alert_dispatch_update ON care_alert_dispatch FOR UPDATE USING (%s) WITH CHECK (%s)',
                   pred, pred);
    GRANT UPDATE (tries, next_try_at, finished_at) ON care_alert_dispatch TO ira_app, ira_worker;
END $$;

COMMIT;
