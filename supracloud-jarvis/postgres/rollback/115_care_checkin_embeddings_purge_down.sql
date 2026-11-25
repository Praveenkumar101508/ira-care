-- Rollback of 115_care_checkin_embeddings_purge.sql.
--
-- Nothing to undo in the schema, and the purged notes are not restored: they were
-- health summaries that no deletion promise could reach, and the code that wrote
-- them is gone. The check-ins themselves were never touched.
SELECT 1;
