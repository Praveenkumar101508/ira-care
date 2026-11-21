-- =============================================================================
-- 049_workspace_policies.sql — Phase 3B Batch 5C
--
-- The workspace boundary becomes a database boundary.
--
-- Every one of the seventeen workspace-scoped tables gets four per-command
-- policies whose predicate requires BOTH transaction-local settings:
--
--     tenant_id    = NULLIF(current_setting('app.tenant_id',    true), '')::uuid
-- AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
--
-- ── Why both, and why neither alone is sufficient ───────────────────────────
--
-- **Tenant alone** is what has been in place since 045, and it is what lets one
-- of a customer's workspaces read another's. That is the whole defect Batch 5C
-- exists to close.
--
-- **Workspace alone** looks equivalent and is not. Workspace ids are UUIDv4 and
-- therefore unguessable, which is an argument about difficulty rather than
-- about authorisation — and the moment any code path sets `app.workspace_id`
-- without setting `app.tenant_id`, a row would be reachable with no tenant
-- check at all. Requiring both means the two are checked independently and a
-- mistake in one is caught by the other. `test_phase3b_batch5c.py` proves each
-- half is insufficient by binding one and asserting zero rows.
--
-- ── Fail-closed, and the direction that matters ─────────────────────────────
--
-- An unset GUC gives NULL. `workspace_id = NULL` is NULL, not false, and a
-- policy predicate that is not TRUE denies. So a connection that binds only the
-- tenant now reads **zero rows** from these tables rather than all of them.
--
-- Reads fail silently and writes fail loudly, which is the wrong way round for
-- diagnosis and the right way round for safety. It is also exactly why `048`
-- had to complete first: a row with a NULL `workspace_id` matches nothing under
-- this predicate and would vanish from every read on the day this applies.
--
-- ── The preflight is the guarantee, not a courtesy ──────────────────────────
--
-- Step 1 refuses while any NULL `workspace_id` remains. That makes 048's
-- completion a hard prerequisite enforced by the chain rather than a rule
-- written in a document that somebody has to remember. There is no way to
-- reach these policies with rows they would hide.
--
-- ── Per-command, not FOR ALL ────────────────────────────────────────────────
--
-- Ten of the seventeen carried a single `tenant_isolation FOR ALL` policy and
-- seven carried four per-command ones. This unifies them on the per-command
-- shape, for the reason 045 gave when it introduced it: `FOR ALL` gives one
-- predicate to both USING and WITH CHECK, so the four operations cannot be
-- reasoned about — or narrowed — separately. UPDATE in particular needs both
-- arms stated, because USING alone would let a row be read from the caller's
-- workspace and written into another one. That is how a workspace steals a row
-- rather than reads one.
--
-- ── What this does NOT do ───────────────────────────────────────────────────
--
-- `workspace_id` stays NULLABLE. The NOT NULL constraint is `050`, deliberately
-- after this one: the policy makes existing NULL rows invisible, the constraint
-- makes new ones impossible, and applying the constraint first would fail on
-- rows that still existed while applying only the policy would leave the next
-- writer free to create more.
--
-- Rollback: rollback/049_workspace_policies_down.sql
-- =============================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 0. The RLS window — without which this migration silently does nothing
--
--    Every table below has FORCE ROW LEVEL SECURITY, and FORCE applies to the
--    schema owner too. This migration runs as the owner and binds no
--    `app.tenant_id`, so every SELECT here counts zero rows and every UPDATE
--    matches zero rows — **without failing**. A migration that reports success
--    having touched nothing is the worst possible outcome, and it has already
--    happened twice in this schema's history (`035`, and the CI comment about
--    superuser-vs-migrator backfills).
--
--    It happened a third time, which is why this window exists. `045` opened a
--    NO FORCE window over the seventeen content tables and backfilled them with
--    `default_workspace_id(tenant_id)` — a function that reads `workspaces`,
--    which was NOT in that window. So the lookup returned NULL for every row,
--    the backfill set NULL over NULL, and the migration reported success. The
--    defect was invisible until this batch made a NULL workspace refuse.
--
--    Hence `workspaces` is in the list below. Anything this migration READS to
--    decide what to write has to be, not just what it writes.
--
--    `NO FORCE`, never `DISABLE` — RLS stays enabled throughout, so only the
--    owner is exempt, only inside this transaction, and a failure rolls the
--    window shut with everything else.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'agents', 'calendar_events', 'commitments', 'companion_versions',
        'companions', 'conversations', 'decisions', 'edges', 'entities',
        'files', 'lifeos_events', 'memory_embeddings', 'messages', 'people',
        'projects', 'reminders', 'tasks', 'workspaces']
    LOOP
        EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY', t);
    END LOOP;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Preflight — refuse while any row would be hidden by what follows
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t         text;
    n         bigint;
    report    text := '';
    missing   text := '';
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'agents', 'calendar_events', 'commitments', 'companion_versions',
        'companions', 'conversations', 'decisions', 'edges', 'entities',
        'files', 'lifeos_events', 'memory_embeddings', 'messages', 'people',
        'projects', 'reminders', 'tasks']
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = t
               AND column_name = 'workspace_id')
        THEN
            missing := missing || format(E'\n  %s', t);
            CONTINUE;
        END IF;

        EXECUTE format('SELECT count(*) FROM %I WHERE workspace_id IS NULL', t)
           INTO n;
        IF n > 0 THEN
            report := report || format(E'\n  %s: %s row(s)', t, n);
        END IF;
    END LOOP;

    IF missing <> '' THEN
        RAISE EXCEPTION
            E'Refusing to apply workspace policies: these tables have no workspace_id column:%', missing
            USING HINT =
                '045 adds the column. A policy predicating on a column that '
                'does not exist would fail at query time rather than here.';
    END IF;

    IF report <> '' THEN
        RAISE EXCEPTION
            E'Refusing to apply workspace policies while rows have no workspace:%', report
            USING HINT =
                'Every one of these rows would become INVISIBLE the moment '
                'these policies exist — the predicate evaluates to NULL rather '
                'than false for a NULL workspace_id, and a predicate that is '
                'not TRUE denies. Not deleted: invisible, with no error raised '
                'anywhere. Run 048_workspace_backfill.sql, resolve whatever it '
                'refuses, and apply this again. scripts/phase3b/'
                'workspace_null_audit.py reports the full picture.';
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Replace the tenant-only policies with tenant-and-workspace ones
--
--    Dropped and recreated inside one transaction, so there is no window in
--    which a table has no policy while forced RLS is on — that window would
--    deny everything rather than allow it, which is safe, but a concurrent
--    reader seeing zero rows for a moment is still a defect.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t          text;
    scope      text := 'tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid'
                       ' AND workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid';
    policy     text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'agents', 'calendar_events', 'commitments', 'companion_versions',
        'companions', 'conversations', 'decisions', 'edges', 'entities',
        'files', 'lifeos_events', 'memory_embeddings', 'messages', 'people',
        'projects', 'reminders', 'tasks']
    LOOP
        -- Every existing policy on the table goes, by name from the catalogue
        -- rather than from a hardcoded list. Ten tables carry
        -- `tenant_isolation FOR ALL` and seven carry four per-command policies;
        -- reading the catalogue means this does not have to know which is which,
        -- and cannot leave a stale permissive policy behind. A leftover
        -- `FOR ALL` predicating only on tenant would be OR-ed with the new ones
        -- and would defeat the entire migration silently.
        FOR policy IN
            SELECT policyname FROM pg_policies
             WHERE schemaname = 'public' AND tablename = t
        LOOP
            EXECUTE format('DROP POLICY %I ON %I', policy, t);
        END LOOP;

        EXECUTE format(
            'CREATE POLICY workspace_select ON %I FOR SELECT USING (%s)', t, scope);
        EXECUTE format(
            'CREATE POLICY workspace_insert ON %I FOR INSERT WITH CHECK (%s)', t, scope);
        -- USING and WITH CHECK are the same predicate deliberately. USING alone
        -- would let an UPDATE read a row from the caller's workspace and write
        -- it into another — it passes the read check on the way in and lands
        -- anywhere. That is a cross-workspace write dressed as an edit.
        EXECUTE format(
            'CREATE POLICY workspace_update ON %I FOR UPDATE USING (%s) WITH CHECK (%s)',
            t, scope, scope);
        EXECUTE format(
            'CREATE POLICY workspace_delete ON %I FOR DELETE USING (%s)', t, scope);

        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    END LOOP;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Assert the intended state
