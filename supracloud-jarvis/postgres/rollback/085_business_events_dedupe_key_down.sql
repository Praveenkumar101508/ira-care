-- =============================================================================
-- 085_business_events_dedupe_key_down.sql — reverse of 085
--
-- Drops the duplicate protection added by P14-R3 (FINDING AQ).
--
-- Loses the CONSTRAINT, never a record: every row survives, and the only
-- consequence is that a provider redelivering a webhook can once again create a
-- second row for one event and alert the owner twice.
--
-- The index is dropped before the column so this reads in the reverse order of
-- the migration; dropping the column alone would take the index with it, but an
-- explicit statement is the point of a rollback file.
-- =============================================================================

BEGIN;

DROP INDEX IF EXISTS uq_business_events_dedupe;

ALTER TABLE business_events
    DROP COLUMN IF EXISTS dedupe_key;

COMMIT;
