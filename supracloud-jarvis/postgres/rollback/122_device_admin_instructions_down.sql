-- Rollback of 122_device_admin_instructions.sql. Refuses while any administrator grant or
-- instruction exists: the record of who could reach a watch's restrictions, and who did, is
-- part of the fleet's history.
BEGIN;
DO $$
DECLARE n BIGINT;
BEGIN
    SELECT (SELECT count(*) FROM deployment_administrators)
         + (SELECT count(*) FROM care_device_admin_instructions) INTO n;
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % deployment administrator grant(s) or device instruction(s) exist.', n; END IF;
END $$;
DROP TABLE IF EXISTS care_device_admin_events;
DROP TABLE IF EXISTS care_device_admin_instructions;
DROP FUNCTION IF EXISTS care_device_admin_instructions_guard();
DROP TABLE IF EXISTS deployment_administrators;
COMMIT;
