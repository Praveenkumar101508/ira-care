-- =============================================================================
-- rollback/077_ledger_chain_eras_down.sql
--
-- Drops the chain-era record.
--
-- ── Read this before running it ────────────────────────────────────────────
--
-- Reversing this does not restore a previous state — there was no previous
-- state, 077 only wrote down a fact. What it destroys is the ONLY record of
-- where this deployment's ledger stopped being one global chain.
--
-- That boundary is `MAX(seq)` at the moment 077 first ran. Re-applying 077
-- afterwards will stamp a DIFFERENT and WRONG boundary, because rows written
-- in between will be counted into the legacy era. Anchors already issued
-- against era 2 would then be evaluated against era 1, and the answer would be
-- `LEGACY_GLOBAL_ANCHOR` for anchors that are nothing of the kind.
--
-- So this refuses once the boundary has been used — i.e. once any ledger row
-- exists above it. Reversing before that is harmless and is permitted, which
-- is the case CI's rollback cycle exercises on a freshly built schema.
--
-- There is no way to make this reversible in the general case, and pretending
-- otherwise by re-deriving the boundary is exactly the "silently reinterpret
-- it" the owner's decision forbids.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    boundary BIGINT;
    newer    BIGINT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class
                    WHERE relname = 'trust_ledger_chain_eras') THEN
        RETURN;
    END IF;

    SELECT ends_at_seq INTO boundary
      FROM trust_ledger_chain_eras WHERE era = 1;

    IF boundary IS NULL THEN
        RETURN;  -- nothing was ever stamped
    END IF;

    SELECT count(*) INTO newer FROM trust_ledger WHERE seq > boundary;

    IF newer > 0 THEN
        RAISE EXCEPTION
            '077-down REFUSING: % ledger row(s) have been written since the '
            'cutover was recorded at seq %. Dropping this table destroys the '
            'only record of that boundary, and re-applying 077 would stamp a '
            'later one — which would classify those % row(s) as legacy-global '
            'and answer LEGACY_GLOBAL_ANCHOR for tenant anchors that are not.',
            newer, boundary, newer
        USING HINT =
            'Record the current era rows somewhere outside this database '
            'first: SELECT * FROM trust_ledger_chain_eras. Then re-insert them '
            'by hand after re-applying 077, so the boundary is restored rather '
            'than recomputed.';
    END IF;
END $$;

DROP TABLE IF EXISTS trust_ledger_chain_eras;

COMMIT;
