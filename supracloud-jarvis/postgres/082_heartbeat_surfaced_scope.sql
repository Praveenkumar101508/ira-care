-- =============================================================================
-- 082_heartbeat_surfaced_scope.sql — Phase 12 P12-03b, finding H
--
-- The heartbeat de-duplication ledger gets a tenant, a workspace, and Row-Level
-- Security — and its uniqueness moves from GLOBAL to per-workspace.
--
-- ── IRREVERSIBLE-ONCE-USED: reversal re-opens finding H ────────────────────
--
-- Same position as `081`, and declared for the same reason. `rollback/082`
-- refuses on a deployment with more than one tenant, and the shared cycle's
-- database holds thousands by the time it runs — so 082 is never reversed
-- there, and cannot be re-applied either. Both halves are proved on a
-- disposable single-tenant database in the dedicated step instead.
--
-- ── What was wrong ─────────────────────────────────────────────────────────
--
-- `UNIQUE (item_key)` across the whole deployment. `item_key` is
-- `decision_review:<id>` or `stale_commitment:<source_id>`, so a collision
-- between two tenants means the SECOND one is silently suppressed: the row is
-- already there, `ON CONFLICT DO NOTHING` returns nothing, `_claim_item`
-- reports "already surfaced", and that tenant is never told about their own
-- stale commitment.
--
-- A missing heartbeat is the quietest failure in this subsystem. Nothing
-- errors, nothing logs, and the user simply never hears about the thing they
-- said they would do.
--
-- Phase 3A found this and recorded the fix without making it:
--
--   scripts/phase3a/generate_docs_2.py
--     | `heartbeat_surfaced` | `UNIQUE (item_key)` | `UNIQUE (tenant_id, user_id, item_key)` |
--     | Dedupe keys are per-person; global uniqueness suppresses another
--       user's notification. |
--
-- ── Why WORKSPACE and not USER, against that recommendation ────────────────
--
-- Phase 3A classified this `USER_PRIVATE`, and that was correct for the
-- product that existed then: a single-owner deployment where the deployment,
-- the tenant and the person were one party. They are not one party in cloud,
-- and the recommendation cannot be implemented as written, because
-- **the generator has no person**:
--
--   * `_claim_item` runs on `owner_connection`, which leaves `app.user_id`
--     deliberately UNSET — its docstring says so, and gives the reason: a
--     scheduled job has no authenticated person, and binding the owner's id
--     would hand it read access to the owner's TOTP secret and voiceprint.
--   * the heartbeat's principal comes from `default_workspace_job_principal`,
--     whose user is `uuid5(IRA_UUID_NAMESPACE, 'job:heartbeat')` — a synthetic
--     JOB identity, byte-identical for every tenant in the deployment.
--
-- A `user_id` column filled with that value is a per-person ledger in shape
-- and not in substance. It would isolate by tenant — because `tenant_id` is in
-- the key too — while recording `job:heartbeat` as the person, which is worse
-- than not claiming to be per-person at all.
--
-- What the ledger actually de-duplicates is workspace content.
-- `gather_pending_decisions` reads `decisions` and
-- `gather_stale_memory_commitments` reads `memory_embeddings`, both through
-- `workspace_connection`, both workspace-scoped since `049`. The item being
-- suppressed belongs to a workspace, so the record of having surfaced it
-- belongs to the same workspace.
--
-- This is the same conclusion `081` reached about `briefings`, for the same
-- reason, one batch earlier.
--
-- ── What this replaces, and why the mitigation was not the boundary ────────
--
-- `tests/tenancy/test_soft_failure_boundaries.py` pins `_claim_item` to
-- `owner_connection` because that helper REFUSES in the cloud edition. That is
-- a real mitigation and it is not a boundary: it prevents a cloud process from
-- writing the ledger by preventing it from writing at all. The test's own
-- failure message names this migration as the alternative —
--
--     "Either restore it or give the table a workspace and bind one."
--
-- — so this is the path the repository already wrote down.
--
-- ── Legacy rows ────────────────────────────────────────────────────────────
--
-- Deleted, and unlike `081` this costs almost nothing. The rows are de-dupe
-- keys, not content: losing them means an item that was already announced may
-- be announced once more. That is a duplicate heartbeat, not a disclosure and
-- not a loss. Assigning them a workspace by guess would instead SUPPRESS a
-- real item in whichever workspace received the guess, which is the failure
-- this migration exists to remove.
--
-- Rollback: rollback/082_heartbeat_surfaced_scope_down.sql
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_legacy bigint;
BEGIN
    SELECT count(*) INTO n_legacy FROM heartbeat_surfaced;
    IF n_legacy > 0 THEN
        RAISE NOTICE
            '082: deleting % de-duplication key(s). They carry no workspace, '
            'and guessing one would suppress a real item in whichever '
            'workspace received the guess. The only consequence of losing them '
            'is that an already-announced item may be announced once more.',
            n_legacy;
    END IF;
END $$;

DELETE FROM heartbeat_surfaced;

ALTER TABLE heartbeat_surfaced
    ADD COLUMN tenant_id    uuid NOT NULL
        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    ADD COLUMN workspace_id uuid NOT NULL
        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid;

ALTER TABLE heartbeat_surfaced
    ADD CONSTRAINT heartbeat_surfaced_tenant_id_fkey
        FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    ADD CONSTRAINT heartbeat_surfaced_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE;

-- THE FIX ITSELF. Global uniqueness is what suppresses another workspace's
-- item; scoped uniqueness de-duplicates within the workspace that owns the
-- item and nowhere else.
ALTER TABLE heartbeat_surfaced DROP CONSTRAINT heartbeat_surfaced_item_key_key;
ALTER TABLE heartbeat_surfaced
    ADD CONSTRAINT heartbeat_surfaced_scope_key
        UNIQUE (tenant_id, workspace_id, item_key);

ALTER TABLE heartbeat_surfaced ENABLE ROW LEVEL SECURITY;
ALTER TABLE heartbeat_surfaced FORCE ROW LEVEL SECURITY;

CREATE POLICY workspace_select ON heartbeat_surfaced FOR SELECT
    USING (tenant_id    = NULLIF(current_setting('app.tenant_id',    true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);

CREATE POLICY workspace_insert ON heartbeat_surfaced FOR INSERT
    WITH CHECK (tenant_id    = NULLIF(current_setting('app.tenant_id',    true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);

CREATE POLICY workspace_update ON heartbeat_surfaced FOR UPDATE
    USING (tenant_id    = NULLIF(current_setting('app.tenant_id',    true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (tenant_id    = NULLIF(current_setting('app.tenant_id',    true), '')::uuid
            AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);

CREATE POLICY workspace_delete ON heartbeat_surfaced FOR DELETE
    USING (tenant_id    = NULLIF(current_setting('app.tenant_id',    true), '')::uuid
       AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON heartbeat_surfaced TO ira_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON heartbeat_surfaced TO ira_worker;

COMMIT;
