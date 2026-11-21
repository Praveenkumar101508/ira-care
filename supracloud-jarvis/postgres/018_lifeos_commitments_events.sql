-- 018_lifeos_commitments_events.sql — PR 71 part 2: LifeOS commitment domain
-- + append-only event domain (brief §5.3, §5.6).
--
-- ADDITIVE ONLY: two new tables, no changes to existing ones.
--
-- ROLLBACK (documented, not automated):
--   DROP TRIGGER IF EXISTS lifeos_events_immutable ON lifeos_events;
--   DROP FUNCTION IF EXISTS lifeos_events_no_mutate();
--   DROP TABLE IF EXISTS lifeos_events;
--   DROP TABLE IF EXISTS commitments;

CREATE TABLE IF NOT EXISTS commitments (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         TEXT NOT NULL,
    statement       TEXT NOT NULL,
    person          TEXT,             -- who it involves (free text; people.id later)
    project         TEXT,
    deadline        TIMESTAMPTZ,
    state           TEXT NOT NULL DEFAULT 'open'
                    CHECK (state IN ('open', 'at_risk', 'done', 'missed', 'cancelled')),
    risk_note       TEXT,
    next_action     TEXT,
    reminder_policy TEXT NOT NULL DEFAULT 'default',
    outcome         TEXT,
    source          TEXT,             -- provenance: where IRA learned this
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_commitments_user_live
    ON commitments (user_id, deadline)
    WHERE state IN ('open', 'at_risk');

-- Append-only event log (seed of the Trust Ledger, PR 73). UPDATE and DELETE
-- are refused at the database level — auditability must not depend on
-- application-code discipline alone.
CREATE TABLE IF NOT EXISTS lifeos_events (
    seq      BIGSERIAL PRIMARY KEY,
    at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id  TEXT NOT NULL,
    kind     TEXT NOT NULL,           -- e.g. memory_saved, permission_changed,
                                      -- approval, action, error, security_event
    payload  JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_lifeos_events_user_at
    ON lifeos_events (user_id, at);

CREATE OR REPLACE FUNCTION lifeos_events_no_mutate() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'lifeos_events is append-only';
END $$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'lifeos_events_immutable') THEN
        CREATE TRIGGER lifeos_events_immutable
            BEFORE UPDATE OR DELETE ON lifeos_events
            FOR EACH ROW EXECUTE FUNCTION lifeos_events_no_mutate();
    END IF;
END $$;