--
--    A migration that reports success without checking is the one that makes
--    the next person trust a state nobody verified. Four properties, each of
--    which has a plausible way of not holding: the count, the predicates, the
--    absence of a leftover, and forced RLS.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t        text;
    rec      record;
    problems text := '';
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'agents', 'calendar_events', 'commitments', 'companion_versions',
        'companions', 'conversations', 'decisions', 'edges', 'entities',
        'files', 'lifeos_events', 'memory_embeddings', 'messages', 'people',
        'projects', 'reminders', 'tasks']
    LOOP
        -- Exactly four policies, and exactly the four expected names. A
        -- leftover permissive policy is OR-ed with these and would reopen the
        -- boundary without changing anything visible about the new ones.
        IF (SELECT count(*) FROM pg_policies
             WHERE schemaname = 'public' AND tablename = t) <> 4 THEN
            problems := problems || format(
                E'\n  %s has %s policies, expected 4', t,
                (SELECT count(*) FROM pg_policies
                  WHERE schemaname = 'public' AND tablename = t));
        END IF;

        IF EXISTS (SELECT 1 FROM pg_policies
                    WHERE schemaname = 'public' AND tablename = t
                      AND policyname NOT IN ('workspace_select', 'workspace_insert',
                                             'workspace_update', 'workspace_delete'))
        THEN
            problems := problems || format(
                E'\n  %s carries a policy this migration did not create', t);
        END IF;

        -- Every predicate must name BOTH settings. A policy that lost the
        -- workspace arm looks correct in a listing and enforces nothing new.
        FOR rec IN
            SELECT policyname, cmd, qual, with_check FROM pg_policies
             WHERE schemaname = 'public' AND tablename = t
        LOOP
            IF rec.cmd <> 'INSERT' AND (
                rec.qual IS NULL
                OR rec.qual NOT LIKE '%app.tenant_id%'
                OR rec.qual NOT LIKE '%app.workspace_id%')
            THEN
                problems := problems || format(
                    E'\n  %s.%s USING does not require both settings', t, rec.policyname);
            END IF;
            IF rec.cmd IN ('INSERT', 'UPDATE') AND (
                rec.with_check IS NULL
                OR rec.with_check NOT LIKE '%app.tenant_id%'
                OR rec.with_check NOT LIKE '%app.workspace_id%')
            THEN
                problems := problems || format(
                    E'\n  %s.%s WITH CHECK does not require both settings', t, rec.policyname);
            END IF;
        END LOOP;

        IF NOT EXISTS (
            SELECT 1 FROM pg_class
             WHERE oid = t::regclass AND relrowsecurity AND relforcerowsecurity)
        THEN
            problems := problems || format(
                E'\n  %s lost ENABLE/FORCE ROW LEVEL SECURITY', t);
        END IF;
    END LOOP;

    IF problems <> '' THEN
        RAISE EXCEPTION E'049 did not reach its intended state:%', problems;
    END IF;
