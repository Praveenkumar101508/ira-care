-- =============================================================================
-- rollback/030_key_management_down.sql — rollback for ../030_key_management.sql
--
-- ⚠️  THIS FILE LIVES IN rollback/ ON PURPOSE. DO NOT MOVE IT UP A LEVEL.
--     postgres/ is mounted as /docker-entrypoint-initdb.d and every .sql there
--     runs on a fresh install, in order. See tests/tenancy/test_migration_hygiene.py.
--
-- Reverses the envelope-encryption key registry. Run ONLY, and MANUALLY.
--
-- ⚠️  READ BEFORE RUNNING — THIS DESTROYS KEYS, NOT JUST A TABLE
--
-- Every live connector credential in this deployment is sealed under a DEK that
-- exists in exactly one place: `tenant_data_keys.wrapped_dek`. Dropping the
-- table does not "roll back to the old scheme" — it destroys the keys, and every
-- credential sealed under them becomes permanently unopenable. Users would see
-- their connected accounts stop working with no way to recover them short of
-- reconnecting each one.
--
-- That is indistinguishable from crypto-shredding every tenant at once, except
-- that nobody asked for it and no tombstone records that it happened.
--
-- So this script REFUSES while any live key exists. To roll back deliberately:
--
--   1. Disconnect the connectors (which revokes at the provider first — see
--      core/connectors/store.disconnect), or
--   2. Destroy each tenant's keys through core/connectors/keyring.destroy_keys,
--      which leaves a tombstone and a reason, then run this.
--
-- Destroyed rows do not block the rollback: their material is already gone, so
-- dropping them removes a record, not a key. The guard prints how many
-- tombstones are being discarded, because that record is the answer to "why did
-- my Gmail stop working" and losing it silently is its own small harm.
-- =============================================================================

BEGIN;

-- ── Guard: live key material is never silently dropped ───────────────────────
DO $$
DECLARE
    live      INTEGER;
    tombstones INTEGER;
    sealed    INTEGER;
BEGIN
    IF to_regclass('public.tenant_data_keys') IS NULL THEN
        RAISE NOTICE 'tenant_data_keys does not exist; nothing to roll back.';
        RETURN;
    END IF;

    SELECT COUNT(*) INTO live       FROM tenant_data_keys WHERE destroyed_at IS NULL;
    SELECT COUNT(*) INTO tombstones FROM tenant_data_keys WHERE destroyed_at IS NOT NULL;

    IF live > 0 THEN
        SELECT COUNT(*) INTO sealed
        FROM connector_accounts
        WHERE encryption_key_id LIKE 'dek:%' AND credential_ciphertext IS NOT NULL;

        RAISE EXCEPTION
            'Refusing to roll back: % live data-encryption key(s) exist, and % '
            'stored credential(s) are sealed under them. Dropping this table '
            'destroys those keys — the credentials become permanently '
            'unopenable and every connected account stops working with no way '
            'back except reconnecting. Disconnect the accounts first (which '
            'revokes at the provider), or destroy the keys deliberately through '
            'core/connectors/keyring.destroy_keys so a tombstone and a reason '
            'survive.',
            live, sealed;
    END IF;

    IF tombstones > 0 THEN
        RAISE NOTICE
            'Discarding % destruction tombstone(s). Their key material is '
            'already gone, so no key is being destroyed here — but the record '
            'of WHY the affected credentials stopped opening goes with them.',
            tombstones;
    END IF;
END
$$;

DROP TRIGGER IF EXISTS tenant_data_keys_shred_only ON tenant_data_keys;
DROP FUNCTION IF EXISTS tenant_data_keys_shred_only();

DROP INDEX IF EXISTS idx_tenant_data_keys_live;
DROP INDEX IF EXISTS one_active_key_per_tenant_purpose;

DROP TABLE IF EXISTS tenant_data_keys;

COMMIT;
