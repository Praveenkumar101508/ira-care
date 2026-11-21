-- =============================================================================
-- rollback/071_model_training_runs_down.sql
--
-- Drops the training-run tables.
--
-- ── It REFUSES while a run has been approved or has produced weights ───────
--
-- Two states cost something no re-run can recreate:
--
--   * an OWNER APPROVAL — PP-04, a person's decision to spend money and train
--     on somebody else's weights, with their name and the date on it;
--   * a COMPLETED run — the only record of which base model, which config
--     digest and which dataset digests produced a given adapter. Weights whose
--     provenance has been deleted are weights nobody can account for, and
--     `model_card` generation reads exactly these rows.
--
-- `draft`, `cancelled` and `failed` rows carry neither, so they are dropped
-- without argument.
--
-- ── Dropping this cannot make training possible ────────────────────────────
--
-- Which is why the guard is about the RECORD rather than about safety. With
-- these tables gone nothing can record a run at all — the closed direction. The
-- risk is amnesia, not exposure.
--
-- ── Order ──────────────────────────────────────────────────────────────────
--
-- The binding trigger fires on `training_run_datasets` and reads
-- `model_training_runs`, so it comes off first; otherwise the CASCADE from
-- dropping the parent would fire a trigger whose SELECT targets a table being
-- dropped in the same statement.
-- =============================================================================

BEGIN;


DO $$
DECLARE
    committed INTEGER;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'model_training_runs') THEN
        SELECT count(*) INTO committed
          FROM model_training_runs
         WHERE approved_by IS NOT NULL OR state = 'completed';

        IF committed > 0 THEN
            RAISE EXCEPTION
                '071-down REFUSING: % training run(s) carry an owner approval '
                'or produced an adapter. Dropping these tables deletes the only '
                'record of who approved the spend, and of which base model, '
                'config and datasets produced those weights.', committed
            USING HINT =
                'If that is genuinely what you mean, withdraw them '
                'deliberately first: UPDATE model_training_runs SET state = '
                '''cancelled'', approved_by = NULL, approved_at = NULL. Note '
                'that a completed run cannot be cancelled without also '
                'clearing adapter_sha256, which is the point — the provenance '
                'goes with the claim.';
        END IF;
    END IF;
END $$;

-- ── Later migrations first ─────────────────────────────────────────────────
--
-- `072` hangs `model_registry.training_run_id` off this table with a foreign
-- key, so dropping `model_training_runs` while `072` is applied fails on
-- PostgreSQL's own "other objects depend on it". Accurate, and it arrives after
-- the approval guard below has already passed — which reads as "the rollback is
-- broken" rather than "peel 072 first". The chain reverses in reverse order and
-- this says so, by name.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'model_registry'
                  AND column_name = 'training_run_id') THEN
        RAISE EXCEPTION
            '071-down OUT OF ORDER: 072 is still applied. '
            'model_registry.training_run_id references model_training_runs, so '
            'this file cannot drop it.'
        USING HINT =
            'Run postgres/rollback/072_model_promotion_gate_down.sql first — '
            'note that it refuses while any registry row still names the run '
            'that produced it.';
    END IF;
END $$;

DROP TRIGGER IF EXISTS training_run_binding_gate_trigger ON training_run_datasets;
DROP FUNCTION IF EXISTS training_run_binding_gate();

DROP TRIGGER IF EXISTS model_training_run_gate_trigger ON model_training_runs;
DROP FUNCTION IF EXISTS model_training_run_gate();

DROP TABLE IF EXISTS training_run_datasets;
DROP TABLE IF EXISTS model_training_runs;

COMMIT;
