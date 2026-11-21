-- 021_neurokernel.sql — owner-scoped cognitive events, goals and predictions.
-- All tables are additive. The NeuroKernel remains OFF by default and the
-- application only writes here when IRA_NEUROKERNEL_PERSISTENCE_ENABLED=true.

CREATE TABLE IF NOT EXISTS cognitive_events (
    event_id   TEXT PRIMARY KEY,
    at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    source     TEXT NOT NULL,
    summary    TEXT NOT NULL,
    payload    JSONB NOT NULL DEFAULT '{}',
    metrics    JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_cognitive_events_owner_at
    ON cognitive_events (user_id, at DESC);

CREATE TABLE IF NOT EXISTS cognitive_goals (
    goal_id          TEXT PRIMARY KEY,
    owner_id         TEXT NOT NULL,
    title            TEXT NOT NULL,
    desired_outcome  TEXT NOT NULL,
    priority         DOUBLE PRECISION NOT NULL CHECK (priority >= 0 AND priority <= 1),
    state            TEXT NOT NULL,
    deadline         TEXT,
    source           TEXT NOT NULL CHECK (source = 'owner'),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cognitive_goals_owner_state
    ON cognitive_goals (owner_id, state, priority DESC);

CREATE TABLE IF NOT EXISTS cognitive_predictions (
    prediction_id       TEXT PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    owner_id            TEXT NOT NULL,
    action              TEXT NOT NULL,
    expected_effects    JSONB NOT NULL DEFAULT '[]',
    success_probability DOUBLE PRECISION NOT NULL,
    risk                DOUBLE PRECISION NOT NULL,
    uncertainty         DOUBLE PRECISION NOT NULL,
    reversible          BOOLEAN NOT NULL,
    model_version       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cognitive_predictions_owner_at
    ON cognitive_predictions (owner_id, created_at DESC);

-- Events and predictions are evidence. Reuse the immutable trigger function
-- created by migration 018 so buggy code cannot rewrite cognitive history.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'cognitive_events_immutable') THEN
        CREATE TRIGGER cognitive_events_immutable
            BEFORE UPDATE OR DELETE ON cognitive_events
            FOR EACH ROW EXECUTE FUNCTION lifeos_events_no_mutate();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'cognitive_predictions_immutable') THEN
        CREATE TRIGGER cognitive_predictions_immutable
            BEFORE UPDATE OR DELETE ON cognitive_predictions
            FOR EACH ROW EXECUTE FUNCTION lifeos_events_no_mutate();
    END IF;
END $$;
