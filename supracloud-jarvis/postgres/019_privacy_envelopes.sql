-- 019_privacy_envelopes.sql — PR 72: privacy envelopes (brief §7).
--
-- Every memory/event/commitment can belong to an envelope. A NULL envelope_id
-- means OWNER-PRIVATE-BY-DEFAULT: only the row's own user_id can see it —
-- legacy rows therefore become private automatically, never public.
--
-- ADDITIVE ONLY. ROLLBACK (documented, not automated):
--   ALTER TABLE memory_embeddings DROP COLUMN IF EXISTS envelope_id;
--   ALTER TABLE commitments       DROP COLUMN IF EXISTS envelope_id;
--   ALTER TABLE lifeos_events     DROP COLUMN IF EXISTS envelope_id;
--   DROP TABLE IF EXISTS envelope_members;
--   DROP TABLE IF EXISTS privacy_envelopes;

CREATE TABLE IF NOT EXISTS privacy_envelopes (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    kind        TEXT NOT NULL CHECK (kind IN
                ('owner_private', 'person_shared', 'household', 'project', 'public')),
    name        TEXT NOT NULL,
    owner_id    TEXT NOT NULL,             -- who controls membership
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (owner_id, name)
);

CREATE TABLE IF NOT EXISTS envelope_members (
    envelope_id UUID NOT NULL REFERENCES privacy_envelopes(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL,
    granted_by  TEXT NOT NULL,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (envelope_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_envelope_members_user
    ON envelope_members (user_id);

ALTER TABLE memory_embeddings
    ADD COLUMN IF NOT EXISTS envelope_id UUID REFERENCES privacy_envelopes(id);
ALTER TABLE commitments
    ADD COLUMN IF NOT EXISTS envelope_id UUID REFERENCES privacy_envelopes(id);
ALTER TABLE lifeos_events
    ADD COLUMN IF NOT EXISTS envelope_id UUID REFERENCES privacy_envelopes(id);

CREATE INDEX IF NOT EXISTS idx_memory_envelope
    ON memory_embeddings (envelope_id) WHERE envelope_id IS NOT NULL;
