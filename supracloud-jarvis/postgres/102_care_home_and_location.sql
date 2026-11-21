-- =============================================================================
-- 102_care_home_and_location.sql — IRA CARE: responders know where to go
--
--   care_people
--     home_address          the address responders are sent to (free text)
--     home_latitude/longitude, home_radius_m
--                           entered by the carer; "at home" means within the radius
--     emergency_summary     free text the carer controls; no fields of our own
--     access_notes_*        key safe, which door, pets: SEALED under a per-row data key
--                           (AES-256-GCM), the data key wrapped under the configured
--                           key-encryption key and bound to the person's row, exactly as
--                           voice templates are (062). Shown only to tier 1 and 2
--                           recipients while an alert is open; every view is written to
--                           care_access_log
--   care_alerts
--     location_*            where the watch was when the SOS or suspected fall STARTED.
--                           Sent once, never continuously; lives and dies with the alert.
--     at_home               whether that location is within the home radius (NULL = unknown)
--   care_access_log.resource_kind + 'access_notes'
--
-- No new table, so no new RLS. Rollback: rollback/102_care_home_and_location_down.sql
-- =============================================================================

BEGIN;

ALTER TABLE care_people
    ADD COLUMN IF NOT EXISTS home_address TEXT NOT NULL DEFAULT '' CHECK (length(home_address) <= 400),
    ADD COLUMN IF NOT EXISTS home_latitude DOUBLE PRECISION
        CHECK (home_latitude IS NULL OR home_latitude BETWEEN -90 AND 90),
    ADD COLUMN IF NOT EXISTS home_longitude DOUBLE PRECISION
        CHECK (home_longitude IS NULL OR home_longitude BETWEEN -180 AND 180),
    ADD COLUMN IF NOT EXISTS home_radius_m INTEGER NOT NULL DEFAULT 150
        CHECK (home_radius_m BETWEEN 20 AND 5000),
    ADD COLUMN IF NOT EXISTS emergency_summary TEXT NOT NULL DEFAULT '' CHECK (length(emergency_summary) <= 2000),
    ADD COLUMN IF NOT EXISTS access_notes_ciphertext BYTEA,
    ADD COLUMN IF NOT EXISTS access_notes_nonce BYTEA,
    ADD COLUMN IF NOT EXISTS access_notes_wrapped_dek BYTEA,
    ADD COLUMN IF NOT EXISTS access_notes_wrap_nonce BYTEA,
    ADD COLUMN IF NOT EXISTS access_notes_kek_id TEXT CHECK (access_notes_kek_id IS NULL OR length(access_notes_kek_id) <= 80),
    ADD COLUMN IF NOT EXISTS access_notes_key_version INTEGER CHECK (access_notes_key_version IS NULL OR access_notes_key_version >= 1),
    ADD CONSTRAINT care_people_home_coordinates_pair
        CHECK ((home_latitude IS NULL) = (home_longitude IS NULL)),
    -- All six or none: a row with ciphertext and no key is text nobody can read.
    ADD CONSTRAINT care_people_access_notes_whole
        CHECK (num_nulls(access_notes_ciphertext, access_notes_nonce, access_notes_wrapped_dek,
                         access_notes_wrap_nonce, access_notes_kek_id, access_notes_key_version) IN (0, 6));

ALTER TABLE care_alerts
    ADD COLUMN IF NOT EXISTS location_latitude DOUBLE PRECISION
        CHECK (location_latitude IS NULL OR location_latitude BETWEEN -90 AND 90),
    ADD COLUMN IF NOT EXISTS location_longitude DOUBLE PRECISION
        CHECK (location_longitude IS NULL OR location_longitude BETWEEN -180 AND 180),
    ADD COLUMN IF NOT EXISTS location_accuracy_m DOUBLE PRECISION
        CHECK (location_accuracy_m IS NULL OR location_accuracy_m >= 0),
    ADD COLUMN IF NOT EXISTS location_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS at_home BOOLEAN,
    ADD CONSTRAINT care_alerts_location_pair
        CHECK ((location_latitude IS NULL) = (location_longitude IS NULL));

ALTER TABLE care_access_log DROP CONSTRAINT care_access_log_resource_kind_check;
ALTER TABLE care_access_log ADD CONSTRAINT care_access_log_resource_kind_check
    CHECK (resource_kind IN ('vitals', 'transcript', 'checkins', 'alerts', 'medication', 'report',
                             'person', 'consent', 'access_notes'));

COMMIT;
