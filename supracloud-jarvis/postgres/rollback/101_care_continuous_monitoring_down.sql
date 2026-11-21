-- Rollback of 101_care_continuous_monitoring.sql. Refuses while any alert is
-- 'asking' or any check-in uses the new kinds, because the old constraints
-- would then fail to apply.
BEGIN;
DO $$
DECLARE n BIGINT;
BEGIN
    SELECT count(*) INTO n FROM care_alerts WHERE status = 'asking' OR kind = 'off_wrist_night';
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % alert row(s) use the 101 states.', n; END IF;
    SELECT count(*) INTO n FROM care_checkins WHERE question_kind IN ('safety', 'morning');
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % check-in row(s) use the 101 kinds.', n; END IF;
END $$;
ALTER TABLE care_safety_settings
    DROP COLUMN IF EXISTS ask_first_wait_s,
    DROP COLUMN IF EXISTS heart_rate_confirm_readings,
    DROP COLUMN IF EXISTS heart_rate_confirm_seconds,
    DROP COLUMN IF EXISTS night_off_wrist_minutes,
    DROP COLUMN IF EXISTS usual_wake_time,
    DROP COLUMN IF EXISTS wake_grace_minutes;
ALTER TABLE care_alerts DROP CONSTRAINT care_alerts_status_check;
ALTER TABLE care_alerts ADD CONSTRAINT care_alerts_status_check
    CHECK (status IN ('open', 'escalating', 'acknowledged', 'resolved', 'cancelled'));
ALTER TABLE care_alerts DROP CONSTRAINT care_alerts_kind_check;
ALTER TABLE care_alerts ADD CONSTRAINT care_alerts_kind_check
    CHECK (kind IN ('help_requested', 'no_answer_no_movement', 'heart_rate_outside_range',
        'off_wrist_during_check', 'missed_checkins', 'off_wrist', 'low_battery',
        'sensor_permission_off', 'no_sync', 'manual'));
ALTER TABLE care_checkins DROP CONSTRAINT care_checkins_question_kind_check;
ALTER TABLE care_checkins ADD CONSTRAINT care_checkins_question_kind_check
    CHECK (question_kind IN ('meal', 'medication', 'mood', 'pain', 'plans', 'wellbeing', 'custom'));
DROP INDEX IF EXISTS idx_care_alerts_open;
CREATE INDEX idx_care_alerts_open ON care_alerts (tenant_id, workspace_id, next_action_at)
    WHERE status IN ('open', 'escalating');
DELETE FROM schema_migrations WHERE name = '101_care_continuous_monitoring.sql';
COMMIT;
