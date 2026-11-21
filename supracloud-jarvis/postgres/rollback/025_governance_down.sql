-- =============================================================================
-- rollback/025_governance_down.sql — rollback for ../025_governance.sql
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
-- Reverses Phase 4's governance schema. Run ONLY, and MANUALLY.
--
-- ⚠️  READ BEFORE RUNNING — THIS ONE REMOVES SAFETY RAILS
--
-- Rolling this back does not merely drop tables. It removes:
--
--   * every user's Constitution — the permanent rules they wrote to constrain
--     their assistant;
--   * every permission grant, including the record of what they revoked;
--   * the append-only log of what IRA was permitted to do and why.
--
-- After this runs, `core/governance` finds no policies and no rules. It does
-- NOT therefore start allowing things: the broker fails closed, so an IRA with
-- this migration rolled back refuses side-effecting actions rather than
-- performing them unchecked. That is the intended failure direction, but it
-- does mean a rolled-back deployment is a largely non-functional one.
--
-- This script REFUSES to run when more than one tenant holds constitution
-- rules or grants — the same guard 023 and 024 use. On a multi-tenant
-- deployment there is no rollback that is safe for everyone.
--
-- What it does NOT do, deliberately:
--   * It does not touch `approval_requests` — 023 created that table and 023's
--     rollback owns it.
--   * It does not touch `trust_ledger`, which is append-only and is the other
--     half of the audit record.
--
-- To re-apply afterwards, run 025_governance.sql again — it is idempotent and
-- re-seeds the tool policies. User constitutions are NOT recoverable by
-- re-applying; export them first if they matter.
-- =============================================================================

BEGIN;

-- ── Guard: refuse on a deployment that has served more than one tenant ───────
DO $$
DECLARE
    tenant_count INTEGER;
BEGIN
    SELECT COUNT(DISTINCT tenant_id) INTO tenant_count
    FROM (
        SELECT tenant_id FROM constitution_rules
        UNION
        SELECT tenant_id FROM permission_grants
    ) AS scoped;

    IF tenant_count > 1 THEN
        RAISE EXCEPTION
            'Refusing to roll back: % tenants hold constitution rules or '
            'permission grants. Dropping these tables destroys the permanent '
            'rules those users wrote to constrain their assistant, and the '
            'record of what they granted and revoked. Export per tenant first.',
            tenant_count;
    END IF;
END
$$;

-- ── Drop the append-only trigger before the table ────────────────────────────
-- The trigger refuses DELETE, so it has to go first or the DROP TABLE below
-- would be the only way to remove the rows and the intent would be unclear.
DROP TRIGGER IF EXISTS permission_decisions_no_update ON permission_decisions;
DROP FUNCTION IF EXISTS permission_decisions_append_only();

-- ── Drop in dependency order ─────────────────────────────────────────────────
-- permission_decisions references constitution_rules and permission_grants, so
-- it goes first.
DROP TABLE IF EXISTS permission_decisions;
DROP TABLE IF EXISTS approval_events;
DROP TABLE IF EXISTS permission_grants;
DROP TABLE IF EXISTS constitution_rules;

-- ── Drop the global policy baseline ──────────────────────────────────────────
-- Holds no customer data — re-created wholesale by re-applying 025.
DROP TABLE IF EXISTS tool_policies;

COMMIT;
