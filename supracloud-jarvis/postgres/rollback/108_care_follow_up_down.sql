-- Rollback of 108_care_follow_up.sql. Refuses while any alert records an outcome:
-- that is the record of what happened to a person.
BEGIN;
DO $$
DECLARE n BIGINT;
BEGIN
    SELECT count(*) INTO n FROM care_alerts WHERE outcome <> '';
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % alert row(s) carry an outcome.', n; END IF;
END $$;
DROP INDEX IF EXISTS idx_care_alerts_follow_up;
ALTER TABLE care_alerts
    DROP COLUMN IF EXISTS follow_up_due_at,
    DROP COLUMN IF EXISTS follow_up_asked_at,
    DROP COLUMN IF EXISTS re_escalations,
    DROP COLUMN IF EXISTS outcome;
ALTER TABLE care_safety_settings
    DROP COLUMN IF EXISTS follow_up_minutes,
    DROP COLUMN IF EXISTS follow_up_grace_minutes;
COMMIT;
