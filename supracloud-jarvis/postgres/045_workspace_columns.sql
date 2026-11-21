-- =============================================================================
-- 045_workspace_columns.sql — Phase 3B Batch 5B (part 1 of 2)
--
-- The 17 `WORKSPACE_SCOPED` tables gain their ownership columns.
--
-- ── What this migration does NOT do, and why that is the whole design ────────
--
-- It does **not** add `app.workspace_id` to any policy, and it does **not**
-- force workspace RLS. That is Batch 5C, after the call sites are rewired.
--
-- R-43 is the reason. A workspace predicate on a policy is not "preparation" —
-- it is enforcement, effective on the next statement. Nothing in the
-- application calls `workspace_connection()` yet (Batch 5A shipped it unwired),
-- so a workspace predicate added here would make every current query return
-- zero rows: reads silently empty, writes refused. The column has to exist
-- before the call sites can be moved, and the call sites have to move before
-- the predicate can be enforced. This migration is the first of those three
-- steps and deliberately not the second.
--
-- The `PHASE_3B_BATCH_5B_READINESS_REVIEW.md` phrasing "extend the existing
-- policy" was ambiguous on exactly this point. Extending a Group A policy with
-- a workspace arm *is* forcing workspace isolation. It is 5C's work.
--
-- ── Two groups, two different jobs ───────────────────────────────────────────
--
--   Group A — 10 tables that already carry `tenant_id` with forced RLS.
--             They gain `workspace_id` only. Their existing `tenant_isolation`
--             policy is left exactly as it is: 5C rewrites it with a workspace
--             arm, and touching it twice would mean reviewing it twice.
--
--   Group B — 7 tables with NO `tenant_id`, NO RLS and NO policies at all
--             (R-64). They gain BOTH columns, and four per-command policies
--             built from scratch on the tenant predicate.
--
-- D-20 decided Group B gets both columns in one migration: the tables are
-- empty, no second tenant exists, and splitting would do the constraint rescope
-- in `046` twice.
--
-- ── The backfill, and the assumption inside it ───────────────────────────────
--
-- Every existing row is assigned to its tenant's DEFAULT workspace, resolved by
-- `default_workspace_id(tenant_id)` from `039_workspace_substrate.sql`. For
-- Group B, `tenant_id` is backfilled to PRIVATE_TENANT_ID first, on the same
-- grounds every earlier batch used: this schema has only ever run as a
-- single-owner install, so a row predating the column can only have come from
-- that owner.
--
-- **This is R-40 becoming live.** Assigning pre-existing content to one
-- workspace destroys any grouping that content already had. Batch 5A's
-- inventory found no grouping construct on any of the 17 — no folder, no
-- notebook, no project scope; `projects` groups nothing because nothing
-- references `projects.id` — so there is nothing to destroy today. That is a
-- statement about the data as it is now, and it expires the moment a second
-- tenant's content exists before this migration runs.
--
-- Neither column is a runtime default in the fallback sense. `workspace_id`
-- takes its DEFAULT from `app.workspace_id`, which is unset until 5C, so an
-- INSERT that omits it resolves to NULL — and NULL is permitted for now,
-- precisely because nothing sets the GUC yet. `046` does not add NOT NULL
-- either; that is 5C's contract stage, once the writers supply the value.
--
-- Rollback: rollback/045_workspace_columns_down.sql
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 0. Backfill window — see `035_memberships.sql` section 0
--
--    Group A's ten tables have forced RLS. The backfill below runs as the
--    schema owner, and FORCE applies to the owner too, so without this the
--    UPDATE would match zero rows *without failing* and every row would keep a
--    NULL workspace. `035` shipped that bug once; this is the fix pattern.
--
--    `NO FORCE`, never `DISABLE` — RLS stays enabled throughout, so only the
--    owner is exempt and only inside this migration. Restored in section 5.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['commitments','companion_versions','companions','conversations',
                             'files','lifeos_events','memory_embeddings','messages','people','tasks']
    LOOP
        EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY', t);
    END LOOP;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Group A — add the workspace column
