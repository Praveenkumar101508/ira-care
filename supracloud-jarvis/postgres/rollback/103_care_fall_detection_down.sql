-- Rollback of 103_care_fall_detection.sql. Refuses while any alert is a suspected fall.
BEGIN;
DO $$
DECLARE n BIGINT;
BEGIN
    SELECT count(*) INTO n FROM care_alerts WHERE kind = 'suspected_fall';
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % suspected-fall alert row(s) exist.', n; END IF;
END $$;
ALTER TABLE care_safety_settings
    DROP COLUMN IF EXISTS fall_detection_enabled,
    DROP COLUMN IF EXISTS fall_countdown_s,
    DROP COLUMN IF EXISTS fall_impact_g,
    DROP COLUMN IF EXISTS fall_still_seconds,
    DROP COLUMN IF EXISTS fall_still_threshold_g;
ALTER TABLE care_alerts DROP CONSTRAINT care_alerts_kind_check;
ALTER TABLE care_alerts ADD CONSTRAINT care_alerts_kind_check
    CHECK (kind IN ('help_requested', 'no_answer_no_movement', 'heart_rate_outside_range',
        'off_wrist_during_check', 'missed_checkins', 'off_wrist', 'low_battery',
        'sensor_permission_off', 'no_sync', 'manual', 'off_wrist_night'));
COMMIT;
