-- Rollback of 102_care_home_and_location.sql. Refuses while any access-log row
-- records a view of access notes, because the old constraint would then fail.
BEGIN;
DO $$
DECLARE n BIGINT;
BEGIN
    SELECT count(*) INTO n FROM care_access_log WHERE resource_kind = 'access_notes';
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % access-log row(s) record access-note views.', n; END IF;
END $$;
ALTER TABLE care_people
    DROP CONSTRAINT IF EXISTS care_people_home_coordinates_pair,
    DROP CONSTRAINT IF EXISTS care_people_access_notes_whole,
    DROP COLUMN IF EXISTS home_address,
    DROP COLUMN IF EXISTS home_latitude,
    DROP COLUMN IF EXISTS home_longitude,
    DROP COLUMN IF EXISTS home_radius_m,
    DROP COLUMN IF EXISTS emergency_summary,
    DROP COLUMN IF EXISTS access_notes_ciphertext,
    DROP COLUMN IF EXISTS access_notes_nonce,
    DROP COLUMN IF EXISTS access_notes_wrapped_dek,
    DROP COLUMN IF EXISTS access_notes_wrap_nonce,
    DROP COLUMN IF EXISTS access_notes_kek_id,
    DROP COLUMN IF EXISTS access_notes_key_version;
ALTER TABLE care_alerts
    DROP CONSTRAINT IF EXISTS care_alerts_location_pair,
    DROP COLUMN IF EXISTS location_latitude,
    DROP COLUMN IF EXISTS location_longitude,
    DROP COLUMN IF EXISTS location_accuracy_m,
    DROP COLUMN IF EXISTS location_at,
    DROP COLUMN IF EXISTS at_home;
ALTER TABLE care_access_log DROP CONSTRAINT care_access_log_resource_kind_check;
ALTER TABLE care_access_log ADD CONSTRAINT care_access_log_resource_kind_check
    CHECK (resource_kind IN ('vitals', 'transcript', 'checkins', 'alerts', 'medication', 'report', 'person', 'consent'));
COMMIT;
