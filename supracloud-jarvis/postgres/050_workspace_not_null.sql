-- =============================================================================
-- 050_workspace_not_null.sql — Phase 3B Batch 5C, final stage
--
-- `workspace_id` becomes NOT NULL on all seventeen workspace-scoped tables.
--
-- ── Why this is last, and not part of 049 ───────────────────────────────────
--
-- The policy and the constraint do different jobs and have to land in this
-- order:
--
--   * **049 (the policy)** makes existing NULL rows invisible and stops any
--     connection creating new ones — `WITH CHECK` requires the row's workspace
--     to equal the bound one, and NULL never equals anything.
--   * **050 (the constraint)** makes a NULL unrepresentable at all.
--
-- Applying the constraint first would fail on every row that still had a NULL,
-- which is why it could not be part of 045. Applying only the policy would
-- leave the column able to hold a value nothing can read — a shape that is
-- legal in the schema and unreachable in practice, which is precisely the kind
-- of gap somebody later "fixes" by relaxing the policy.
--
-- ── What becomes true here that was not true before ─────────────────────────
--
-- **The composite foreign keys stop being vacuous.** `046` added FKs like
-- `edges (tenant_id, workspace_id, src_entity_id) REFERENCES entities
-- (tenant_id, workspace_id, id)`, and they are MATCH SIMPLE — a NULL in ANY
-- referencing column skips the check entirely. So while `workspace_id` was
-- nullable, an edge with a NULL workspace could join entities in two different
-- workspaces and no constraint objected. 049 covered that by making the row
-- unwritable; this fills it, which is a different and stronger statement.
--
-- **The scoped unique constraints stop depending on NULLS NOT DISTINCT.** That
-- property stays on the indexes regardless — it costs nothing once the column
-- cannot be NULL, and it is the thing that would still hold if a future
-- migration dropped this constraint.
--
-- ── The preflight ──────────────────────────────────────────────────────────
--
-- Refuses while any NULL remains, with a per-table count. `ALTER TABLE ... SET
-- NOT NULL` would refuse anyway, with a message naming one row and no context;
-- this names every table and how many, which is the difference between an
-- error and a work list.
--
-- Rollback: rollback/050_workspace_not_null_down.sql
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
-- 1. Forward repair — for databases that consumed a blind backfill
--
--    `045` opened a NO FORCE window over the seventeen content tables and then
--    backfilled them with `default_workspace_id(tenant_id)` — a function that
--    reads `workspaces`, which was NOT in that window. Under FORCE RLS the
--    lookup returned NULL for every row, the backfill wrote NULL over NULL, and
--    the migration recorded itself as applied having achieved nothing.
--
--    **Correcting 045 does not help those databases.** A migration recorded as
--    applied does not run again, so a fresh install and an upgraded one would
--    diverge silently — the worst possible outcome for a repair. Applied
--    migration history is immutable once any environment may have consumed it,
--    so the repair has to be a forward step, and this is it.
--
--    It lives here rather than in a later migration because the ordering is
--    forced: on an affected database `SET NOT NULL` below is the statement that
--    would fail, so the repair must precede it in the same transaction. A
--    separate 051 would never be reached.
--
--    ── What it will and will not decide ─────────────────────────────────────
--
--    Identical to `048`, deliberately: a row whose tenant holds exactly ONE
--    active workspace is placed, because there is one place it could go and
--    assigning it is arithmetic. Anything else is left for the preflight below
--    to refuse by name. There is no "first workspace", no "the default", and no
--    provisioning of a tenant that has none — those are decisions about
--    somebody's content and a migration is the worst place to make them.
--
--    On a database that never had the defect this is a no-op that reports zero,
--    which is why it is safe to carry for everyone.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t      text;
    placed bigint;
    total  bigint := 0;
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
                -- rather than a choice among candidates.
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
            RAISE NOTICE '050 repair: placed % row(s) in %', placed, t;
        END IF;
    END LOOP;
    IF total > 0 THEN
        RAISE NOTICE '050 repair: % row(s) placed that an earlier backfill missed', total;
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Preflight — a work list rather than an error
--
--    Anything the repair above could not place. Two causes and they need
--    opposite remedies, so the hint names both rather than merging them.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t      text;
    n      bigint;
    report text := '';
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'agents', 'calendar_events', 'commitments', 'companion_versions',
        'companions', 'conversations', 'decisions', 'edges', 'entities',
        'files', 'lifeos_events', 'memory_embeddings', 'messages', 'people',
        'projects', 'reminders', 'tasks']
    LOOP
        EXECUTE format('SELECT count(*) FROM %I WHERE workspace_id IS NULL', t)
           INTO n;
        IF n > 0 THEN
            report := report || format(E'\n  %s: %s row(s)', t, n);
        END IF;
    END LOOP;

    IF report <> '' THEN
        RAISE EXCEPTION
            E'Refusing to make workspace_id NOT NULL while rows have no workspace:%', report
            USING HINT =
                'The repair above places every row whose tenant holds '
                'exactly one active workspace, so what is left is either '
                'AMBIGUOUS — the tenant holds several and somebody has to say '
                'which — or HOMELESS, the tenant holds none and needs '
                'provisioning or the rows removed. Those are opposite answers '
                'and only an operator knows which applies. Note that these rows '
                'are already invisible under 049''s policies, so an application '
                'that appears to be working is not evidence they are gone: '
                'scripts/phase3b/workspace_null_audit.py reads them on an '
                'unfiltered connection and is the thing to trust here.';
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. The constraint
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'agents', 'calendar_events', 'commitments', 'companion_versions',
        'companions', 'conversations', 'decisions', 'edges', 'entities',
        'files', 'lifeos_events', 'memory_embeddings', 'messages', 'people',
        'projects', 'reminders', 'tasks']
    LOOP
        EXECUTE format('ALTER TABLE %I ALTER COLUMN workspace_id SET NOT NULL', t);
    END LOOP;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Assert the intended state — including what this migration MAKES true
