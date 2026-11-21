-- =============================================================================
-- rollback/097_langgraph_checkpoint_tables_down.sql
--
-- Reverses 097: LangGraph's checkpoint tables leave the migration chain.
--
-- ── What this gives back, said plainly ──────────────────────────────────────
--
-- The behaviour before 097. The runtime role cannot create these tables, so
-- `agents/graph.init_checkpointer` falls back to an in-memory checkpointer and
-- a paused conversation graph does not survive a restart. Revert the
-- application change with this file; the application keeps working either way,
-- in memory.
--
-- ── What it deletes ─────────────────────────────────────────────────────────
--
-- Every saved conversation-graph checkpoint. Messages, memories and approvals
-- are IRA's own tables and are untouched. On an install whose tables existed
-- before 097 (a `setup()` that once succeeded), this drops those rows too:
-- there is no way to tell them apart, and leaving tables behind would leave
-- conversation content that erasure then has to keep finding.
-- =============================================================================

BEGIN;

DROP TABLE IF EXISTS public.checkpoint_writes;
DROP TABLE IF EXISTS public.checkpoint_blobs;
DROP TABLE IF EXISTS public.checkpoints;
DROP TABLE IF EXISTS public.checkpoint_migrations;

COMMIT;
