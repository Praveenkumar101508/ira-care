-- =============================================================================
-- 088_technical_library_fulltext_down.sql — reverse of 088
--
-- Drops the partial full-text index on Technical Library rows.
--
-- Loses SPEED, never a record and never a result: the library's keyword query
-- still returns exactly the same rows, found by sequential scan instead. No
-- other path reads this index, so nothing else changes.
-- =============================================================================

BEGIN;

DROP INDEX IF EXISTS idx_memory_embeddings_techlib_fts;

COMMIT;
