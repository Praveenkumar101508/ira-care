-- =============================================================================
-- rollback/024_companion_catalogue_down.sql — rollback for ../024_companion_catalogue.sql
--
-- ⚠️  THIS FILE LIVES IN rollback/ ON PURPOSE. DO NOT MOVE IT UP A LEVEL.
--
-- docker-compose.portable.yml mounts the whole postgres/ directory as
-- /docker-entrypoint-initdb.d, and PostgreSQL executes EVERY .sql it finds
-- there in alphabetical order on first start. A rollback script sitting beside
-- the migrations runs immediately after the migration it reverses, on every
-- fresh install. That is not hypothetical — it happened to 023's down script.
-- tests/tenancy/test_migration_hygiene.py keeps this directory free of them.
--
-- Reverses Phase 3's companion catalogue. Run ONLY, and MANUALLY.
--
-- ⚠️  READ BEFORE RUNNING
--
-- `companion_preferences` and `companion_versions` are CUSTOMER DATA. Dropping
-- them destroys every user's companion settings and the entire history of what
-- their assistant was configured to be. This script REFUSES to run when more
-- than one tenant holds companion data — the same guard 023's rollback uses,
-- and for the same reason: on a multi-tenant deployment there is no rollback
-- that is safe for everyone, so the operator must export per tenant first.
--
-- What it does NOT do, deliberately:
--   * It does not drop `companions` or any column 023 created — 024 only added
--     to that table, and 023's own rollback owns it.
--   * It does not drop the columns it added to `companions` (status,
--     activated_at, version). They are additive, harmless when unused, and
--     dropping `status` would destroy the draft/active distinction for anyone
--     who re-applies 024 afterwards.
--   * It does not delete rows from `companions`.
--
-- To re-apply afterwards, run 024_companion_catalogue.sql again — it is
-- idempotent, and its seed data is re-inserted by the same ON CONFLICT clauses.
-- =============================================================================

BEGIN;

-- ── Guard: refuse on a deployment that has served more than one tenant ───────
DO $$
DECLARE
    tenant_count INTEGER;
BEGIN
    SELECT COUNT(DISTINCT tenant_id) INTO tenant_count
    FROM (
        SELECT tenant_id FROM companion_preferences
        UNION
        SELECT tenant_id FROM companion_versions
    ) AS scoped;

    IF tenant_count > 1 THEN
        RAISE EXCEPTION
            'Refusing to roll back: % tenants hold companion data. '
            'Dropping these tables would destroy every one of their companion '
            'settings and history. Export per tenant first, then re-run.',
            tenant_count;
    END IF;
END
$$;

-- ── Drop the customer-data tables (guarded above) ────────────────────────────
DROP TABLE IF EXISTS companion_versions;
DROP TABLE IF EXISTS companion_preferences;

-- ── Drop the global catalogues ───────────────────────────────────────────────
-- These hold no customer data — only which avatars/voices/personalities this
-- deployment offers. Re-created wholesale by re-applying 024.
DROP TABLE IF EXISTS avatar_catalogue;
DROP TABLE IF EXISTS voice_catalogue;
DROP TABLE IF EXISTS personality_presets;

-- ── Undo the constraints 024 put on `companions` ─────────────────────────────
-- The COLUMNS stay (see the header). Only the constraints that would reject a
-- pre-024 write are removed, so a rolled-back deployment can still write the
-- table the way 023 left it.
DROP INDEX IF EXISTS companions_one_active_per_user;

ALTER TABLE companions DROP CONSTRAINT IF EXISTS companions_autonomy_level_check;
ALTER TABLE companions DROP CONSTRAINT IF EXISTS companions_status_check;

COMMIT;