--
--    ON DELETE RESTRICT, per the carry-forward rule. Deleting a workspace must
--    be a deliberate operation that names what it destroys; a cascade from
--    `workspaces` would silently take a customer's conversations with it, and
--    the deletion matrix (D-14) requires that to be an explicit erasure path
--    rather than a foreign-key side effect.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['commitments','companion_versions','companions','conversations',
                             'files','lifeos_events','memory_embeddings','messages','people','tasks']
    LOOP
        EXECUTE format(
            'ALTER TABLE %I ADD COLUMN IF NOT EXISTS workspace_id UUID '
            'REFERENCES workspaces(id) ON DELETE RESTRICT', t);
    END LOOP;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Group B — add BOTH columns (R-64, D-20)
--
--    These seven have had no tenant boundary at all since the schema began.
--    They were classified WORKSPACE_SCOPED in Phase 3A and therefore skipped by
--    every tenant batch, correctly — and the effect nobody wrote down is that
--    they stayed unprotected throughout.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['agents','calendar_events','decisions','edges',
                             'entities','projects','reminders']
    LOOP
        EXECUTE format(
            'ALTER TABLE %I ADD COLUMN IF NOT EXISTS tenant_id UUID '
            'REFERENCES tenants(id) ON DELETE RESTRICT', t);
        EXECUTE format(
            'ALTER TABLE %I ADD COLUMN IF NOT EXISTS workspace_id UUID '
            'REFERENCES workspaces(id) ON DELETE RESTRICT', t);
        -- Attribution, not authorisation. Nullable for good: several of these
        -- are written by workers with no user, and inventing one would be a lie
        -- in an attribution column. ON DELETE SET NULL, because deleting a
        -- person must not delete the tenant's content.
        EXECUTE format(
            'ALTER TABLE %I ADD COLUMN IF NOT EXISTS created_by UUID '
            'REFERENCES users(id) ON DELETE SET NULL', t);
    END LOOP;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Backfill
--
--    Group B's tenant first, then every table's workspace from that tenant's
--    default. Ordering matters: `default_workspace_id()` reads `tenant_id`, so
--    a Group B row with no tenant yet would resolve to NULL.
--
--    ── The append-only window ───────────────────────────────────────────────
--
--    `lifeos_events` refuses UPDATE outright:
--
--        This log is append-only: rows may be inserted and (for erasure)
--        deleted, but never edited. An event log whose entries can be
--        rewritten proves nothing about what happened.
--
--    That trigger is correct and this migration does not weaken it. But a
--    backfill of a NEW column is not an edit of what the log says: no `kind`,
--    no payload, no timestamp changes — only an ownership column that did not
--    exist when the row was written. The alternatives were worse. Leaving the
--    rows NULL would mean the tenant's own history is the one thing that never
--    joins a workspace. Adding a permanent carve-out to the trigger, the way
--    `biometric_audit` got one for redaction in Batch 1, would widen what the
--    application can do forever in exchange for a one-time need.
--
--    So the trigger is disabled for the duration of this migration and
--    re-enabled below — the same shape as the `NO FORCE` / `FORCE` window
--    above, and legitimate for the same reason: it is a schema migration
--    running as the schema owner, not something any application path can reach.
--    If this file fails, the transaction rolls back and the trigger comes back
--    with it.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE lifeos_events DISABLE TRIGGER lifeos_events_immutable;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['agents','calendar_events','decisions','edges',
                             'entities','projects','reminders']
    LOOP
        EXECUTE format(
            'UPDATE %I SET tenant_id = ''e91aceeb-89fb-5ad2-b43c-4eee2bad079c''::uuid '
            'WHERE tenant_id IS NULL', t);
    END LOOP;

    FOREACH t IN ARRAY ARRAY['agents','calendar_events','commitments','companion_versions',
                             'companions','conversations','decisions','edges','entities',
                             'files','lifeos_events','memory_embeddings','messages',
                             'people','projects','reminders','tasks']
    LOOP
        -- R-40 lives in this statement.
        EXECUTE format(
            'UPDATE %I SET workspace_id = default_workspace_id(tenant_id) '
            'WHERE workspace_id IS NULL AND tenant_id IS NOT NULL', t);
    END LOOP;
