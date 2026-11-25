-- Rollback of 116_care_plans.sql. Refuses while any care plan exists, or any record of one
-- having been read: a care plan and who looked at it are part of the record of care.
BEGIN;
DO $$
DECLARE n BIGINT;
BEGIN
    SELECT count(*) INTO n FROM care_plans;
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % care plan version(s) exist.', n; END IF;
    SELECT count(*) INTO n FROM care_access_log WHERE resource_kind = 'care_plan';
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % access log row(s) record a care plan being read.', n; END IF;
    SELECT count(*) INTO n FROM care_access_log WHERE action = 'change';
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % access log row(s) record a change.', n; END IF;
END $$;
DROP TABLE IF EXISTS care_plans;
DROP FUNCTION IF EXISTS care_plans_insert_only();
ALTER TABLE care_access_log DROP CONSTRAINT care_access_log_resource_kind_check;
ALTER TABLE care_access_log ADD CONSTRAINT care_access_log_resource_kind_check
    CHECK (resource_kind IN ('vitals', 'transcript', 'checkins', 'alerts', 'medication', 'report',
                             'person', 'consent', 'access_notes'));
ALTER TABLE care_access_log DROP CONSTRAINT care_access_log_action_check;
ALTER TABLE care_access_log ADD CONSTRAINT care_access_log_action_check
    CHECK (action IN ('view', 'export', 'search'));
COMMIT;
