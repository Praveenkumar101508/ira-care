-- =============================================================================
-- rollback/094_remembered_facts_into_vault_down.sql
--
-- Moves every fact `094` put in the Memory Vault back to the name it was filed
-- under before, and removes the marker. That is exactly the state before 094.
--
-- It needs no guard: nothing is deleted and nothing is created. A moved fact
-- the owner forgot after 094 is gone, and stays gone; this restores the place
-- of what is left, not what the owner chose to delete.
--
-- Only rows carrying `metadata.moved_to_vault` move. A fact saved to the vault
-- after #60, or any other vault entry, has no marker and stays.
--
-- Same RLS window as 094, for the same reason.
-- =============================================================================

BEGIN;

ALTER TABLE memory_embeddings NO FORCE ROW LEVEL SECURITY;

UPDATE memory_embeddings
   SET user_id  = metadata -> 'moved_to_vault' ->> 'from',
       metadata = metadata - 'moved_to_vault'
 WHERE user_id = 'owner'
   AND metadata ? 'moved_to_vault'
   AND coalesce(metadata -> 'moved_to_vault' ->> 'from', '') <> '';

ALTER TABLE memory_embeddings FORCE ROW LEVEL SECURITY;

COMMIT;
