-- Rollback of 114_care_watch_unpaired.sql.
--
-- Nothing is lost: an alert of the new kind is kept as 'manual', its details
-- unchanged (they still say the watch was unpaired, and by whom).
BEGIN;
UPDATE care_alerts SET kind = 'manual' WHERE kind = 'watch_unpaired';
ALTER TABLE care_alerts DROP CONSTRAINT care_alerts_kind_check;
ALTER TABLE care_alerts ADD CONSTRAINT care_alerts_kind_check
    CHECK (kind IN ('help_requested', 'no_answer_no_movement', 'heart_rate_outside_range',
        'off_wrist_during_check', 'missed_checkins', 'off_wrist', 'low_battery',
        'sensor_permission_off', 'no_sync', 'manual', 'off_wrist_night', 'suspected_fall',
        'concerning_phrase', 'visit_late', 'visit_missed'));
COMMIT;
