-- =============================================================================
-- 101_care_continuous_monitoring.sql — IRA CARE continuous monitoring
--
-- Readings are now checked as they arrive, day and night, and a rule that
-- fires ASKS FIRST ("Are you OK?") before it escalates. This migration adds
-- the per-person settings that drive it, the alert state for "asking", and
-- the check-in kinds for a safety question and the morning check.
--
--   care_safety_settings
--     ask_first_wait_s               how long "Are you OK?" waits before an SOS
--     heart_rate_confirm_readings    readings in a row outside the range (2)
--     heart_rate_confirm_seconds     over at least this long (120): one spike is ignored
--     night_off_wrist_minutes        watch off at night this long -> a notice, never an SOS
--     usual_wake_time                the morning check runs if nothing has moved by
--     wake_grace_minutes             this long after it
--   care_alerts.status  + 'asking'   the question is out; next_action_at is the deadline
--   care_alerts.kind    + 'off_wrist_night'
--   care_checkins.question_kind + 'safety', 'morning'
--
-- No new table, so no new RLS. Rollback: rollback/101_care_continuous_monitoring_down.sql
-- =============================================================================

BEGIN;

ALTER TABLE care_safety_settings
    ADD COLUMN IF NOT EXISTS ask_first_wait_s INTEGER NOT NULL DEFAULT 60
        CHECK (ask_first_wait_s BETWEEN 15 AND 600),
    ADD COLUMN IF NOT EXISTS heart_rate_confirm_readings INTEGER NOT NULL DEFAULT 2
        CHECK (heart_rate_confirm_readings BETWEEN 1 AND 10),
    ADD COLUMN IF NOT EXISTS heart_rate_confirm_seconds INTEGER NOT NULL DEFAULT 120
        CHECK (heart_rate_confirm_seconds BETWEEN 0 AND 3600),
    ADD COLUMN IF NOT EXISTS night_off_wrist_minutes INTEGER NOT NULL DEFAULT 60
        CHECK (night_off_wrist_minutes BETWEEN 5 AND 720),
    ADD COLUMN IF NOT EXISTS usual_wake_time TIME NOT NULL DEFAULT '07:30',
    ADD COLUMN IF NOT EXISTS wake_grace_minutes INTEGER NOT NULL DEFAULT 60
        CHECK (wake_grace_minutes BETWEEN 10 AND 240);

ALTER TABLE care_alerts DROP CONSTRAINT care_alerts_status_check;
ALTER TABLE care_alerts ADD CONSTRAINT care_alerts_status_check
    CHECK (status IN ('asking', 'open', 'escalating', 'acknowledged', 'resolved', 'cancelled'));

ALTER TABLE care_alerts DROP CONSTRAINT care_alerts_kind_check;
ALTER TABLE care_alerts ADD CONSTRAINT care_alerts_kind_check
    CHECK (kind IN (
        'help_requested', 'no_answer_no_movement', 'heart_rate_outside_range',
        'off_wrist_during_check', 'missed_checkins', 'off_wrist', 'off_wrist_night', 'low_battery',
        'sensor_permission_off', 'no_sync', 'manual'));

ALTER TABLE care_checkins DROP CONSTRAINT care_checkins_question_kind_check;
ALTER TABLE care_checkins ADD CONSTRAINT care_checkins_question_kind_check
    CHECK (question_kind IN ('meal', 'medication', 'mood', 'pain', 'plans', 'wellbeing', 'custom',
                             'safety', 'morning'));

-- The open-alerts index must also cover alerts that are asking.
DROP INDEX IF EXISTS idx_care_alerts_open;
CREATE INDEX idx_care_alerts_open ON care_alerts (tenant_id, workspace_id, next_action_at)
    WHERE status IN ('asking', 'open', 'escalating');

COMMIT;
