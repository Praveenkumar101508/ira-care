-- =============================================================================
-- 077_ledger_chain_eras.sql — Phase 10, batch P10-04.
--
-- Records WHERE this deployment's Trust Ledger stopped being one global chain
-- and became one chain per tenant, so an anchor can be checked against the
-- chain model it was actually created for.
--
-- ── Why this is data and not a constant ────────────────────────────────────
--
-- P9-02 bound `append_record` to a tenant connection. The head read is
-- RLS-filtered, so from that commit `prev_hash` links WITHIN a tenant and `seq`
-- is globally gapped. Everything written before it belongs to a single global
-- chain; everything after belongs to one of many.
--
-- Nothing in the table says which. There is no column for it and adding one
-- would mean writing to rows an append-only trigger exists to protect. But the
-- boundary is exactly determinable at the moment this migration runs: the
-- highest `seq` that exists NOW is the last row of the global era.
--
-- A Python constant could not carry that. Every install cut over at a different
-- `seq`, at a different time, and a constant changes with a deploy — so the one
-- fact an auditor needs would be a property of the code they are running rather
-- than of the database they are auditing.
--
-- ── IRREVERSIBLE-ONCE-USED: the boundary cannot be recomputed ─────────────
--
-- `rollback/077` runs cleanly while the boundary is unused, and REFUSES the
-- moment any ledger row sits above it. That is not a rollback that forgot to
-- be written — it is the only honest answer:
--
--   drop the table  →  the boundary is gone
--   re-apply 077    →  it stamps MAX(seq) again, which is now LATER
--   result          →  tenant-era records classified as legacy, and
--                      LEGACY_GLOBAL_ANCHOR returned for anchors that are not
--
-- No ordering of the CI cycle avoids that, because the shared database has
-- rows above the boundary by the time the cycle runs. So this migration is
-- exempt from the peel/re-apply cycle and proves both halves — clean drop when
-- unused, refusal when used — on its own disposable database instead.
--
-- `scripts/migration_workflow.py` reads the marker above and still requires a
-- rollback file that refuses. A migration cannot skip the cycle by saying
-- nothing; it has to say why, here, where the reviewer already is.
--
-- ── Exempt from the peel is NOT exempt from going back on ─────────────────
--
-- This migration is still RE-APPLIED inside the cycle, after 042. That is not
-- a contradiction and it is not decoration: 042 grants
-- `SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public` to `ira_app`,
-- so re-applying it hands back the UPDATE this migration revokes. The first
-- version of this batch left 077 out of the cycle entirely and CI went red on
-- exactly that — `ira_app` finished the cycle able to move the boundary that
-- decides which chain model its own records belong to.
--
-- Re-applying is safe because everything below is written to be restated
-- rather than recomputed: both inserts are ON CONFLICT DO NOTHING, and the
-- assertion that no ledger row sits above the boundary is scoped to the apply
-- that stamped it. `test.yml` compares the boundary before and after rather
-- than trusting that.

-- ── What this migration deliberately does NOT do ───────────────────────────
--
-- It does not touch a single ledger row. No hash is recomputed, no `tenant_id`
-- is reinterpreted, no historical anchor is made to "fit" the new model.
--
-- The owner's instruction is the whole design here:
--
--     "An anchor may only verify the chain model it was originally created
--      for."
--
-- So a pre-cutover anchor stays a legacy global anchor forever. It is not
-- verified against a tenant chain, and it is not called invalid either — it
-- anchors something real that this deployment used to be. `trust/ledger.py`
-- answers `LEGACY_GLOBAL_ANCHOR / NOT_VALID_FOR_TENANT_CHAIN` rather than
-- either.
--
-- ── The two rows ───────────────────────────────────────────────────────────
--
--   era 1  global   [1 .. cutover_seq]        closed at apply time
--   era 2  tenant   [cutover_seq+1 .. ∞)      open
--
-- A fresh database gets `cutover_seq = 0`, so era 1 is empty and every record
-- it will ever hold is era 2. That is correct and is why the boundary is stored
-- rather than inferred from "is there any data".
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'trust_ledger') THEN
        RAISE EXCEPTION
            '077 REFUSING: trust_ledger does not exist. 020_trust_ledger.sql '
            'has not been applied, so there is no chain to record a cutover '
            'for and applying this would record a boundary in an empty world '
            'as though it meant something.';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS trust_ledger_chain_eras (
    era              SMALLINT PRIMARY KEY,
    scope            TEXT        NOT NULL,
    -- The last `seq` belonging to this era. NULL means the era is still open.
    ends_at_seq      BIGINT,
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note             TEXT        NOT NULL,

    CONSTRAINT chain_era_scope_check
        CHECK (scope IN ('legacy_global', 'tenant')),
    -- Exactly one era may be open, and it is the newest. Enforced as a shape
    -- rather than a convention: two open eras would make "which era is this
    -- seq in" ambiguous, and an anchor decision cannot be made on an ambiguous
    -- answer.
    CONSTRAINT chain_era_closed_unless_current
        CHECK ((ends_at_seq IS NULL) = (era = 2))
);

