-- Rollback of 106_care_sos_state.sql. Refuses while any acknowledgement says 'arrived'.
BEGIN;
DO $$
DECLARE n BIGINT;
BEGIN
    SELECT count(*) INTO n FROM care_acknowledgements WHERE kind = 'arrived';
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % arrival acknowledgement row(s) exist.', n; END IF;
END $$;
DROP TABLE IF EXISTS care_device_events;
ALTER TABLE care_acknowledgements DROP CONSTRAINT care_acknowledgements_kind_check;
ALTER TABLE care_acknowledgements ADD CONSTRAINT care_acknowledgements_kind_check
    CHECK (kind IN ('on_my_way', 'checked_in_person', 'spoke_to_person', 'false_alarm', 'handed_over'));
ALTER TABLE care_safety_settings DROP COLUMN IF EXISTS reading_upload_interval_s;
COMMIT;
