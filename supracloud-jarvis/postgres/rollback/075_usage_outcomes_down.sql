-- =============================================================================
-- rollback/075_usage_outcomes_down.sql
--
-- Narrows `model_usage.outcome` back to `029`'s four values.
--
-- ── Why this refuses rather than deletes ───────────────────────────────────
--
-- Rows already recorded with `refused_policy` or `abandoned` cannot survive the
-- narrower CHECK. There are two ways to handle that and only one of them is
-- honest:
--
--   * delete them, and lose the record that IRA blocked an unapproved model;
--   * refuse, and make somebody decide.
--
-- It refuses. `model_usage` is a billing and audit record — deleting rows to
-- make a constraint fit is the shape `064` and `070-down` already refuse for
-- their own tables. The hint says exactly what to run if discarding them is
-- genuinely what is meant.
--
-- Note what this rollback does NOT restore: the DEFECT. Before 075 the gateway
-- wrote `refused_policy` and the fail-soft writer dropped it. After this
-- rollback it will do so again. That is what reversing this migration means,
-- and it is stated here so nobody runs it expecting a clean revert.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    stranded INTEGER;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'model_usage') THEN
        RETURN;
    END IF;

    SELECT count(*) INTO stranded
      FROM model_usage
     WHERE outcome IN ('refused_policy', 'abandoned');

    IF stranded > 0 THEN
        RAISE EXCEPTION
            '075-down REFUSING: % usage row(s) record refused_policy or '
            'abandoned. Narrowing the vocabulary would make them '
            'unrepresentable, and they are the record that IRA refused an '
            'unapproved model or that a stream was never read.', stranded
        USING HINT =
            'If discarding them is genuinely what you mean, do it deliberately '
            'first: DELETE FROM model_usage WHERE outcome IN '
            '(''refused_policy'', ''abandoned''). Then re-run this rollback.';
    END IF;
END $$;

ALTER TABLE model_usage
    DROP CONSTRAINT IF EXISTS refusals_consume_nothing;

ALTER TABLE model_usage
    ADD CONSTRAINT refusals_consume_nothing CHECK (
        outcome NOT IN ('refused_privacy', 'refused_quota')
        OR (prompt_tokens = 0 AND completion_tokens = 0 AND cost = 0)
    );

ALTER TABLE model_usage
    DROP CONSTRAINT IF EXISTS model_usage_outcome_check;

ALTER TABLE model_usage
    ADD CONSTRAINT model_usage_outcome_check CHECK (
        outcome IN ('ok', 'refused_privacy', 'refused_quota', 'error')
    );

COMMENT ON COLUMN model_usage.outcome IS
    'ok | refused_privacy | refused_quota | error. Note that the gateway also '
    'writes refused_policy, which this vocabulary rejects and the fail-soft '
    'usage writer then drops — see 075.';

COMMIT;
