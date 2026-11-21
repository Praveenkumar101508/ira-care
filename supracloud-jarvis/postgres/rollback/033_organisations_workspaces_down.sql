-- Rollback for 033_organisations_workspaces.sql (Phase 3B Batch 0A).
--
-- Refuses while more than one tenant holds an organisation or workspace, on the
-- same principle as 023-032: dropping these tables in a multi-tenant database
-- destroys the content boundary, and nothing can reconstruct which workspace a
-- row belonged to.
--

DO $$
DECLARE tenant_count INTEGER;
BEGIN
    SELECT count(DISTINCT tenant_id) INTO tenant_count FROM workspaces;
    IF tenant_count > 1 THEN
        RAISE EXCEPTION
            'Refusing rollback: % tenants hold workspaces. Dropping them would '
            'destroy the content boundary.', tenant_count
        USING HINT = 'Roll forward instead.';
    END IF;
END
$$;

-- `tenants` RLS is not touched by 033 (see its header — the classification says
-- PLATFORM_GLOBAL, protected by grants), so there is nothing to undo here.

DROP TRIGGER IF EXISTS trg_workspaces_updated_at ON workspaces;
DROP TRIGGER IF EXISTS trg_organisations_updated_at ON organisations;
DROP POLICY IF EXISTS tenant_isolation ON workspaces;
DROP POLICY IF EXISTS tenant_isolation ON organisations;

DROP TABLE IF EXISTS workspaces;
DROP TABLE IF EXISTS organisations;
