-- =============================================================================
-- rollback/068_model_licence_register_down.sql
--
-- Removes the licence register and the gate it feeds.
--
-- ── It REFUSES while a model is serving under an approval ──────────────────
--
-- Dropping this table does not open a hole in the obvious direction — `067`'s
-- `serving_is_licensed` survives, so a serving row still needs SOMETHING in
-- `commercial_licence`. It opens it in the quiet direction: that constraint is
-- satisfied by any string, which is the defect `068` exists to close. A rollback
-- run while models are serving therefore leaves them serving, with the record of
-- WHY they were allowed to gone and the free-text field left standing in its
-- place.
--
-- So the guard is on the state that would be silently degraded, and the HINT
-- says what to do instead. Retiring first is a decision an operator can make;
-- discarding the approvals underneath running models is not.
--
-- ── Order matters ──────────────────────────────────────────────────────────
--
-- The trigger comes off `model_registry` before the column does, and the column
-- before the table, because `licence_id` is ON DELETE RESTRICT and the trigger
-- reads a table that is about to stop existing. Dropping the table first leaves
-- a trigger that raises on every INSERT into `model_registry` — a rollback that
-- breaks the thing it was rolling back to.
-- =============================================================================

BEGIN;



DO $$
DECLARE
    serving INTEGER;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'model_licence_register')
       AND EXISTS (SELECT 1 FROM pg_attribute
                    WHERE attrelid = 'model_registry'::regclass
                      AND attname = 'licence_id'
                      AND NOT attisdropped) THEN

        SELECT count(*) INTO serving
          FROM model_registry
         WHERE status = 'serving' AND licence_id IS NOT NULL;

        IF serving > 0 THEN
            RAISE EXCEPTION
                '068-down REFUSING: % model version(s) are serving under a '
                'recorded licence approval. Dropping the register removes the '
                'only record of who approved them and on what evidence, while '
                'leaving them serving.', serving
            USING HINT =
                'Retire them first, deliberately: UPDATE model_registry SET '
                'status = ''retired'', updated_at = NOW() WHERE status = '
                '''serving''. Then re-run this rollback.';
        END IF;
    END IF;
END $$;

-- ── Later migrations first (added by Phase 7, P7-05) ───────────────────────
--
-- `071` gave `model_training_runs.base_licence_id` a foreign key to this
-- table, so `DROP TABLE model_licence_register` now fails while `071` is
-- applied — with PostgreSQL's own "other objects depend on it", arriving after
-- the serving guard below has already passed. Accurate, and it reads as a
-- broken rollback rather than one run in the wrong order.
--
-- **On editing a merged migration's rollback.** The immutability rule is about
-- files that have been APPLIED to a database: a rollback script is an operator
-- tool, is not applied state, and is not tracked by
-- `MIGRATION_FILE_HASHES.json`. There is also no forward migration that can
-- repair a rollback script. The alternative was shipping one that cannot run,
-- so this is a guard added rather than behaviour changed: every case `068-down`
-- reversed before, it still reverses.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'model_training_runs') THEN
        RAISE EXCEPTION
            '068-down OUT OF ORDER: 071 is still applied. '
            'model_training_runs.base_licence_id references '
            'model_licence_register, so this file cannot drop it.'
        USING HINT =
            'Peel newest first: postgres/rollback/072_model_promotion_gate_'
            'down.sql, then 071_model_training_runs_down.sql, then this one. '
            'Each of those refuses while it holds a decision somebody made.';
    END IF;
END $$;

DROP TRIGGER IF EXISTS model_registry_licence_gate ON model_registry;
DROP FUNCTION IF EXISTS model_registry_requires_approved_licence();

ALTER TABLE model_registry DROP COLUMN IF EXISTS licence_id;

DROP TRIGGER IF EXISTS model_licence_register_withdrawal_gate
    ON model_licence_register;
DROP FUNCTION IF EXISTS model_licence_register_withdrawal();

DROP TABLE IF EXISTS model_licence_register;

COMMIT;