COMMENT ON TABLE trust_ledger_chain_eras IS
    'Where this deployment''s Trust Ledger changed chain model. Era 1 is one '
    'global chain; era 2 (P9-02 onward) is one chain per tenant, keyed on '
    'app.tenant_id under FORCE RLS. Written once, at migration time, and never '
    'updated: an anchor may only verify the chain model it was created for, so '
    'moving this boundary afterwards would change what an already-issued '
    'anchor claims.';

COMMENT ON COLUMN trust_ledger_chain_eras.ends_at_seq IS
    'Last seq in this era; NULL while the era is open. The boundary is the '
    'highest seq that existed when 077 was applied — exact, per-deployment, '
    'and not knowable from any code constant.';

-- ── Stamp the boundary, once ───────────────────────────────────────────────
--
-- ON CONFLICT DO NOTHING, not DO UPDATE. Re-running this migration must not
-- move a boundary that anchors have already been issued against. A migration
-- that is idempotent by overwriting is not idempotent about anything that
-- matters.
INSERT INTO trust_ledger_chain_eras (era, scope, ends_at_seq, note)
SELECT 1, 'legacy_global', COALESCE(MAX(seq), 0),
       'One global hash chain. Anchors taken during this era anchor the whole '
       'deployment and are NOT valid for any per-tenant chain.'
  FROM trust_ledger
ON CONFLICT (era) DO NOTHING;

INSERT INTO trust_ledger_chain_eras (era, scope, ends_at_seq, note)
VALUES (2, 'tenant', NULL,
        'One hash chain per tenant (P9-02). prev_hash links within a tenant; '
        'seq is globally allocated and therefore gapped per tenant. An anchor '
        'from this era names the tenant it anchors.')
ON CONFLICT (era) DO NOTHING;

-- ── Who may read it, and who may not write it ──────────────────────────────
--
-- The runtime reads this on every anchor decision and must never be able to
-- move the boundary — the same reasoning as 076: the process that answers
-- requests must not be able to edit the record of what it answered.
GRANT SELECT ON trust_ledger_chain_eras TO ira_app;
GRANT SELECT ON trust_ledger_chain_eras TO ira_worker;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON trust_ledger_chain_eras FROM ira_app;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON trust_ledger_chain_eras FROM ira_worker;

-- ── Prove the stamp took, and that it is unambiguous ───────────────────────
DO $$
DECLARE
    eras        INTEGER;
    open_now    INTEGER;
    boundary    BIGINT;
    stray       BIGINT;
    stamped_now BOOLEAN;
BEGIN
    SELECT count(*) INTO eras FROM trust_ledger_chain_eras;
    IF eras <> 2 THEN
        RAISE EXCEPTION
            '077 FAILED: % chain era row(s) recorded, expected exactly 2.', eras;
    END IF;

    SELECT count(*) INTO open_now
      FROM trust_ledger_chain_eras WHERE ends_at_seq IS NULL;
    IF open_now <> 1 THEN
        RAISE EXCEPTION
            '077 FAILED: % open era(s). Exactly one era may be open, or '
            '"which era is this seq in" has no answer.', open_now;
    END IF;

    -- `recorded_at` defaults to NOW(), which is transaction start — so it
    -- equals now() exactly when the row above was inserted by THIS
    -- transaction. That distinguishes the apply that stamped the boundary from
    -- a re-apply, and the two have opposite expectations.
    SELECT ends_at_seq, recorded_at = now() INTO boundary, stamped_now
      FROM trust_ledger_chain_eras WHERE era = 1;

    -- On the apply that STAMPED it, the boundary must actually bound
    -- something: no row may exist above it, because it was just read as
    -- MAX(seq). This cannot be violated inside one transaction, and it is
    -- asserted anyway because the cost is one count and the failure it would
    -- catch — a boundary recorded from a stale read — is silent and permanent.
    --
    -- On a RE-APPLY the same count is expected to be large and means the
    -- opposite: those are era-2 rows, which is what the boundary exists to
    -- separate. Asserting it unconditionally turns every re-apply into a
    -- failure, and re-applying is required — the cycle in `test.yml` puts 077
    -- back on so that 042's blanket GRANT does not leave `ira_app` able to move
    -- this boundary. CI found that by going red, not by argument.
    IF stamped_now THEN
        SELECT count(*) INTO stray FROM trust_ledger WHERE seq > boundary;
        IF stray <> 0 THEN
            RAISE EXCEPTION
                '077 FAILED: % ledger row(s) already sit above the recorded '
                'cutover seq %. The boundary would mark existing global-era '
                'records as tenant-era.', stray, boundary;
        END IF;
    END IF;

    IF has_table_privilege('ira_app', 'trust_ledger_chain_eras', 'UPDATE') THEN
        RAISE EXCEPTION
            '077 FAILED: ira_app can move the chain-era boundary. The process '
            'that writes the ledger must not be able to redefine which chain '
            'model its records belong to.';
    END IF;
END $$;

COMMIT;
