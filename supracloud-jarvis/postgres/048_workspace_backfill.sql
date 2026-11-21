-- =============================================================================
-- 048_workspace_backfill.sql — Phase 3B Batch 5C
--
-- Give every workspace-scoped row a workspace, where that is determined, and
-- REFUSE where it is not.
--
-- ── Why this has to happen before the policies, not with them ───────────────
--
-- `045` added `workspace_id` to the seventeen workspace-scoped tables as a
-- NULLABLE column. That was the only choice available: `NOT NULL` on a
-- populated table with no writer supplying a value is a migration that cannot
-- apply. The consequence is rows that are currently legal and currently
-- readable, and would stop being either the moment a policy reads the GUC:
--
--     workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
--
-- For a NULL row that predicate is **NULL, not false**, and a policy predicate
-- that is not TRUE denies. So on the day the policies land, every NULL row
-- disappears from every read simultaneously. Not deleted — invisible, with no
-- error raised anywhere and nothing in any log to explain why somebody's vault
-- went empty. That is the single worst failure mode available in this whole
-- retrofit, and it is entirely preventable by counting first.
--
-- `scripts/phase3b/workspace_null_audit.py` is the count. This is the fix for
-- the part of it that is arithmetic.
--
-- ── What this migration will and will not decide ────────────────────────────
--
-- **Backfilled.** A row whose tenant has exactly ONE active workspace. There is
-- precisely one place it could belong, so assigning it is arithmetic and not a
-- judgement about anybody's content.
--
-- **Refused — tenant has several workspaces.** Choosing which of a customer's
-- workspaces their task, memory or conversation belongs in is a decision about
-- their data. A migration is the worst possible place to make it silently: a
-- backfill that guesses is indistinguishable from one that is right until
-- somebody looks, and by then the rows have moved.
--
-- **Refused — tenant has no workspace at all.** There is nowhere to put the
-- row. Either the tenant was never put through `provision_tenant_substrate`,
-- or the rows are residue from a tenant that no longer operates. Provisioning
-- and deleting are both defensible and they are opposite; the operator picks.
--
-- **Refused — row has no tenant either.** It cannot be attributed to an owner,
-- so it cannot be placed. This should be impossible (`tenant_id` is NOT NULL on
-- these tables) and is checked anyway, because "should be impossible" is how
-- the interesting failures introduce themselves.
--
-- ── On a fresh database this migration does nothing ─────────────────────────
--
-- Which is correct, and worth stating so nobody concludes it is broken. Its
-- work is on deployments that already hold content. The refusals are exercised
-- by `tests/tenancy/test_migration_048_workspace_backfill.py`, which builds
-- each state deliberately rather than waiting for one to occur.
--
-- Rollback: rollback/048_workspace_backfill_down.sql — which refuses, because
-- setting workspaces back to NULL would recreate the invisibility this exists
-- to prevent.
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
-- 1. No row may be unattributable
--
--    Checked before anything is written. A row with no tenant cannot be placed
--    in a workspace and cannot be reasoned about at all, so finding one means
--    stopping rather than proceeding around it.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t        text;
    orphans  bigint;
    report   text := '';
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'agents', 'calendar_events', 'commitments', 'companion_versions',
        'companions', 'conversations', 'decisions', 'edges', 'entities',
        'files', 'lifeos_events', 'memory_embeddings', 'messages', 'people',
        'projects', 'reminders', 'tasks']
    LOOP
        EXECUTE format('SELECT count(*) FROM %I WHERE tenant_id IS NULL', t)
           INTO orphans;
        IF orphans > 0 THEN
            report := report || format(E'\n  %s: %s row(s)', t, orphans);
        END IF;
    END LOOP;

    IF report <> '' THEN
        RAISE EXCEPTION
            E'Refusing to backfill: rows with no tenant_id:%', report
            USING HINT =
                'These rows cannot be attributed to an owner, so they cannot be '
                'placed in a workspace. tenant_id is NOT NULL on every one of '
                'these tables, so their existence means the constraint was '
                'dropped or the rows predate it. Resolve them by hand — '
                'assigning them here would be inventing an owner.';
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Backfill the determined case
--
--    One statement per table, each restricted to tenants holding exactly one
--    active workspace. `is_default` is deliberately NOT consulted: the question
--    is how many places the row could go, and where there is one place its
--    default-ness is irrelevant. Where there are several, being the default
--    does not make it the right one.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t       text;
    placed  bigint;
    total   bigint := 0;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'agents', 'calendar_events', 'commitments', 'companion_versions',
        'companions', 'conversations', 'decisions', 'edges', 'entities',
        'files', 'lifeos_events', 'memory_embeddings', 'messages', 'people',
        'projects', 'reminders', 'tasks']
    LOOP
        EXECUTE format($fmt$
            UPDATE %I AS target
               SET workspace_id = sole.id
              FROM (
                -- `HAVING count(*) = 1` is what makes this safe: the group has
                -- exactly one row, so the aggregate is a syntax requirement
                -- rather than a choice among candidates. (`array_agg` and not
                -- `min`, because PostgreSQL has no `min(uuid)` — and a cast to
                -- text to get one would silently impose an ordering, which is
                -- the shape of picking a winner.)
                SELECT tenant_id, (array_agg(id))[1] AS id
                  FROM workspaces
                 WHERE status = 'active'
                 GROUP BY tenant_id
                HAVING count(*) = 1) AS sole
             WHERE target.workspace_id IS NULL
               AND target.tenant_id = sole.tenant_id
        $fmt$, t);
        GET DIAGNOSTICS placed = ROW_COUNT;
        total := total + placed;
        IF placed > 0 THEN
            RAISE NOTICE '048: placed % row(s) in %', placed, t;
        END IF;
    END LOOP;
    RAISE NOTICE '048: % row(s) placed in total', total;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Refuse on whatever is left
