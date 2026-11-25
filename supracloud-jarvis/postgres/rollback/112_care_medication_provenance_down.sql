-- Rollback of 112_care_medication_provenance.sql.
--
-- What is lost: the correction history, who recorded each dose (source,
-- recorded_by) and the check-in's binding to one medicine and dose. The doses
-- themselves (care_medication_events.status, confirmed_via) are untouched, and
-- the application from before 112 reads them unchanged. Export
-- care_medication_event_corrections first if the history must be kept.
BEGIN;
DROP TABLE IF EXISTS care_medication_event_corrections;
ALTER TABLE care_medication_events DROP CONSTRAINT IF EXISTS care_medication_events_source_check;
ALTER TABLE care_medication_events DROP COLUMN IF EXISTS recorded_by;
ALTER TABLE care_medication_events DROP COLUMN IF EXISTS source;
ALTER TABLE care_checkins DROP CONSTRAINT IF EXISTS care_checkins_dose_binding_whole;
ALTER TABLE care_checkins DROP COLUMN IF EXISTS dose_due_at;
ALTER TABLE care_checkins DROP COLUMN IF EXISTS medication_id;
COMMIT;
