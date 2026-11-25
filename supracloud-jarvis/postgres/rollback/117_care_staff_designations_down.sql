-- Rollback of 117_care_staff_designations.sql. Refuses while any designation exists: dropping
-- the table would silently take coordinators' access away.
BEGIN;
DO $$
DECLARE n BIGINT;
BEGIN
    SELECT count(*) INTO n FROM care_staff_designations;
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % staff designation(s) exist.', n; END IF;
END $$;
DROP TABLE IF EXISTS care_staff_designations;
COMMIT;
