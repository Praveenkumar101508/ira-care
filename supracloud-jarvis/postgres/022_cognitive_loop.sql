-- 022_cognitive_loop.sql — immutable closed-loop planning and outcome evidence.
-- Additive only. The NeuroKernel and persistence flags remain OFF by default.

CREATE TABLE IF NOT EXISTS cognitive_cycles (
    cycle_id             TEXT PRIMARY KEY,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    owner_id             TEXT NOT NULL,
    summary              TEXT NOT NULL,
    percept_event_id     TEXT NOT NULL,
    alternatives         JSONB NOT NULL DEFAULT '[]',
    selected_proposal_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_cognitive_cycles_owner_at
    ON cognitive_cycles (owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS cognitive_prediction_outcomes (
    outcome_id        TEXT PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    prediction_id     TEXT NOT NULL UNIQUE REFERENCES cognitive_predictions(prediction_id),
    owner_id          TEXT NOT NULL,
    succeeded         BOOLEAN NOT NULL,
    observed_effects  JSONB NOT NULL DEFAULT '[]',
    safety_incident   BOOLEAN NOT NULL DEFAULT FALSE,
    evidence_event_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_cognitive_outcomes_owner_at
    ON cognitive_prediction_outcomes (owner_id, created_at DESC);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'cognitive_cycles_immutable') THEN
        CREATE TRIGGER cognitive_cycles_immutable
            BEFORE UPDATE OR DELETE ON cognitive_cycles
            FOR EACH ROW EXECUTE FUNCTION lifeos_events_no_mutate();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'cognitive_outcomes_immutable') THEN
        CREATE TRIGGER cognitive_outcomes_immutable
            BEFORE UPDATE OR DELETE ON cognitive_prediction_outcomes
            FOR EACH ROW EXECUTE FUNCTION lifeos_events_no_mutate();
    END IF;
END $$;
