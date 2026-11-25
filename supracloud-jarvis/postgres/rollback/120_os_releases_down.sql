-- Rollback of 120_os_releases.sql. Refuses while any release is catalogued: devices may
-- have been offered one, and the record of what was published is part of the fleet's history.
BEGIN;
DO $$
DECLARE n BIGINT;
BEGIN
    SELECT count(*) INTO n FROM os_releases;
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % OS release(s) are catalogued.', n; END IF;
END $$;
DROP TABLE IF EXISTS os_releases;
COMMIT;
