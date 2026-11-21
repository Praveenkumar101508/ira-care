-- =============================================================================
-- rollback/048_workspace_backfill_down.sql
--
-- ── This rollback REFUSES, and there is nothing to restore ──────────────────
--
-- `048` set `workspace_id` on rows that had none. Undoing that means setting
-- them back to NULL, and there are two problems with it, either of which alone
-- would be enough.
--
-- **It cannot be done correctly.** The migration recorded no "was NULL before"
-- marker, and after the fact a row placed by `048` is indistinguishable from a
-- row that carried the same workspace all along — same tenant, same single
-- workspace, same value. Clearing "the rows 048 touched" would mean clearing
-- every row in every single-workspace tenant, which is far more than 048 did.
--
-- **It would restore the defect.** NULL `workspace_id` is precisely the state
-- that makes a row invisible under a workspace policy: the predicate evaluates
-- to NULL rather than false, and a policy predicate that is not TRUE denies.
-- Rolling forward is safe — a placed row is readable by the workspace it is in
-- and by nothing else. Rolling back manufactures rows that nobody can read.
--
-- So this stops. The schema stays where 048 left it, which is the safe
-- direction: every row attributed to a workspace, none invisible.
--
-- The same shape as rollback/046 and rollback/047, and for the same underlying
-- reason — the forward direction narrows, and the backward direction cannot
-- widen without deciding something about somebody's data.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    placed bigint := 0;
    t      text;
    n      bigint;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'agents', 'calendar_events', 'commitments', 'companion_versions',
        'companions', 'conversations', 'decisions', 'edges', 'entities',
        'files', 'lifeos_events', 'memory_embeddings', 'messages', 'people',
        'projects', 'reminders', 'tasks']
    LOOP
        EXECUTE format('SELECT count(*) FROM %I WHERE workspace_id IS NOT NULL', t)
           INTO n;
        placed := placed + n;
    END LOOP;

    RAISE EXCEPTION
        E'Refusing to reverse 048. % row(s) currently carry a workspace, and this script cannot tell which of them 048 placed.',
        placed
        USING HINT =
            'There is no marker to reverse. A row 048 placed is identical to '
            'one that already carried that workspace, so clearing "what 048 '
            'did" would clear every row in every single-workspace tenant. And '
            'the target state is the defect: a NULL workspace_id makes a row '
            'invisible under a workspace policy — the predicate is NULL, not '
            'false, and a predicate that is not TRUE denies. Staying on the '
            'backfilled state is the safe direction. If a specific row is in '
            'the wrong workspace, move that row.';
END
$$;

COMMIT;
