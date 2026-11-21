-- =============================================================================
-- 097_langgraph_checkpoint_tables.sql
--
-- Conversation-graph state survives a restart on a native install: LangGraph's
-- checkpoint tables are created by the migration chain, not by the application.
-- Owner's decision, 24 October 2026, 00:52 UTC: option A of the checkpointer
-- finding (docs/testing/evidence/install-check-listeners-2026-10-23/README.md).
--
-- ── The defect ──────────────────────────────────────────────────────────────
--
-- `agents/graph.init_checkpointer` called LangGraph's `AsyncPostgresSaver.setup()`
-- at start-up, as the runtime role. `setup()` always begins with
-- `CREATE TABLE IF NOT EXISTS checkpoint_migrations`, and PostgreSQL checks
-- CREATE on the schema before it looks at IF NOT EXISTS. The runtime role has
-- no CREATE on `public`, on purpose (042). So on every native install `setup()`
-- failed, IRA fell back to an in-memory checkpointer, and a paused or
-- interrupted conversation graph did not survive a restart. Messages, memories
-- and approvals are IRA's own tables and were never affected.
--
-- ── What this file does ─────────────────────────────────────────────────────
--
--   * Creates the four tables exactly as langgraph-checkpoint-postgres 3.1.3's
--     ten migrations leave them (its `BasePostgresSaver.MIGRATIONS`), owned by
--     ira_schema_owner like every other table (042).
--   * Records those ten migrations (v 0-9) in `checkpoint_migrations`, so
--     `init_checkpointer` sees the schema is current and skips `setup()`. A
--     later LangGraph with more migrations finds the tables behind and falls
--     back to memory with a warning that says to migrate; it never half-applies.
--   * Grants ira_app SELECT, INSERT, UPDATE and DELETE on the three tables that
--     hold state, and SELECT only on `checkpoint_migrations`: LangGraph writes
--     that table only inside `setup()`, which the runtime role never runs, and a
--     runtime that could write it could mark a migration applied that is not.
--     No TRUNCATE, no REFERENCES, no DDL. ira_worker gets nothing; no worker
--     code touches the checkpointer.
--
-- ── Isolation, said plainly ─────────────────────────────────────────────────
--
-- These tables have no tenant, workspace or user column and no RLS: LangGraph
-- issues its own queries and sets no session variables, so a policy keyed on
-- `app.*` settings would hide every row from it. What separates one person's
-- state from another's is the thread key, `agents/graph.checkpoint_thread_id`:
-- the verified tenant, workspace and user come first and the client controls
-- only the suffix (3C-01, ADR-0009), and erasure removes a person's rows by
-- that prefix (`core/privacy/erasure._erase_checkpoints`). That is the model
-- the tables have had since 3C-01; this file moves their creation into the
-- chain and changes nothing about who can read them.
--
-- ── Existing tables ─────────────────────────────────────────────────────────
--
-- An install whose `setup()` once succeeded already has these tables; 042
-- moved them to ira_schema_owner. Every statement here is IF NOT EXISTS or ON
-- CONFLICT DO NOTHING, so such an install keeps its rows and gains whatever
-- the later LangGraph migrations added. CREATE INDEX runs without
-- CONCURRENTLY, which cannot run inside a transaction; the index names and
-- definitions are LangGraph's.
--
-- Rollback: rollback/097_langgraph_checkpoint_tables_down.sql
-- =============================================================================

BEGIN;

-- LangGraph migration 0
CREATE TABLE IF NOT EXISTS public.checkpoint_migrations (
    v INTEGER PRIMARY KEY
);

-- LangGraph migration 1
CREATE TABLE IF NOT EXISTS public.checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type TEXT,
    checkpoint JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

-- LangGraph migration 2
CREATE TABLE IF NOT EXISTS public.checkpoint_blobs (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    version TEXT NOT NULL,
    type TEXT NOT NULL,
    blob BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

-- LangGraph migration 3
CREATE TABLE IF NOT EXISTS public.checkpoint_writes (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    type TEXT,
    blob BYTEA NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

-- LangGraph migration 4 (5 is `SELECT 1`, a no-op kept for numbering)
ALTER TABLE public.checkpoint_blobs ALTER COLUMN blob DROP NOT NULL;

-- LangGraph migrations 6-8, without CONCURRENTLY (see the header)
CREATE INDEX IF NOT EXISTS checkpoints_thread_id_idx ON public.checkpoints(thread_id);
CREATE INDEX IF NOT EXISTS checkpoint_blobs_thread_id_idx ON public.checkpoint_blobs(thread_id);
CREATE INDEX IF NOT EXISTS checkpoint_writes_thread_id_idx ON public.checkpoint_writes(thread_id);

-- LangGraph migration 9
ALTER TABLE public.checkpoint_writes ADD COLUMN IF NOT EXISTS task_path TEXT NOT NULL DEFAULT '';

-- What `setup()` would have recorded, so the application skips it.
INSERT INTO public.checkpoint_migrations (v)
SELECT generate_series(0, 9)
ON CONFLICT (v) DO NOTHING;

ALTER TABLE public.checkpoint_migrations OWNER TO ira_schema_owner;
ALTER TABLE public.checkpoints           OWNER TO ira_schema_owner;
ALTER TABLE public.checkpoint_blobs      OWNER TO ira_schema_owner;
ALTER TABLE public.checkpoint_writes     OWNER TO ira_schema_owner;

REVOKE ALL ON public.checkpoint_migrations, public.checkpoints,
              public.checkpoint_blobs, public.checkpoint_writes
    FROM PUBLIC, ira_app, ira_worker;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON public.checkpoints, public.checkpoint_blobs, public.checkpoint_writes
    TO ira_app;
GRANT SELECT ON public.checkpoint_migrations TO ira_app;

COMMIT;
