-- =============================================================================
-- 108_care_follow_up.sql — IRA CARE: "I'm on my way" is a promise with a deadline
--
--   care_safety_settings
--     follow_up_minutes        how long after "I'm on my way" an arrival or an
--                              outcome is expected (30)
--     follow_up_grace_minutes  how long the responder gets to answer "have you
--                              arrived?" before the alert escalates again (10)
--   care_alerts
--     follow_up_due_at         when the responder is asked, then when the alert
--                              re-escalates; NULL once someone has arrived or the
--                              alert is resolved
--     follow_up_asked_at       when the responder was asked
--     re_escalations           how many times the chain was restarted because
--                              nobody arrived
--     outcome                  what happened, recorded when the alert is resolved;
--                              an alert stays open until it is
--
-- No new table, so no new RLS. Rollback: rollback/108_care_follow_up_down.sql
-- =============================================================================

BEGIN;

ALTER TABLE care_safety_settings
    ADD COLUMN IF NOT EXISTS follow_up_minutes INTEGER NOT NULL DEFAULT 30
        CHECK (follow_up_minutes BETWEEN 5 AND 180),
    ADD COLUMN IF NOT EXISTS follow_up_grace_minutes INTEGER NOT NULL DEFAULT 10
        CHECK (follow_up_grace_minutes BETWEEN 2 AND 60);

ALTER TABLE care_alerts
    ADD COLUMN IF NOT EXISTS follow_up_due_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS follow_up_asked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS re_escalations SMALLINT NOT NULL DEFAULT 0 CHECK (re_escalations BETWEEN 0 AND 20),
    ADD COLUMN IF NOT EXISTS outcome TEXT NOT NULL DEFAULT ''
        CHECK (outcome IN ('', 'all_well', 'needed_help', 'taken_to_hospital', 'false_alarm', 'other'));

CREATE INDEX IF NOT EXISTS idx_care_alerts_follow_up ON care_alerts (tenant_id, workspace_id, follow_up_due_at)
    WHERE status = 'acknowledged' AND follow_up_due_at IS NOT NULL;

COMMIT;
