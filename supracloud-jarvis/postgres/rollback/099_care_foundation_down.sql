-- Rollback of 099_care_foundation.sql: drops the fifteen care tables and the
-- append-only trigger function. Refuses while any care person exists, because
-- that would destroy a person's health history: delete the people first,
-- deliberately, through the care package.
BEGIN;

DO $$
DECLARE
    n BIGINT;
BEGIN
    IF to_regclass('public.care_people') IS NOT NULL THEN
        SELECT count(*) INTO n FROM care_people;
        IF n > 0 THEN
            RAISE EXCEPTION 'Refusing rollback: % care person row(s) exist. Remove them deliberately first.', n;
        END IF;
    END IF;
END $$;

DROP TABLE IF EXISTS public.care_access_log;
DROP TABLE IF EXISTS public.care_consents;
DROP TABLE IF EXISTS public.care_transcripts;
DROP TABLE IF EXISTS public.care_medication_events;
DROP TABLE IF EXISTS public.care_medications;
DROP TABLE IF EXISTS public.care_routines;
DROP TABLE IF EXISTS public.care_acknowledgements;
DROP TABLE IF EXISTS public.care_alert_attempts;
DROP TABLE IF EXISTS public.care_alerts;
DROP TABLE IF EXISTS public.care_checkins;
DROP TABLE IF EXISTS public.care_safety_settings;
DROP TABLE IF EXISTS public.care_vitals;
DROP TABLE IF EXISTS public.care_devices;
DROP TABLE IF EXISTS public.care_circle_members;
DROP TABLE IF EXISTS public.care_people;
DROP FUNCTION IF EXISTS public.care_access_log_append_only();

DELETE FROM schema_migrations WHERE name = '099_care_foundation.sql';

COMMIT;
