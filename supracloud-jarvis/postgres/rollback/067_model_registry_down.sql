-- =============================================================================
-- rollback/067_model_registry_down.sql
--
-- Drops the model registry and the pull allowlist.
--
-- ── It REFUSES while anything is serving ───────────────────────────────────
--
-- A `serving` row is the record of which model version is answering requests
-- right now, and — through `rollback_target` — the only record of what to go
-- back to. Dropping the table mid-flight destroys both, and the symptom is not
-- an error: the gateway starts refusing every request because no model is
-- registered, which reads as an outage of the model fabric rather than as a
-- rollback somebody ran.
--
-- Unlike the audit guards, this is genuinely safe to clear, and the HINT says
-- how rather than leaving the operator to work it out: retire the serving rows
-- first. That is a decision an operator can make; silently discarding them is
-- not.
--
-- ── The allowlist is dropped without a guard, and that is deliberate ────────
--
-- Removing it cannot enable a fetch. The loader refuses when it finds no
-- allowlist entry, so an absent table is the closed direction — every pull
-- fails rather than every pull succeeding. Guarding it would protect nothing
-- and would make an emergency rollback harder for no gain.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    serving INTEGER;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'model_registry') THEN
        SELECT count(*) INTO serving
          FROM model_registry WHERE status = 'serving';

        IF serving > 0 THEN
            RAISE EXCEPTION
                '067-down REFUSING: % model version(s) are still serving. '
                'Dropping the registry destroys the record of what is running '
                'and of what to roll back to.', serving
            USING HINT =
                'Retire them first, deliberately: UPDATE model_registry SET '
                'status = ''retired'', updated_at = NOW() WHERE status = '
                '''serving''. The gateway then refuses rather than guessing, '
                'which is the intended behaviour with no registered model.';
        END IF;
    END IF;
END $$;

-- `rollback_target` is a self-reference, so the constraint goes with the table.
DROP TABLE IF EXISTS model_registry;
DROP TABLE IF EXISTS model_pull_allowlist;

COMMIT;
