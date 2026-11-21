-- =============================================================================
-- rollback/031_pretenancy_isolation_down.sql — rollback for
-- ../031_pretenancy_isolation.sql
--
-- ⚠️  THIS FILE LIVES IN rollback/ ON PURPOSE. DO NOT MOVE IT UP A LEVEL.
--     postgres/ is mounted as /docker-entrypoint-initdb.d and every .sql there
--     runs on a fresh install, in order. See tests/tenancy/test_migration_hygiene.py.
--
-- Removes tenant isolation from the ten pre-tenancy tables. Run ONLY, and
-- MANUALLY.
--
-- ⚠️  READ BEFORE RUNNING — THIS REMOVES A SECURITY BOUNDARY
--
-- After this script, all ten — `files`, `commitments`, `lifeos_events`,
-- `envelope_members`, `privacy_envelopes` and the five cognitive-loop tables —
-- are protected by application-level `WHERE user_id = $1` filtering and nothing
-- else. That is the exact state `RELEASE_READINESS.md` R03 recorded as blocker
-- B1, except that R03 undercounted it at five.
--
-- In a single-owner Private-edition install that is the pre-existing situation
-- and rolling back is survivable. **In a multi-tenant deployment it is not**, so
-- this script refuses while more than one tenant holds rows in any of them.
--
-- Note what it does NOT do: it does not revert the application to
-- `acquire()`. `owner_connection` keeps working after this — binding a tenant
-- on a table without RLS is harmless — so the rollback is a database operation
-- and does not need a matching deploy.
-- =============================================================================

BEGIN;

-- ── Guard: never silently un-isolate a multi-tenant install ──────────────────
DO $$
DECLARE
    t        TEXT;
    tables   TEXT[] := ARRAY['cognitive_events','cognitive_cycles','cognitive_goals',
                             'cognitive_predictions','cognitive_prediction_outcomes',
                             'commitments','envelope_members','files',
                             'lifeos_events','privacy_envelopes'];
    tenants  INTEGER;
    offender TEXT;
BEGIN
    IF to_regclass('public.files') IS NULL THEN
        RAISE NOTICE 'the pre-tenancy tables do not exist; nothing to roll back.';
        RETURN;
    END IF;

    FOREACH t IN ARRAY tables LOOP
        -- The policy would hide rows from this very check, so it is momentarily
        -- stood down per table. Running as the owner, inside the transaction
        -- that is about to drop the policy anyway.
        EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('SELECT COUNT(DISTINCT tenant_id) FROM %I', t) INTO tenants;
        IF tenants > 1 THEN
            offender := t;
        END IF;
    END LOOP;

    IF offender IS NOT NULL THEN
        RAISE EXCEPTION
            'Refusing to roll back: %s holds rows for more than one tenant. '
            'Dropping tenant_id and the policy would merge them into one '
            'undifferentiated set with only application-level WHERE clauses '
            'between them — blocker B1, reintroduced, on a database that has '
            'real multi-tenant data in it. Export and separate the tenants '
            'first.', offender;
    END IF;
END
$$;

-- ── Policies and RLS ─────────────────────────────────────────────────────────
DO $$
DECLARE
    t TEXT;
    tables TEXT[] := ARRAY['cognitive_events','cognitive_cycles','cognitive_goals',
                           'cognitive_predictions','cognitive_prediction_outcomes',
                           'commitments','envelope_members','files',
                           'lifeos_events','privacy_envelopes'];
BEGIN
    FOREACH t IN ARRAY tables LOOP
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I DISABLE ROW LEVEL SECURITY', t);
    END LOOP;
END
$$;

DROP INDEX IF EXISTS idx_cognitive_events_tenant;
DROP INDEX IF EXISTS idx_cognitive_cycles_tenant;
DROP INDEX IF EXISTS idx_cognitive_goals_tenant;
DROP INDEX IF EXISTS idx_cognitive_predictions_tenant;
DROP INDEX IF EXISTS idx_cognitive_outcomes_tenant;
DROP INDEX IF EXISTS idx_commitments_tenant;
DROP INDEX IF EXISTS idx_envelope_members_tenant;
DROP INDEX IF EXISTS idx_files_tenant;
DROP INDEX IF EXISTS idx_lifeos_events_tenant;
DROP INDEX IF EXISTS idx_privacy_envelopes_tenant;

-- ── The columns ──────────────────────────────────────────────────────────────
-- DROP COLUMN, not "set nullable": leaving a half-populated tenant_id behind
-- would make the next re-apply's backfill skip exactly the rows written while
-- the rollback was in force.
ALTER TABLE cognitive_events  DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE cognitive_cycles  DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE cognitive_goals   DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE cognitive_predictions         DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE cognitive_prediction_outcomes DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE commitments       DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE envelope_members  DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE files             DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE lifeos_events     DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE privacy_envelopes DROP COLUMN IF EXISTS tenant_id;

COMMIT;
