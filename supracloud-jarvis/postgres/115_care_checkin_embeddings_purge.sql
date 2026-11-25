-- =============================================================================
-- 115_care_checkin_embeddings_purge.sql — IRA CARE: no check-in summaries in
-- the general memory store (remediation F6)
--
-- Until this change, every answered check-in also wrote a note ("Check-in
-- (wellbeing): Said they had not eaten yet.") into `memory_embeddings`, the
-- assistant's general memory, under the workspace's vault namespace. Nothing in
-- IRA CARE reads those notes back, and they carry no person, so they could not
-- be reached by "forget this day", by erasing the person, or by the care
-- retention job: health summaries kept outside every deletion promise.
--
-- The write is removed in the same change (care/checkin_cycle.py). This purges
-- the notes already written. The check-in itself, and IRA's day summary in
-- care_conversation_memory, are untouched: those are the person's and can be
-- corrected and deleted.
--
-- ── RLS window ─────────────────────────────────────────────────────────────
--
-- Migrations run as ira_schema_owner, and FORCE RLS applies to the owner: with
-- no app.tenant_id bound, the DELETE would match no rows and report success.
-- Forcing is lifted for this transaction only and restored at the end, as in
-- 049, 093 and 094; a failure anywhere rolls the window shut with the rest.
--
-- Idempotent: a second run finds nothing to delete.
--
-- Deletes rows. Rollback: rollback/115_care_checkin_embeddings_purge_down.sql
-- (a no-op: the notes are not restored, by design).
-- =============================================================================
BEGIN;

ALTER TABLE memory_embeddings NO FORCE ROW LEVEL SECURITY;

DO $$
DECLARE
    purged INTEGER;
BEGIN
    DELETE FROM memory_embeddings WHERE source_type = 'care_checkin';
    GET DIAGNOSTICS purged = ROW_COUNT;
    RAISE NOTICE '115: purged % check-in note(s) from memory_embeddings', purged;
END $$;

ALTER TABLE memory_embeddings FORCE ROW LEVEL SECURITY;

COMMIT;