--
--    Two causes, reported separately because they need opposite remedies, and
--    a report that merged them would send the operator looking for content
--    decisions that do not exist. The diagnostic names tenant, table and count
--    — never row contents, which are customer data.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t          text;
    rec        record;
    ambiguous  text := '';
    homeless   text := '';
    stmt       text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'agents', 'calendar_events', 'commitments', 'companion_versions',
        'companions', 'conversations', 'decisions', 'edges', 'entities',
        'files', 'lifeos_events', 'memory_embeddings', 'messages', 'people',
        'projects', 'reminders', 'tasks']
    LOOP
        stmt := format($fmt$
            SELECT c.tenant_id,
                   c.n,
                   coalesce(w.n, 0) AS workspaces
              FROM (SELECT tenant_id, count(*) AS n
                      FROM %I
                     WHERE workspace_id IS NULL
                     GROUP BY tenant_id) c
              LEFT JOIN (SELECT tenant_id, count(*) AS n
                           FROM workspaces
                          WHERE status = 'active'
                          GROUP BY tenant_id) w
                ON w.tenant_id = c.tenant_id
             ORDER BY c.n DESC
             LIMIT 20
        $fmt$, t);

        FOR rec IN EXECUTE stmt
        LOOP
            IF rec.workspaces = 0 THEN
                homeless := homeless || format(
                    E'\n  %s tenant=%s rows=%s', t, rec.tenant_id, rec.n);
            ELSE
                ambiguous := ambiguous || format(
                    E'\n  %s tenant=%s rows=%s workspaces=%s',
                    t, rec.tenant_id, rec.n, rec.workspaces);
            END IF;
        END LOOP;
    END LOOP;

    IF ambiguous <> '' OR homeless <> '' THEN
        RAISE EXCEPTION
            E'Refusing to complete 048 — rows remain with no workspace.\n\nAMBIGUOUS (tenant holds several workspaces; a decision about their content is owed):%\n\nHOMELESS (tenant holds no workspace; nowhere to put the row):%',
            coalesce(nullif(ambiguous, ''), E'\n  none'),
            coalesce(nullif(homeless, ''), E'\n  none')
            USING HINT =
                'Run scripts/phase3b/workspace_null_audit.py for the full '
                'picture. AMBIGUOUS rows need somebody to say which workspace '
                'they belong to — this migration will not choose, because a '
                'backfill that guesses is indistinguishable from one that is '
                'right until somebody looks. HOMELESS rows need their tenant '
                'provisioned (provision_tenant_substrate) or the rows removed; '
                'those are opposite answers and only the operator knows which '
                'applies. Nothing has been changed — this migration is in a '
                'transaction and is about to roll back.';
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Assert the intended state
--
--    Reaching here means step 3 found nothing, so this should be trivially
--    true. Asserted anyway: a migration that reports success without checking
--    is the one that makes the next person trust a state nobody verified.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t         text;
    remaining bigint;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'agents', 'calendar_events', 'commitments', 'companion_versions',
        'companions', 'conversations', 'decisions', 'edges', 'entities',
        'files', 'lifeos_events', 'memory_embeddings', 'messages', 'people',
        'projects', 'reminders', 'tasks']
    LOOP
        EXECUTE format('SELECT count(*) FROM %I WHERE workspace_id IS NULL', t)
           INTO remaining;
        IF remaining > 0 THEN
            RAISE EXCEPTION
                '048 finished with % NULL workspace_id row(s) still in %',
                remaining, t;
        END IF;
    END LOOP;
END
$$;

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
