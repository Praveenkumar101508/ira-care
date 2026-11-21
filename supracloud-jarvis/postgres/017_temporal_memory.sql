-- 017_temporal_memory.sql — PR 71: temporal + provenance memory.
--
-- STRICTLY ADDITIVE: existing rows keep working (every new column is nullable
-- or defaulted), retrieval that ignores the new columns behaves exactly as
-- before. Forward-validated by tests/coding/test_temporal_memory.py (SQL
-- shape) and the migration runner's per-file transaction.
--
-- ROLLBACK (documented, not automated):
--   ALTER TABLE memory_embeddings
--     DROP CONSTRAINT IF EXISTS memory_confidence_range,
--     DROP CONSTRAINT IF EXISTS memory_sensitivity_values,
--     DROP CONSTRAINT IF EXISTS memory_type_values;
--   DROP INDEX IF EXISTS idx_memory_active;
--   DROP INDEX IF EXISTS idx_memory_superseded_by;
--   ALTER TABLE memory_embeddings
--     DROP COLUMN IF EXISTS memory_type,      DROP COLUMN IF EXISTS source,
--     DROP COLUMN IF EXISTS observed_at,      DROP COLUMN IF EXISTS valid_from,
--     DROP COLUMN IF EXISTS valid_until,      DROP COLUMN IF EXISTS confidence,
--     DROP COLUMN IF EXISTS sensitivity,      DROP COLUMN IF EXISTS owner_confirmed,
--     DROP COLUMN IF EXISTS superseded_by,    DROP COLUMN IF EXISTS deleted_at,
--     DROP COLUMN IF EXISTS embedding_version;

ALTER TABLE memory_embeddings
    ADD COLUMN IF NOT EXISTS memory_type       TEXT NOT NULL DEFAULT 'fact',
    ADD COLUMN IF NOT EXISTS source            TEXT,
    ADD COLUMN IF NOT EXISTS observed_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS valid_from        TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS valid_until       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS confidence        REAL NOT NULL DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS sensitivity       TEXT NOT NULL DEFAULT 'private',
    ADD COLUMN IF NOT EXISTS owner_confirmed   BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS superseded_by     UUID REFERENCES memory_embeddings(id),
    ADD COLUMN IF NOT EXISTS deleted_at        TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS embedding_version TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'memory_confidence_range') THEN
        ALTER TABLE memory_embeddings
            ADD CONSTRAINT memory_confidence_range CHECK (confidence >= 0 AND confidence <= 1);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'memory_sensitivity_values') THEN
        ALTER TABLE memory_embeddings
            ADD CONSTRAINT memory_sensitivity_values
            CHECK (sensitivity IN ('public', 'shared', 'private', 'secret'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'memory_type_values') THEN
        ALTER TABLE memory_embeddings
            ADD CONSTRAINT memory_type_values
            CHECK (memory_type IN (
                'fact', 'preference', 'temporary_state', 'goal', 'instruction',
                'hypothesis', 'historical_fact', 'superseded_fact', 'commitment',
                'decision', 'relationship', 'resource', 'event'
            ));
    END IF;
END $$;

-- Active-memory retrieval path: not deleted, not superseded.
CREATE INDEX IF NOT EXISTS idx_memory_active
    ON memory_embeddings (user_id)
    WHERE deleted_at IS NULL AND superseded_by IS NULL;

CREATE INDEX IF NOT EXISTS idx_memory_superseded_by
    ON memory_embeddings (superseded_by);
