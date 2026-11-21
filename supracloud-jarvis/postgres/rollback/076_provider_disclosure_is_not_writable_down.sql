-- =============================================================================
-- rollback/076_provider_disclosure_is_not_writable_down.sql
--
-- Restores `ira_app`'s and `ira_worker`'s write access to `model_providers`.
--
-- ── Read this before running it ────────────────────────────────────────────
--
-- Reversing this migration re-opens the defect it closed: the process that
-- answers requests regains the ability to edit the record of where it sent
-- them, including whether a provider retains prompts. Nothing else changes.
--
-- It is written anyway, and it restores exactly what `042`'s default privileges
-- had granted rather than something tidier, because a rollback that leaves a
-- different privilege set from the one it replaced is not a rollback. `067`'s
-- and `068`'s tables are untouched here: they were never granted this DML.
--
-- There is nothing to refuse over. Unlike `069-down`, `070-down` or `075-down`,
-- no row records a human decision that narrowing would destroy — this is a
-- grant, and the only cost of reversing it is the control itself.
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'model_providers') THEN
        RETURN;
    END IF;
END $$;

GRANT INSERT, UPDATE, DELETE ON model_providers TO ira_app;
GRANT INSERT, UPDATE, DELETE ON model_providers TO ira_worker;

-- TRUNCATE is deliberately NOT restored. `042` exists to withhold it — its own
-- comment records that a blanket `GRANT ALL` had widened privileges precisely
-- by including it — and re-granting it here to be symmetrical would undo a
-- different migration's control while reversing this one.

COMMENT ON TABLE model_providers IS
    'Phase 8 commercial catalogue. Platform-authored operator configuration. '
    'NOTE: ira_app and ira_worker can write this table — see 076, which revoked '
    'that and has been rolled back.';

COMMIT;
