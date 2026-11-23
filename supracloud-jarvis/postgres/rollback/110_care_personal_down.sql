-- Rollback of 110_care_personal.sql. Refuses while any reminder has been confirmed or
-- any memory edited by the person: those are the person's own record.
BEGIN;
DO $$
DECLARE n BIGINT;
BEGIN
    SELECT count(*) INTO n FROM care_reminders WHERE status = 'confirmed';
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % confirmed reminder row(s).', n; END IF;
    SELECT count(*) INTO n FROM care_conversation_memory WHERE edited_at IS NOT NULL;
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % memory row(s) edited by the person.', n; END IF;
END $$;
ALTER TABLE care_safety_settings DROP COLUMN IF EXISTS hydration_every_minutes;
ALTER TABLE care_conversation_memory DROP COLUMN IF EXISTS edited_at, DROP COLUMN IF EXISTS edited_by;
DELETE FROM care_consents WHERE kind = 'topics';
ALTER TABLE care_consents DROP CONSTRAINT care_consents_kind_check;
ALTER TABLE care_consents ADD CONSTRAINT care_consents_kind_check
    CHECK (kind IN ('transcripts_family', 'transcripts_carer', 'transcripts_organisation',
                    'health_data_family', 'data_retention'));
UPDATE care_checkins SET question_kind = 'custom' WHERE question_kind IN ('reminder', 'hydration');
ALTER TABLE care_checkins DROP CONSTRAINT care_checkins_question_kind_check;
ALTER TABLE care_checkins ADD CONSTRAINT care_checkins_question_kind_check
    CHECK (question_kind IN ('meal', 'medication', 'mood', 'pain', 'plans', 'wellbeing', 'custom', 'safety', 'morning'));
ALTER TABLE care_checkins DROP COLUMN IF EXISTS reminder_id;
DROP TABLE IF EXISTS care_reminders;
DROP TABLE IF EXISTS care_communication_profiles;
COMMIT;