END
$$;

COMMENT ON COLUMN tasks.workspace_id IS
    'Batch 5C. Part of the RLS predicate: a row is reachable only on a '
    'connection that has bound BOTH app.tenant_id and app.workspace_id. An '
    'unset setting yields NULL, the predicate is then NULL rather than false, '
    'and a predicate that is not TRUE denies — so an unbound connection reads '
    'nothing here rather than everything.';

-- ─────────────────────────────────────────────────────────────────────────────
-- Close the RLS window. Asserted, not assumed: a migration that left FORCE off
-- would hand the application's own role an owner-shaped hole in every policy.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t        text;
    problems text := '';
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'agents', 'calendar_events', 'commitments', 'companion_versions',
        'companions', 'conversations', 'decisions', 'edges', 'entities',
        'files', 'lifeos_events', 'memory_embeddings', 'messages', 'people',
        'projects', 'reminders', 'tasks', 'workspaces']
    LOOP
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        IF NOT EXISTS (SELECT 1 FROM pg_class
                        WHERE oid = t::regclass AND relrowsecurity AND relforcerowsecurity)
        THEN
            problems := problems || format(E'\n  %s', t);
        END IF;
    END LOOP;
    IF problems <> '' THEN
        RAISE EXCEPTION E'the RLS window did not close on:%', problems;
    END IF;
END
$$;

COMMIT;
