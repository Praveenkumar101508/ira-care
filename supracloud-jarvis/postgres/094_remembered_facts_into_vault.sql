-- =============================================================================
-- 094_remembered_facts_into_vault.sql
--
-- Moves the facts the owner saved with "remember ..." in chat, before #60, into
-- the Memory Vault. Decided by the owner on 2026-10-19.
--
-- ── What was wrong ─────────────────────────────────────────────────────────
--
-- Until #60, the owner's "remember X" saved X at once as a typed fact filed
-- under the CHAT USERNAME (`memory_embeddings.user_id = 'admin'`, say). The
-- Memory Vault lists and forgets only its own namespace (`owner`), so each of
-- those facts was:
--
--   * recalled by chat, which searches the owner's names and the vault;
--   * not shown in the Memory Vault, and not removable with its "forget";
--   * removable only by "delete my memories", which deletes everything.
--
-- #60 files every new one in the vault, after the owner approves it. This moves
-- the ones saved before it, so that every remembered fact is where the owner
-- can see it and forget it.
--
-- ── Which rows ─────────────────────────────────────────────────────────────
--
-- A row moves when all four hold:
--
--   source_type = 'fact'       written only by `memory/temporal.save_memory`;
--   source = 'owner_chat'      the "remember" command and the owner's
--                              resolution route are its only callers that set
--                              this, in the whole history of the repository,
--                              and both are owner-only;
--   user_id <> 'owner'         not already in the vault;
--   a PRIVATE-edition tenant   the vault is the owner's own only there. In the
--                              cloud edition the vault is the workspace's,
--                              readable by every member holding
--                              `workspace.read`, so moving a fact there would
--                              widen who can read it. The cloud never ran the
--                              "remember" command (`is_owner` is False for every
--                              cloud user), so nothing is expected there; if a
--                              row exists anyway it stays where it is.
--
-- Whole replacement chains move together: a fact the owner replaced and the
-- fact that replaced it both match, so `superseded_by` never ends up pointing
-- across namespaces. Content and embedding are unchanged, so chat recall finds
-- the same facts as before. Tenant and workspace are unchanged, so RLS scope is
-- unchanged.
--
-- Each moved row records where it came from in
-- `metadata.moved_to_vault = {"from": <old user_id>, "by": "094"}`. That is
-- what makes the rollback exact rather than a guess.
--
-- ── RLS window ─────────────────────────────────────────────────────────────
--
-- Migrations run as ira_schema_owner, and FORCE RLS applies to the owner: with
-- no app.tenant_id bound, the UPDATE would match no rows and report success.
-- Forcing is lifted for this transaction only and restored at the end, as in
-- 049 and 093; a failure anywhere rolls the window shut with everything else.
-- `tenants` has no RLS, so the edition check reads it directly.
--
-- Idempotent: a second run finds nothing outside the vault to move.
--
-- Rollback: rollback/094_remembered_facts_into_vault_down.sql moves every row
-- carrying the marker back to the name it came from.
-- =============================================================================

BEGIN;

ALTER TABLE memory_embeddings NO FORCE ROW LEVEL SECURITY;

DO $$
DECLARE
    moved INTEGER;
BEGIN
    UPDATE memory_embeddings m
       SET user_id  = 'owner',
           metadata = coalesce(m.metadata, '{}'::jsonb)
                      || jsonb_build_object('moved_to_vault',
                             jsonb_build_object('from', m.user_id, 'by', '094'))
     WHERE m.source_type = 'fact'
       AND m.source = 'owner_chat'
       AND m.user_id <> 'owner'
       AND m.tenant_id IN (SELECT t.id FROM tenants t WHERE t.edition = 'private');
    GET DIAGNOSTICS moved = ROW_COUNT;
    RAISE NOTICE '094: moved % remembered fact(s) into the Memory Vault', moved;
END $$;

ALTER TABLE memory_embeddings FORCE ROW LEVEL SECURITY;

COMMIT;
