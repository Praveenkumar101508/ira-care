-- =============================================================================
-- 066_job_runs.sql — Phase 4 batch 11, background work that can be observed
--
-- P4-06 made retention enforceable and gave it an HTTP caller. Nothing ran it on
-- a schedule, so a user's retention window still only applied when somebody
-- pressed a button. This is the table the scheduler writes to.
--
-- ── Why a table and not a log line ──────────────────────────────────────────
--
-- "Did retention run last night, and what did it remove" is a question asked
-- after a complaint, and log retention is shorter than data retention almost
-- everywhere. A row survives, can be queried by tenant, and can be joined
-- against the deletions it caused.
--
-- It is also what makes the job SAFE to restart. A run that dies mid-flight
-- leaves a `running` row with no `finished_at`; the next start can see it,
-- record it as interrupted, and carry on — rather than the operator inferring
-- from an absence.
--
-- ── Not tenant-scoped, deliberately ────────────────────────────────────────
--
-- A job run spans tenants: one tick enumerates every tenant that has retention
-- settings. Attaching `tenant_id` to the run row would force one row per tenant
-- per tick and make "did the 03:00 pass happen" a question about a set of rows
-- rather than one.
--
-- Per-tenant OUTCOMES live in `detail`, a JSONB object keyed by tenant id, so
-- the aggregate and the breakdown are in the same row and cannot disagree.
--
-- The table therefore holds no user content and no tenant-scoped data — only
-- counts and identifiers — so it is readable by the operator and NOT exposed to
-- the application role beyond INSERT/UPDATE of its own runs.
--
-- Rollback: rollback/066_job_runs_down.sql
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS job_runs (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job          TEXT NOT NULL,
    -- Which process claimed it. An operator chasing a stuck run needs to know
    -- which instance to look at, and in a fleet "the scheduler" is not an
    -- answer.
    runner       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running', 'succeeded', 'failed',
                                   'interrupted', 'skipped')),
    started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ,
    -- Aggregates, so the common question is answerable without parsing JSON.
    tenants_seen INTEGER NOT NULL DEFAULT 0,
    items_removed INTEGER NOT NULL DEFAULT 0,
    -- Per-tenant breakdown and, for a failure, the error TYPE. Never a message:
    -- an exception string can carry a row identifier or a fragment of content,
    -- and this table is read by operators who are not that tenant.
    detail       JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT job_runs_finished_when_not_running
        CHECK (status = 'running' OR finished_at IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS job_runs_recent_idx ON job_runs (job, started_at DESC);

-- Finding a run that never finished is the restart-recovery query, and it must
-- not scan the whole history to do it.
CREATE INDEX IF NOT EXISTS job_runs_unfinished_idx
    ON job_runs (job, started_at) WHERE finished_at IS NULL;

COMMENT ON TABLE job_runs IS
    'One row per scheduled job execution. Holds counts and identifiers only — '
    'no user content and no tenant-scoped data. See core/jobs/scheduler.py.';

-- ── Access, and a property this deliberately does NOT claim ────────────────
--
-- The first draft granted `ira_app` SELECT only, with a comment saying a request
-- path that could write here could forge a record of work that never happened.
-- That would have been a good property. **It is not the one this deployment
-- has**, and the tests said so immediately: the scheduler is started by the
-- application's own lifespan, so it holds `ira_app` and needs to write.
--
-- The choice was between weakening the grant and weakening the claim. Keeping
-- the comment while granting the write would have been the third option, and
-- the worst — a control document that describes a control nobody implemented is
-- the defect P4-07 spent a whole batch correcting.
--
-- So: `ira_app` can write here, and a compromised request path can therefore
-- write a plausible job-run row. The stronger arrangement is a SEPARATE worker
-- process holding `ira_worker`, where the API role keeps SELECT alone. The
-- grants below already support it — move the scheduler out of the API lifespan
-- and revoke INSERT/UPDATE from `ira_app`, and nothing else changes.
--
-- What this table is NOT is an audit log. It records that work happened, not
-- who reached what, so a forged row is misleading rather than evidence-
-- destroying. `auth_events` (061) is the append-only one, and it is append-only
-- for every role including this one.
--
-- No DELETE for either role. Truncating job history is an operator action.
GRANT SELECT, INSERT, UPDATE ON job_runs TO ira_app;
GRANT SELECT, INSERT, UPDATE ON job_runs TO ira_worker;

-- RLS is enabled with no permissive policy for `ira_app` beyond SELECT, which
-- is the shape 042 established: the grant is the control, and RLS is the second
-- line. FORCE so the table owner is not exempt either.
ALTER TABLE job_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_runs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS job_runs_read ON job_runs;
CREATE POLICY job_runs_read ON job_runs FOR SELECT USING (TRUE);

DROP POLICY IF EXISTS job_runs_write ON job_runs;
CREATE POLICY job_runs_write ON job_runs FOR INSERT WITH CHECK (TRUE);

DROP POLICY IF EXISTS job_runs_finish ON job_runs;
CREATE POLICY job_runs_finish ON job_runs FOR UPDATE USING (TRUE) WITH CHECK (TRUE);

COMMIT;