--    elsewhere
--
--    The NOT NULL is the easy half. The composite foreign keys and the scoped
--    unique indexes were added by 046 and 047 and were partly inert until now;
--    checking them here is how "the vacuity is filled" becomes a verified
--    statement rather than a claim in a comment.
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
        'projects', 'reminders', 'tasks']
    LOOP
        IF NOT (SELECT attnotnull FROM pg_attribute
                 WHERE attrelid = t::regclass AND attname = 'workspace_id')
        THEN
            problems := problems || format(E'\n  %s.workspace_id is still nullable', t);
        END IF;

        -- 049's four policies are untouched by an ALTER COLUMN and must stay
        -- that way. Asserted rather than assumed: a schema change on a table is
        -- exactly the operation during which protections get switched off to
        -- make something apply.
        --
        -- FORCE RLS is deliberately NOT checked here: the backfill window above
        -- is still open at this point, so every table legitimately has FORCE
        -- off. The window-close block at the end of this file restores it and
        -- asserts it, which is the right place for that check and was found to
        -- be by putting it in the wrong one.
        IF (SELECT count(*) FROM pg_policies
             WHERE schemaname = 'public' AND tablename = t) <> 4 THEN
            problems := problems || format(E'\n  %s no longer carries 049''s four policies', t);
        END IF;
    END LOOP;

    IF problems <> '' THEN
        RAISE EXCEPTION E'050 did not reach its intended state:%', problems;
    END IF;
END
$$;

-- The composite foreign keys 046 added are no longer vacuous. Each must span
-- both scope columns; a FK that lost `workspace_id` would let an edge point at
-- an entity in another workspace with nothing to stop it.
DO $$
DECLARE
    rec      record;
    problems text := '';
BEGIN
    FOR rec IN
        SELECT conname, conrelid::regclass::text AS child,
               confrelid::regclass::text AS parent,
               (SELECT array_agg(a.attname ORDER BY x.ord)
                  FROM unnest(c.conkey) WITH ORDINALITY AS x(attnum, ord)
                  JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = x.attnum
               ) AS cols
          FROM pg_constraint c
         WHERE contype = 'f'
           AND conrelid::regclass::text IN ('edges', 'reminders')
           AND array_length(conkey, 1) > 1
    LOOP
        IF NOT ('workspace_id' = ANY(rec.cols)) THEN
            problems := problems || format(
                E'\n  %s on %s does not span workspace_id (%s)',
                rec.conname, rec.child, rec.cols);
        END IF;
    END LOOP;

    IF problems <> '' THEN
        RAISE EXCEPTION
            E'050: a composite foreign key does not carry the workspace:%', problems
            USING HINT =
                'These FKs are what stop a child row referencing a parent in '
                'another workspace. They were MATCH SIMPLE and therefore '
                'vacuous while workspace_id was nullable; making the column '
                'NOT NULL is what activates them, so this is the moment to '
                'check they are shaped to be worth activating.';
    END IF;
END
$$;

-- The scoped unique indexes 046 and 047 created. Each must include
-- `workspace_id`, or two workspaces cannot hold the same name.
DO $$
DECLARE
    want    text[] := ARRAY[
        'agents_tenant_workspace_name_key',
        'idx_entities_scope_type_name',
        'idx_edges_scope_src_dst_relation'];
    idx     text;
    def     text;
    problems text := '';
BEGIN
    FOREACH idx IN ARRAY want
    LOOP
        SELECT indexdef INTO def FROM pg_indexes
         WHERE schemaname = 'public' AND indexname = idx;
        IF def IS NULL THEN
            problems := problems || format(E'\n  %s is missing', idx);
        ELSIF position('workspace_id' in def) = 0 THEN
            problems := problems || format(E'\n  %s does not include workspace_id', idx);
        END IF;
    END LOOP;

    IF problems <> '' THEN
        RAISE EXCEPTION E'050: a scoped unique index is missing or unscoped:%', problems;
    END IF;
END
$$;

COMMENT ON COLUMN tasks.workspace_id IS
    'Batch 5C, final stage. NOT NULL since 050, and part of the RLS predicate '
    'since 049: a row is reachable only on a connection that has bound BOTH '
    'app.tenant_id and app.workspace_id. The NOT NULL is what makes 046''s '
    'MATCH SIMPLE composite foreign keys non-vacuous — while the column was '
    'nullable, a NULL in any referencing column skipped the check entirely.';

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
