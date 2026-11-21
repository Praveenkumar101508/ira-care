-- =============================================================================
-- rollback/070_training_datasets_down.sql
--
-- Drops the training-dataset governance tables.
--
-- ── It REFUSES while a dataset carries a human determination ───────────────
--
-- Two things in these tables cost a person's time and judgement rather than a
-- machine's, and neither can be reconstructed by re-running anything:
--
--   * a licence classification of `known_approved` or `known_restricted`,
--     with the name of whoever read the terms;
--   * a recorded lawful basis, which is `PP-05` — the answer to whether IRA
--     may train on this data at all.
--
-- A rollback that discarded those would silently delete the only record that
-- somebody made those decisions, and the next person would have to make them
-- again with no way of knowing they ever had been.
--
-- ── The check results are NOT guarded, and that is deliberate ──────────────
--
-- `dataset_checks` rows are reproducible: re-run the scanner. Losing them
-- cannot make anything usable, because with the tables gone nothing can reach
-- `train` at all — the closed direction. Guarding them would make an emergency
-- rollback harder for no gain.
--
-- ── Order ──────────────────────────────────────────────────────────────────
--
-- `dataset_checks` references `training_datasets` with ON DELETE CASCADE, and
-- both triggers hang off `training_datasets`. Triggers first, then the child
-- table, then the parent — dropping the parent first would work through the
-- cascade but leaves the functions orphaned, and a later re-apply would find
-- `CREATE OR REPLACE` racing a definition it did not expect.
--
-- ── And a rollback out of order says so (found in CI by P7-04) ─────────────
--
-- `071` added `training_run_datasets.dataset_id REFERENCES training_datasets
-- ON DELETE RESTRICT`, so running this file while `071` is applied fails on
-- `DROP TABLE training_datasets` with PostgreSQL's own message: *"cannot drop
-- table training_datasets because other objects depend on it"*. Accurate, and
-- it arrives AFTER the determination guard has already passed — so an operator
-- reads it as "the rollback is broken" rather than "peel 071 first".
--
-- A migration chain reverses in reverse order. The guard below says which file
-- to run, by name, instead of leaving that to be worked out from a constraint
-- name. Any later migration that references these tables belongs in the same
-- list.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    determined INTEGER;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'training_datasets') THEN
        SELECT count(*) INTO determined
          FROM training_datasets
         WHERE licence_classification IN ('known_approved', 'known_restricted')
            OR lawful_basis IS NOT NULL;

        IF determined > 0 THEN
            RAISE EXCEPTION
                '070-down REFUSING: % dataset(s) carry a human determination — '
                'a licence classification or a recorded lawful basis. Dropping '
                'these tables deletes the only record that somebody made those '
                'decisions.', determined
            USING HINT =
                'Withdraw them deliberately first if that is what you mean: '
                'UPDATE training_datasets SET licence_classification = '
                '''review_required'', licence_classified_by = NULL, '
                'licence_classified_at = NULL, lawful_basis = NULL, '
                'lawful_basis_by = NULL, lawful_basis_at = NULL. Then re-run '
                'this rollback.';
        END IF;
    END IF;
END $$;

-- ── Later migrations first ─────────────────────────────────────────────────
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'training_run_datasets') THEN
        RAISE EXCEPTION
            '070-down OUT OF ORDER: 071 is still applied. '
            'training_run_datasets references training_datasets, so this file '
            'cannot drop it.'
        USING HINT =
            'Run postgres/rollback/071_model_training_runs_down.sql first — '
            'note that it refuses while any run carries an owner approval or '
            'produced an adapter, which is the decision that has to be made '
            'before this one.';
    END IF;
END $$;

DROP TRIGGER IF EXISTS training_dataset_promotion ON training_datasets;
DROP TRIGGER IF EXISTS training_dataset_content_change ON training_datasets;
DROP FUNCTION IF EXISTS training_dataset_promotion_gate();
DROP FUNCTION IF EXISTS training_dataset_content_changed();

DROP TABLE IF EXISTS dataset_checks;
DROP TABLE IF EXISTS training_datasets;

COMMIT;
