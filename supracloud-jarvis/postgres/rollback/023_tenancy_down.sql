-- =============================================================================
-- rollback/023_tenancy_down.sql — rollback for ../023_tenancy.sql
--
-- ⚠️  THIS FILE LIVES IN rollback/ ON PURPOSE. DO NOT MOVE IT UP A LEVEL.
--
-- docker-compose.portable.yml mounts the whole postgres/ directory as
-- /docker-entrypoint-initdb.d, and PostgreSQL executes EVERY .sql it finds
-- there in alphabetical order on first start. A rollback script sitting beside
-- the migrations would therefore run immediately after 023_tenancy.sql on every
-- fresh install — silently dropping the row-level security that migration just
-- created, with nothing in the logs to suggest anything was wrong.
--
-- (That is not hypothetical: it happened. The file was originally placed beside
-- the migrations, the CI chain applied it, and the isolation tests failed.
-- tests/tenancy/test_migration_hygiene.py now asserts this directory stays free
-- of rollback scripts so it cannot recur.)
--
-- Reverses Phase 2's tenancy migration. Run ONLY, and MANUALLY, on a deployment
-- that is going back to single-owner IRA Private.
--
-- ⚠️  READ BEFORE RUNNING
--
-- This drops the isolation boundary. On a deployment that has served more than
-- one tenant, running it makes every tenant's rows visible to every query —
-- there is no "partial" rollback that keeps some isolation. If more than one
-- tenant has real data, EXPORT PER TENANT FIRST; this script will refuse to run
-- otherwise (see the guard below).
--
-- What it does NOT do, deliberately:
--   * It does not DROP the tenant_id columns. Dropping them destroys the only
--     record of which rows belonged to whom, making a re-migration impossible
--     and any later export wrong. They are left in place, nullable.
--   * It does not delete rows.
--   * It does not touch trust_ledger's contents (append-only by design).
--
-- To re-apply afterwards, run 023_tenancy.sql again — it is idempotent and its
-- backfill will re-populate any NULL tenant_id with the private tenant.
-- =============================================================================

BEGIN;

-- ── Guard: refuse to drop isolation while multiple tenants hold data ─────────
DO $$
DECLARE
    populated INT;
BEGIN
    SELECT count(DISTINCT tenant_id) INTO populated
    FROM (
        SELECT tenant_id FROM conversations
        UNION SELECT tenant_id FROM memory_embeddings
        UNION SELECT tenant_id FROM people
        UNION SELECT tenant_id FROM tasks
    ) AS t
    WHERE tenant_id IS NOT NULL;

    IF populated > 1 THEN
        RAISE EXCEPTION
            'Refusing to roll back tenancy: % distinct tenants hold data. '
            'Dropping row-level security here would expose every tenant to every '
            'query. Export per tenant first, reduce to one tenant, then re-run.',
            populated;
    END IF;
END
$$;

-- ── Drop the policies and disable RLS ───────────────────────────────────────
DO $$
DECLARE
    t TEXT;
    scoped TEXT[] := ARRAY[
        'conversations', 'messages', 'memory_embeddings', 'people', 'tasks',
        'trust_ledger', 'approval_requests', 'agent_runs', 'tool_calls',
        'privacy_events', 'tenant_memberships', 'user_devices', 'user_sessions',
        'companions'
    ];
BEGIN
    FOREACH t IN ARRAY scoped LOOP
        IF EXISTS (SELECT 1 FROM pg_class WHERE relname = t) THEN
            EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
            EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I DISABLE ROW LEVEL SECURITY', t);
        END IF;
    END LOOP;
END
$$;

-- ── Relax the NOT NULL constraints ──────────────────────────────────────────
-- The columns and their data stay. Only the requirement goes, so pre-Phase-2
-- code paths that never set tenant_id can insert again.
ALTER TABLE conversations     ALTER COLUMN tenant_id DROP NOT NULL;
ALTER TABLE messages          ALTER COLUMN tenant_id DROP NOT NULL;
ALTER TABLE memory_embeddings ALTER COLUMN tenant_id DROP NOT NULL;
ALTER TABLE people            ALTER COLUMN tenant_id DROP NOT NULL;
ALTER TABLE tasks             ALTER COLUMN tenant_id DROP NOT NULL;
ALTER TABLE trust_ledger      ALTER COLUMN tenant_id DROP NOT NULL;

-- Governance tables introduced by 023 are dropped: nothing before Phase 2 wrote
-- to them, so there is no data to preserve.
DROP TABLE IF EXISTS tool_calls;
DROP TABLE IF EXISTS agent_runs;
DROP TABLE IF EXISTS approval_requests;
DROP TABLE IF EXISTS privacy_events;
DROP TABLE IF EXISTS companions;
DROP TABLE IF EXISTS user_sessions;
DROP TABLE IF EXISTS user_devices;
DROP TABLE IF EXISTS tenant_memberships;

-- `users` and `tenants` are intentionally KEPT. `tenants` predates Phase 2
-- (008_business_data.sql) and the business tables still reference it; dropping
-- it would cascade into investor/client-agent data that has nothing to do with
-- this migration.

COMMIT;
