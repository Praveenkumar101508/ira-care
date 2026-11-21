-- =============================================================================
-- 081_briefings_tenancy.sql — Phase 12 P12-03, finding G
--
-- ***  DESTRUCTIVE AND IRREVERSIBLE  ***
--
-- This migration DELETES every pre-existing row in `briefings`. The content is
-- not copied, archived or recoverable, and `rollback/081_briefings_tenancy_down.sql`
-- restores the SCHEMA only — it cannot and does not bring the rows back.
--
-- That is the intended outcome, not a side effect: those rows carry no provable
-- tenant or workspace ownership, and the alternative is assigning somebody's
-- business and security summary to a workspace chosen by guess. Read the
-- provenance section below before running this on data you care about.
--
-- The only other migration in this chain that deletes data is `040`. If that
-- surprises you, it should — this is the exception, and it is marked so that an
-- operator meets the fact here rather than in the row counts afterwards.
--
-- ── IRREVERSIBLE-ONCE-USED: reversal re-opens finding G ────────────────────
--
-- The marker `scripts/migration_workflow.py` greps for, declared here because
-- the shared rollback cycle cannot peel and re-apply this migration.
--
-- `rollback/081` REFUSES on a deployment with more than one tenant, and by the
-- time that cycle runs the shared database holds thousands. So 081 is never
-- actually reversed there, which means it cannot be re-applied either — its
-- columns, index, RLS and policies are all still in place, and re-running the
-- script would fail on the first `ADD COLUMN`.
--
-- This is exactly `051`'s position, which predates the marker and is handled
-- the same way by hand: the cycle asserts the refusal, tears down the two
-- foreign keys to `workspaces` as a fixture so `033`'s rollback can drop that
-- table, and restores them afterwards. Both halves of this script ARE executed
-- — the reversal on a disposable single-tenant database in the dedicated
-- `Migration 081` step, where it is a legitimate operation.
--
-- The exemption is from the CYCLE, not from being tested. A migration cannot
-- skip it by saying nothing; it has to say why, here, in the file the reviewer
-- is already reading.
--
-- `briefings` gets a tenant, a workspace, and Row-Level Security. Legacy rows
-- are DELETED rather than assigned one, and the reasoning for that is the
-- larger half of this header.
--
-- ── What was wrong ─────────────────────────────────────────────────────────
--
-- `briefings.content` is the most concentrated customer data in the schema.
-- `worker/briefing.py` gathers it from four workspace-scoped tables —
-- `_gather_business_data`, `_gather_security_data`, `_gather_tasks_data`,
-- `_gather_calendar_data` — each through a correctly BOUND connection, which
-- P12-01 observed by execution. The aggregate is then written to this table,
-- which until now had:
--
--     no tenant_id, no workspace_id, no row-level security, no policies.
--
-- and is served by `GET /api/v1/briefing/latest` on a bare `acquire()` behind
-- `require_auth` alone, with no predicate of any kind — `ORDER BY created_at
-- DESC LIMIT 1`. The most recent briefing generated anywhere in the deployment
-- was readable by every authenticated user in it.
--
-- Proved as the application role before this migration was written:
--
--     INSERT INTO briefings (...) VALUES ('morning', 'TENANT-A CONFIDENTIAL: …');
--     SELECT set_config('app.tenant_id', gen_random_uuid()::text, false);
--     SELECT content FROM briefings ORDER BY created_at DESC LIMIT 1;
--     -> TENANT-A CONFIDENTIAL: revenue 1.2M, 3 failed logins
--
-- This is finding 3C-10's shape and worse. That leaked a notification's title
-- and body; this leaks a deliberate summary of another tenant's revenue,
-- security posture, tasks and calendar.
--
-- ── Why the columns are BOTH NOT NULL, unlike 051 ──────────────────────────
--
-- `051` gave `notifications` a NOT NULL tenant and a NULLABLE workspace, and
-- was right to: a billing warning is a tenant-level event and forcing it to
-- name a workspace would mean inventing an answer. Its policy therefore reads
-- `workspace_id IS NULL OR workspace_id = app.workspace_id`.
--
-- That shape must NOT be copied here. A briefing is never tenant-wide — it is
-- the summary of ONE workspace's tasks, calendar, business and security data,
-- and it cannot be generated without a workspace to read them from. A NULL
-- workspace under 051's predicate means "visible from every workspace in the
-- tenant", which for this content is the same cross-workspace disclosure in a
-- smaller blast radius.
--
-- So `briefings` takes the seventeen tables' strict predicate, and both columns
-- are NOT NULL. A briefing whose provenance is unknown becomes UNREPRESENTABLE
-- rather than merely discouraged.
--
-- ── The legacy rows, and why they are deleted rather than backfilled ───────
--
-- Owner decision, P12-03. The short form: **losing an old briefing is
-- preferable to giving sensitive historical data to a workspace we only
-- guessed it belonged to.**
--
-- The test applied was whether ownership can be proven DETERMINISTICALLY from
-- durable evidence. It cannot, for either column:
--
--   * The row records nothing. There is no tenant column, no workspace column,
--     no generator version, no principal. The only column that could hint at
--     provenance is `created_at`, and turning a timestamp into an owner needs
--     deployment records that do not exist in this repository.
--
--   * The generator had three eras, and two of them bound nothing.
--     `887ca42` wrote briefings on a bare `acquire()` — no tenant, no
--     workspace. `9164ddd` ("Bind the eight unscoped write paths to a tenant")
--     added a tenant. `8fb75b7` ("Bind the file vault, companion service and
--     morning briefing") added `workspace_connection`. A row predating the
--     third commit was generated with no workspace context in existence, so
--     there is no workspace it "really" belonged to that this migration could
--     recover.
--
-- `PRIVATE_TENANT_ID` is good evidence about the CURRENT generation path —
-- `generate_briefing` reaches `default_workspace_job_principal(PRIVATE_TENANT_ID)`
-- and no production caller passes a principal. It is not evidence about a
-- given historical row, because nothing ties that row to that path.
--
-- ── Why deletion rather than quarantine ────────────────────────────────────
--
-- The owner's rule allows either, and asks for the simplest design consistent
-- with the retention model. The retention model settles it:
--
--   * `briefings` appears in NO retention policy and in NO erasure path. It is
--     absent from `core/privacy/erasure.py` entirely — it has no `tenant_id`,
--     so `_delete_scoped` could not reach it even if it were listed. These rows
--     are UN-ERASABLE today: a subject access deletion cannot touch them and
--     reports success regardless.
--   * Phase 3A already classified the table `USER_PRIVATE` — "Generated from
--     that user's own data." Nothing claims an audit or legal hold. Unlike
--     `trust_ledger`, it is not append-only by design; unlike
--     `mfa_recovery_codes`, dropping a row locks nobody out.
--
-- So a quarantine state would preserve un-erasable sensitive summaries with no
-- retention justification, and add a second visibility rule to get wrong.
-- Deleting them removes a standing privacy liability, and the count is
-- reported rather than performed quietly.
--
-- Nothing references `briefings` — no foreign key in the schema points at it —
-- so the delete strands no other row.
--
-- ── Provenance is mandatory from here ──────────────────────────────────────
--
--     generate briefing
--       -> verified tenant
--       -> verified workspace
--       -> bound gather passes
--       -> row stores tenant_id AND workspace_id
--       -> FORCE RLS
--       -> tenant/workspace-scoped API
--
-- Rollback: rollback/081_briefings_tenancy_down.sql
-- =============================================================================

BEGIN;

-- ── Step 1. Say what is about to be destroyed, before destroying it ─────────
DO $$
DECLARE
    n_legacy bigint;
BEGIN
    SELECT count(*) INTO n_legacy FROM briefings;
    RAISE NOTICE '081: % legacy briefing row(s) present.', n_legacy;
    IF n_legacy > 0 THEN
        RAISE WARNING
            '081 IS ABOUT TO DELETE % briefing row(s), IRREVERSIBLY. None '
            'carries provable tenant or workspace ownership: no such column '
            'has ever existed on this table, and the generator bound no '
            'workspace before 8fb75b7. They are deleted rather than assigned '
            'an owner by guess. The content is not archived and the rollback '
            'script cannot restore it. Rows deterministically migrated: 0. '
            'Rows quarantined: 0. Rows deleted: %.', n_legacy, n_legacy;
    END IF;
END $$;

-- WARNING rather than NOTICE, deliberately. `NOTICE` is below the default
-- `client_min_messages` in several tooling paths, so the one line an operator
-- most needs to see was the one most likely to be filtered out. Destroying
-- customer data quietly is the failure this whole phase is written against.

DELETE FROM briefings;

-- ── Step 2. The columns ─────────────────────────────────────────────────────
--
-- Added NOT NULL directly, which is only safe because step 1 emptied the
-- table. If a row somehow survives, this statement fails and the whole
-- migration rolls back — which is the correct outcome: it would mean the
-- delete did not do what its own NOTICE said.
ALTER TABLE briefings
    ADD COLUMN tenant_id    uuid NOT NULL
        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    ADD COLUMN workspace_id uuid NOT NULL
        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid;

-- The defaults are the same mechanism `workspace_memberships.tenant_id` and the
-- seventeen tables use: a writer that forgets the columns still lands in its
-- own bound scope rather than somewhere arbitrary, and a writer with nothing
-- bound gets NULL, which NOT NULL then refuses. Loud, not silent.

ALTER TABLE briefings
    ADD CONSTRAINT briefings_tenant_id_fkey
        FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    ADD CONSTRAINT briefings_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE;

-- ON DELETE CASCADE on both, unlike `notifications`' RESTRICT. A briefing is
-- derived data: when the workspace it summarises is torn down, keeping the
-- summary alive would outlive the thing it describes, and P12-01 finding E
-- showed a RESTRICT here becomes a teardown that cannot complete.

CREATE INDEX idx_briefings_scope
    ON briefings (tenant_id, workspace_id, briefing_type, created_at DESC);

-- ── Step 3. Row-Level Security, the strict predicate ───────────────────────
ALTER TABLE briefings ENABLE ROW LEVEL SECURITY;
ALTER TABLE briefings FORCE ROW LEVEL SECURITY;

-- FORCE, so the table owner is bound by its own policies too. Without it the
-- migration role and any future owner-run maintenance read everything, and the
-- boundary is decorative for exactly the connections most likely to be
-- scripted.

CREATE POLICY workspace_select ON briefings FOR SELECT
    USING (tenant_id    = NULLIF(current_setting('app.tenant_id',    true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);

CREATE POLICY workspace_insert ON briefings FOR INSERT
    WITH CHECK (tenant_id    = NULLIF(current_setting('app.tenant_id',    true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);

CREATE POLICY workspace_update ON briefings FOR UPDATE
    USING (tenant_id    = NULLIF(current_setting('app.tenant_id',    true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id    = NULLIF(current_setting('app.tenant_id',    true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);

CREATE POLICY workspace_delete ON briefings FOR DELETE
    USING (tenant_id    = NULLIF(current_setting('app.tenant_id',    true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);

-- Per-command rather than FOR ALL, matching `049`: a FOR ALL policy applies its
-- USING clause as the INSERT check too, which conflates "may read" with "may
-- write" and makes the two impossible to change independently.

GRANT SELECT, INSERT, UPDATE, DELETE ON briefings TO ira_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON briefings TO ira_worker;

COMMIT;
