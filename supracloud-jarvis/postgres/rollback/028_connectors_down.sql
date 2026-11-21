-- =============================================================================
-- rollback/028_connectors_down.sql — rollback for ../028_connectors.sql
--
-- ⚠️  THIS FILE LIVES IN rollback/ ON PURPOSE. DO NOT MOVE IT UP A LEVEL.
--     postgres/ is mounted as /docker-entrypoint-initdb.d and every .sql there
--     runs on a fresh install, in order. See tests/tenancy/test_migration_hygiene.py.
--
-- Reverses Phase 7's connector schema. Run ONLY, and MANUALLY.
--
-- ⚠️  READ BEFORE RUNNING — THIS ONE DESTROYS EVIDENCE
--
-- `action_receipts` is the record of what IRA did with each user's email,
-- calendar and documents. It is append-only precisely so it cannot be quietly
-- rewritten; dropping the table is the one way to remove it, and doing so is
-- indistinguishable afterwards from IRA never having touched anything.
--
-- So this script REFUSES while receipts exist for more than one tenant, and
-- warns loudly for a single tenant. Export first.
--
-- ── Credentials ──────────────────────────────────────────────────────────────
--
-- Dropping `connector_accounts` destroys the stored refresh tokens. That is not
-- a data-loss problem — it is a REVOCATION problem: the tokens are gone from
-- IRA but are still live at Google until revoked there. Rolling this back does
-- NOT revoke anything.
--
-- Disconnect every account through the application first
-- (`POST /api/v1/connectors/{connector}/disconnect`), which calls the
-- provider's revocation endpoint. Then roll back. A dropped table full of
-- unrevoked grants leaves the user believing IRA no longer has access when the
-- grant is still valid.
--
-- This script cannot check that for you — it has no way to ask Google — so it
-- warns and proceeds. That is stated rather than silently assumed.
-- =============================================================================

BEGIN;

-- ── Guard: refuse while more than one tenant has receipts ────────────────────
DO $$
DECLARE
    tenant_count  INTEGER;
    receipt_count INTEGER;
BEGIN
    SELECT COUNT(DISTINCT tenant_id), COUNT(*)
      INTO tenant_count, receipt_count
      FROM action_receipts;

    IF tenant_count > 1 THEN
        RAISE EXCEPTION
            'Refusing to roll back: % tenants hold % Action Receipt(s). These '
            'are the evidence of what IRA did with each user''s email, calendar '
            'and documents; dropping them is indistinguishable from IRA never '
            'having touched anything. Export per tenant first.',
            tenant_count, receipt_count;
    END IF;

    IF receipt_count > 0 THEN
        RAISE WARNING
            'Dropping % Action Receipt(s) for a single tenant. The record of '
            'what IRA read and sent will be gone.', receipt_count;
    END IF;
END
$$;

-- ── Guard: warn about unrevoked credentials ──────────────────────────────────
DO $$
DECLARE
    live_credentials INTEGER;
BEGIN
    SELECT COUNT(*) INTO live_credentials
      FROM connector_accounts
     WHERE credential_ciphertext IS NOT NULL AND revoked_at IS NULL;

    IF live_credentials > 0 THEN
        RAISE WARNING
            '% connector account(s) still hold a credential that has NOT been '
            'revoked at the provider. Dropping this table removes IRA''s copy '
            'but leaves the grant live at Google. Disconnect through the '
            'application first so the provider''s revocation endpoint is '
            'called, or revoke manually at '
            'https://myaccount.google.com/permissions.',
            live_credentials;
    END IF;
END
$$;

-- ── Drop in dependency order ─────────────────────────────────────────────────

DROP TRIGGER IF EXISTS action_receipts_no_rewrite ON action_receipts;
DROP TRIGGER IF EXISTS oauth_states_single_use ON connector_oauth_states;
DROP FUNCTION IF EXISTS action_receipts_append_only();
DROP FUNCTION IF EXISTS oauth_state_single_use();

DROP INDEX IF EXISTS action_receipts_recent_idx;
DROP INDEX IF EXISTS oauth_states_expiry_idx;
DROP INDEX IF EXISTS connector_accounts_live_idx;
DROP INDEX IF EXISTS connector_accounts_tenant_idx;

DROP TABLE IF EXISTS action_receipts;
DROP TABLE IF EXISTS connector_oauth_states;
DROP TABLE IF EXISTS connector_accounts;
DROP TABLE IF EXISTS connector_scopes;

-- Dropped last: the CHECK constraints that used it went with the tables above.
DROP FUNCTION IF EXISTS credential_looks_like_plaintext(BYTEA);

COMMIT;
