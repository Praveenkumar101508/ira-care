-- =============================================================================
-- 088_technical_library_fulltext.sql — keyword search for the Technical Library
--
-- `memory_embeddings` has only ever been searched by vector distance. That is
-- the right tool for "explain how attention works" and the wrong one for an
-- exact technical term: "GROUP BY", "LoRA" or "HNSW" embed close to a dozen
-- neighbouring ideas, and the chunk that actually names the term can rank
-- below ones that merely discuss something similar. The Technical Library
-- (`memory/technical_library.py`) therefore also runs a PostgreSQL full-text
-- query and fuses the two rankings. This index is what makes that query cheap.
--
-- ── Why an expression index, and not a tsvector column ──────────────────────
--
-- Both a GENERATED STORED column and a trigger-maintained column were
-- considered and rejected:
--
--   * Adding a STORED generated column REWRITES the whole table under an
--     ACCESS EXCLUSIVE lock — every personal memory, every self-knowledge
--     chunk — to compute a value only the library reads.
--   * Either column makes EVERY insert pay for `to_tsvector`, including the
--     embedding write behind each chat message, which never needs it.
--
-- An index on the expression `to_tsvector('english', content)` stores the same
-- lexemes without a column, so there is no rewrite and nothing new in the row.
-- The query writes the identical expression and the planner matches it.
--
-- ── Why partial ─────────────────────────────────────────────────────────────
--
-- `WHERE user_id = 'technical_library'` limits the index to library rows:
--
--   * building it scans the table once but indexes only library rows, which
--     on an existing install is none — so the build is effectively instant;
--   * a personal-memory insert does not satisfy the predicate, so it pays
--     nothing — the "do not slow existing inserts" requirement, by
--     construction rather than by measurement;
--   * the retrieval query names that literal namespace, which is what lets the
--     planner prove the predicate and use the index. A `$n` parameter in its
--     place would not.
--
-- The cost is that the opt-in hybrid path for PERSONAL memory
-- (`memory_hybrid_retrieval`, default OFF) has no index behind its full-text
-- half and scans that user's rows. Personal memory is small; that trade is
-- recorded in docs/TECHNICAL_LIBRARY.md rather than hidden.
--
-- 'english' is a regconfig literal, which makes `to_tsvector` IMMUTABLE and
-- therefore indexable. It stems ("grouping" -> "group") and drops stopwords
-- ("BY"), so "GROUP BY" matches on "group".
--
-- ── Reversibility ───────────────────────────────────────────────────────────
--
-- Additive and reversible. No row, column or constraint changes. The down
-- migration drops the index and loses only speed: the library's keyword query
-- still returns the same rows, by sequential scan.
-- =============================================================================

BEGIN;

CREATE INDEX IF NOT EXISTS idx_memory_embeddings_techlib_fts
    ON memory_embeddings
    USING gin (to_tsvector('english', content))
    WHERE user_id = 'technical_library';

COMMIT;
