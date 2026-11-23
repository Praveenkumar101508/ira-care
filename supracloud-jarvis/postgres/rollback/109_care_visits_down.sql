-- Rollback of 109_care_visits.sql. Refuses while any visit records an arrival or an outcome:
-- that is the record of care given.
BEGIN;
DO $$
DECLARE n BIGINT;
BEGIN
    SELECT count(*) INTO n FROM care_visits WHERE arrived_at IS NOT NULL OR outcome <> '';
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % visit row(s) record an arrival or an outcome.', n; END IF;
    SELECT count(*) INTO n FROM care_alerts WHERE kind IN ('visit_late', 'visit_missed');
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % visit alert row(s) exist.', n; END IF;
END $$;
DROP TABLE IF EXISTS care_visit_tasks;
DROP TABLE IF EXISTS care_visits;
ALTER TABLE care_safety_settings DROP COLUMN IF EXISTS visit_late_minutes;
ALTER TABLE care_alerts DROP CONSTRAINT care_alerts_kind_check;
ALTER TABLE care_alerts ADD CONSTRAINT care_alerts_kind_check
    CHECK (kind IN ('help_requested', 'no_answer_no_movement', 'heart_rate_outside_range',
        'off_wrist_during_check', 'missed_checkins', 'off_wrist', 'low_battery',
        'sensor_permission_off', 'no_sync', 'manual', 'off_wrist_night', 'suspected_fall',
        'concerning_phrase'));
COMMIT;
