-- Rollback for 039_workspace_substrate.sql (Phase 3B Batch 2).
--
-- Removes the constraints, the trigger, the functions and the views. It does
-- NOT delete backfilled organisations or workspaces: they are containers other
-- rows may already reference, and a rollback that silently removes a tenant's
-- workspace is worse than the migration it is undoing. The 033 seed is likewise
-- untouched — 033's own rollback owns it.
--
-- The constraint trigger goes first: it is DEFERRABLE and would otherwise fire
-- at COMMIT against a table this script has already changed.

DROP TRIGGER  IF EXISTS workspaces_one_default_check ON workspaces;
DROP FUNCTION IF EXISTS workspaces_require_one_default();

DROP VIEW     IF EXISTS workspace_substrate_status;
DROP FUNCTION IF EXISTS default_workspace_id(UUID);
DROP FUNCTION IF EXISTS provision_tenant_substrate(UUID);

ALTER TABLE workspaces DROP CONSTRAINT IF EXISTS workspaces_archived_at_agrees;
ALTER TABLE workspaces DROP CONSTRAINT IF EXISTS workspaces_archived_is_not_default;
