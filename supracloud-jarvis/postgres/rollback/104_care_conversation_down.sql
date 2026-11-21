-- Rollback of 104_care_conversation.sql. Refuses while any alert is a concerning phrase.
BEGIN;
DO $$
DECLARE n BIGINT;
BEGIN
    SELECT count(*) INTO n FROM care_alerts WHERE kind = 'concerning_phrase';
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % concerning-phrase alert row(s) exist.', n; END IF;
END $$;
DROP TABLE IF EXISTS care_conversation_memory;
ALTER TABLE care_checkins DROP COLUMN IF EXISTS question_source;
ALTER TABLE care_alerts DROP CONSTRAINT care_alerts_kind_check;
ALTER TABLE care_alerts ADD CONSTRAINT care_alerts_kind_check
    CHECK (kind IN ('help_requested', 'no_answer_no_movement', 'heart_rate_outside_range',
        'off_wrist_during_check', 'missed_checkins', 'off_wrist', 'low_battery',
        'sensor_permission_off', 'no_sync', 'manual', 'off_wrist_night', 'suspected_fall'));
COMMIT;
