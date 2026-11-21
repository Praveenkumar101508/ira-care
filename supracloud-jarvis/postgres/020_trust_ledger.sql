-- 020_trust_ledger.sql — PR 73: hash-chained Trust Ledger (brief §9).
--
-- Append-only at the DB level (same trigger pattern as lifeos_events).
-- The hash chain itself is computed and verified in ira/trust/ledger.py;
-- tamper evidence requires anchoring the head hash OUTSIDE this database
-- (documented in the module and the threat model, T7 — no stronger claim).
--
-- ADDITIVE ONLY. ROLLBACK:
--   DROP TRIGGER IF EXISTS trust_ledger_immutable ON trust_ledger;
--   DROP TABLE IF EXISTS trust_ledger;
--   (function lifeos_events_no_mutate is shared with 018 — keep it)

CREATE TABLE IF NOT EXISTS trust_ledger (
    seq        BIGSERIAL PRIMARY KEY,
    at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id    TEXT NOT NULL,
    action_id  TEXT NOT NULL,
    task_id    TEXT,
    kind       TEXT NOT NULL,          -- action | approval | execution |
                                       -- verification | rollback | security_event
    record     JSONB NOT NULL DEFAULT '{}',
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trust_ledger_user ON trust_ledger (user_id, seq);
CREATE INDEX IF NOT EXISTS idx_trust_ledger_action ON trust_ledger (action_id);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trust_ledger_immutable') THEN
        CREATE TRIGGER trust_ledger_immutable
            BEFORE UPDATE OR DELETE ON trust_ledger
            FOR EACH ROW EXECUTE FUNCTION lifeos_events_no_mutate();
    END IF;
END $$;
