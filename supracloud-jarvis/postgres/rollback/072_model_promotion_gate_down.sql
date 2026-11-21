-- =============================================================================
-- rollback/072_model_promotion_gate_down.sql
--
-- Removes the promotion gate and the two columns it added.
--
-- ── It REFUSES while any registry row names a training run ─────────────────
--
-- `training_run_id` is the only link between a served artefact and the run that
-- produced it. Dropping the column deletes that link and leaves
-- `adapter_version` behind — a string saying `v1` with nothing under it, which
-- is precisely the state `072` exists to make unreachable.
--
-- The registry row survives the rollback and keeps serving. So this is not a
-- theoretical loss: it is a model in production whose provenance has been
-- deleted while the model carries on answering requests.
--
-- ── Dropping this cannot make anything serve that could not before ─────────
--
-- `067`'s `serving_is_licensed` and `068`'s `model_registry_licence_gate` are
-- untouched. What is lost is the *provenance*, not the licence — which is why
-- the guard is about the record rather than about exposure.
--
-- ── Order ──────────────────────────────────────────────────────────────────
--
-- Trigger, then function, then constraints, then columns. A column drop would
-- cascade the constraints anyway; naming them makes the reversal legible and
-- means a partial rollback does not leave a constraint referring to a column
-- that is on its way out.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    linked INTEGER;
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'model_registry'
                  AND column_name = 'training_run_id') THEN
        SELECT count(*) INTO linked
          FROM model_registry WHERE training_run_id IS NOT NULL;

        IF linked > 0 THEN
            RAISE EXCEPTION
                '072-down REFUSING: % registry row(s) name the training run '
                'that produced them. Dropping training_run_id deletes the only '
                'link between a served artefact and the run behind it, and '
                'leaves adapter_version as a string with nothing under it.',
                linked
            USING HINT =
                'Retire those versions deliberately first: UPDATE '
                'model_registry SET status = ''retired'', training_run_id = '
                'NULL, adapter_version = NULL, model_card_sha256 = NULL WHERE '
                'training_run_id IS NOT NULL. Then re-run this rollback.';
        END IF;
    END IF;
END $$;

DROP TRIGGER IF EXISTS model_registry_promotion_gate ON model_registry;
DROP FUNCTION IF EXISTS model_registry_promotion_gate();

DROP INDEX IF EXISTS model_registry_training_run_idx;

ALTER TABLE model_registry
    DROP CONSTRAINT IF EXISTS an_ira_model_names_the_run_that_made_it,
    DROP CONSTRAINT IF EXISTS an_ira_model_carries_its_card,
    DROP CONSTRAINT IF EXISTS model_card_digest_is_a_sha256;

ALTER TABLE model_registry
    DROP COLUMN IF EXISTS training_run_id,
    DROP COLUMN IF EXISTS model_card_sha256;

COMMIT;