END
$$;

-- Close the append-only window immediately after the backfill, before anything
-- else in this file runs. `lifeos_events` refuses UPDATE again from here on.
ALTER TABLE lifeos_events ENABLE TRIGGER lifeos_events_immutable;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Defaults, indexes and Group B's policies
--
--    The `workspace_id` DEFAULT resolves to NULL until 5C binds the GUC. That
--    is intentional and is why NOT NULL is not applied here: a NOT NULL now
--    would refuse every insert the application currently makes.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t text;
    all_17 text[] := ARRAY['agents','calendar_events','commitments','companion_versions',
                           'companions','conversations','decisions','edges','entities',
                           'files','lifeos_events','memory_embeddings','messages',
                           'people','projects','reminders','tasks'];
    group_b text[] := ARRAY['agents','calendar_events','decisions','edges',
                            'entities','projects','reminders'];
BEGIN
    FOREACH t IN ARRAY all_17
    LOOP
        EXECUTE format(
            'ALTER TABLE %I ALTER COLUMN workspace_id '
            'SET DEFAULT NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid', t);
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_%s_workspace ON %I (tenant_id, workspace_id)', t, t);
    END LOOP;

    FOREACH t IN ARRAY group_b
    LOOP
        EXECUTE format(
            'ALTER TABLE %I ALTER COLUMN tenant_id '
            'SET DEFAULT NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid', t);

        -- Group B's tenant IS enforceable now — the application has bound
        -- `app.tenant_id` since `023`, so unlike the workspace arm this costs
        -- nothing to switch on and closes R-64.
        EXECUTE format('ALTER TABLE %I ALTER COLUMN tenant_id SET NOT NULL', t);
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);

        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_select ON %I', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_insert ON %I', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_update ON %I', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_delete ON %I', t);

        -- Four per-command policies, not one FOR ALL. R-48: a FOR ALL policy
        -- checks an UPDATE's NEW row against its USING clause as well as WITH
        -- CHECK, which silently couples read and write semantics and makes a
        -- redactable append-only table impossible to redact.
        EXECUTE format(
            'CREATE POLICY tenant_select ON %I FOR SELECT '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
        EXECUTE format(
            'CREATE POLICY tenant_insert ON %I FOR INSERT '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
        -- USING and WITH CHECK are the same predicate deliberately: a row must
        -- belong to the caller's tenant both before and after. USING alone
        -- would let an UPDATE move a row into another tenant — it passes the
        -- read check on the way in and lands anywhere. That is how a tenant
        -- steals a row rather than reads one.
        EXECUTE format(
            'CREATE POLICY tenant_update ON %I FOR UPDATE '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
        EXECUTE format(
            'CREATE POLICY tenant_delete ON %I FOR DELETE '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);

        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    END LOOP;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Close Group A's backfill window
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['commitments','companion_versions','companions','conversations',
                             'files','lifeos_events','memory_embeddings','messages','people','tasks']
    LOOP
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    END LOOP;
END
$$;

COMMENT ON COLUMN conversations.workspace_id IS
    'Owning workspace. Nullable until Batch 5C: the DEFAULT reads app.workspace_id, '
    'which nothing sets yet, and no policy reads this column. Adding a workspace '
    'predicate before the call sites bind the GUC would return zero rows for every '
    'query (R-43).';
COMMENT ON COLUMN agents.tenant_id IS
    'Tenant owner. This table had NO tenant boundary at all until Batch 5B (R-64) — '
    'it was classified WORKSPACE_SCOPED in Phase 3A and correctly skipped by every '
    'tenant batch, which left it unprotected throughout.';
