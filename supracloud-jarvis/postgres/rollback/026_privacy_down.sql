-- =============================================================================
-- rollback/026_privacy_down.sql — rollback for ../026_privacy.sql
--
-- ⚠️  THIS FILE LIVES IN rollback/ ON PURPOSE. DO NOT MOVE IT UP A LEVEL.
--     postgres/ is mounted as /docker-entrypoint-initdb.d and every .sql there
--     runs on a fresh install, in order. See tests/tenancy/test_migration_hygiene.py.
--
-- Reverses Phase 5's privacy schema. Run ONLY, and MANUALLY.
--
-- ⚠️  READ BEFORE RUNNING — THIS ONE REMOVES A USER'S CONTROLS
--
-- Rolling this back drops:
--   * every user's retention settings, including "memory off" and
--     "do not retain voice audio" — settings whose whole purpose is to
--     constrain what IRA keeps;
--   * the record of which deletions they requested and what was actually
--     reached;
--   * the memory metadata that made each stored memory explicable.
--
-- The memory ROWS survive; what is lost is the ability to say where each one
-- came from, why it was kept, or when it should expire. A rolled-back
-- deployment therefore holds memories it can no longer explain — which is the
-- state the Privacy Centre exists to prevent.
--
-- ── The one thing this deliberately does NOT undo ────────────────────────────
--
-- `trust_ledger.body_digest` is NOT dropped, and the redaction-aware trigger is
-- NOT reverted to the strict one.
--
-- Dropping the column would strand every chain-v2 record: their `hash` is
-- sha256(prev_hash ++ body_digest), so without the digest the chain cannot be
-- verified at all — and any record already redacted would become permanently
-- unverifiable. Reverting the trigger would additionally leave redacted rows in
-- place with no way to explain them.
--
-- Rolling back the schema must not destroy the audit trail's integrity. The
-- column and trigger are harmless when unused: an install with no v2 records
-- behaves exactly as it did before.
--
-- The same applies to `permission_decisions.redacted_at` and its trigger.
--
-- This script REFUSES to run while more than one tenant holds retention
-- settings or deletion requests.
-- =============================================================================

BEGIN;

-- ── Guard: refuse on a deployment that has served more than one tenant ───────
DO $$
DECLARE
    tenant_count INTEGER;
BEGIN
    SELECT COUNT(DISTINCT tenant_id) INTO tenant_count
    FROM (
        SELECT tenant_id FROM retention_settings
        UNION
        SELECT tenant_id FROM deletion_requests
    ) AS scoped;

    IF tenant_count > 1 THEN
        RAISE EXCEPTION
            'Refusing to roll back: % tenants hold retention settings or '
            'deletion requests. Dropping these destroys the controls those '
            'users set over what IRA keeps, and the record of the deletions '
            'they asked for. Export per tenant first.',
            tenant_count;
    END IF;
END
$$;

-- ── Guard: refuse while redacted audit records exist ─────────────────────────
-- A redacted row's content is already gone. Rolling back the schema around it
-- would leave a tombstone nobody can account for.
DO $$
DECLARE
    redacted_ledger INTEGER;
    redacted_decisions INTEGER;
BEGIN
    SELECT COUNT(*) INTO redacted_ledger
      FROM trust_ledger WHERE record = '{"__redacted__": true}'::jsonb;
    SELECT COUNT(*) INTO redacted_decisions
      FROM permission_decisions WHERE redacted_at IS NOT NULL;

    IF redacted_ledger > 0 OR redacted_decisions > 0 THEN
        RAISE EXCEPTION
            'Refusing to roll back: % ledger record(s) and % decision(s) have '
            'already been redacted on a user''s request. Their content is gone; '
            'rolling back would leave tombstones this schema can no longer '
            'explain.',
            redacted_ledger, redacted_decisions;
    END IF;
END
$$;

-- ── Drop the customer-data tables (guarded above) ────────────────────────────
DROP TABLE IF EXISTS export_requests;
DROP TABLE IF EXISTS deletion_requests;
DROP TABLE IF EXISTS retention_settings;

-- ── Drop the memory metadata columns ─────────────────────────────────────────
-- The memory rows themselves are left alone. Only the explanatory metadata goes.
-- ONLY the columns 026 actually added. `sensitivity`, `confidence` and
-- `deleted_at` belong to 017_temporal_memory.sql and dropping them here would
-- destroy a working feature from another era — which an earlier draft of this
-- file did, masking a schema collision until CI ran the chain clean.
ALTER TABLE memory_embeddings DROP COLUMN IF EXISTS source_reference;
ALTER TABLE memory_embeddings DROP COLUMN IF EXISTS storage_reason;
ALTER TABLE memory_embeddings DROP COLUMN IF EXISTS sensitivity_category;
ALTER TABLE memory_embeddings DROP COLUMN IF EXISTS expires_at;
ALTER TABLE memory_embeddings DROP COLUMN IF EXISTS last_used_at;
ALTER TABLE memory_embeddings DROP COLUMN IF EXISTS allowed_connectors;
ALTER TABLE memory_embeddings DROP COLUMN IF EXISTS external_provider_policy;
ALTER TABLE memory_embeddings DROP COLUMN IF EXISTS review_state;

DROP INDEX IF EXISTS memory_live_idx;
DROP INDEX IF EXISTS memory_expiry_idx;
DROP INDEX IF EXISTS memory_review_idx;
DROP INDEX IF EXISTS privacy_events_recent_idx;

-- trust_ledger.body_digest, permission_decisions.redacted_at and both triggers
-- are deliberately left in place — see the header.

COMMIT;
