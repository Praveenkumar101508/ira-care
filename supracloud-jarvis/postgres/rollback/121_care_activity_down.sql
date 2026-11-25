-- Rollback of 121_care_activity.sql. Refuses while any activity hour is stored: dropping the
-- table would silently destroy what the watch sent under the person's consent.
-- The 'activity_sharing' consent records are removed: the kind no longer exists after rollback.
BEGIN;
DO $$
DECLARE n BIGINT;
BEGIN
    SELECT count(*) INTO n FROM care_activity_periods;
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % activity period row(s) exist.', n; END IF;
END $$;
DROP TABLE IF EXISTS care_activity_periods;
DELETE FROM care_consents WHERE kind = 'activity_sharing';
ALTER TABLE care_consents DROP CONSTRAINT care_consents_kind_check;
ALTER TABLE care_consents ADD CONSTRAINT care_consents_kind_check
    CHECK (kind IN ('transcripts_family', 'transcripts_carer', 'transcripts_organisation',
                    'health_data_family', 'data_retention', 'topics'));
COMMIT;
