-- =============================================================================
-- 026_privacy.sql — Phase 5: Privacy Centre and Cloud Memory Vault
--
-- Two jobs:
--
--   1. Make memory TRANSPARENT — every stored memory gains the fields a user
--      needs to judge it: where it came from, why it was kept, how sensitive it
--      is, when it expires, when it was last used, and who may see it.
--
--   2. Make erasure POSSIBLE — including in the append-only audit tables, which
--      is the decision Phases 2 and 4 both deferred to this one.
--
-- ── The audit-vs-erasure decision (ADR-0004) ─────────────────────────────────
--
-- The honest problem: `trust_ledger` and `permission_decisions` are append-only
-- because an audit record that can be edited is not evidence. But they hold
-- real content — `resource` and `purpose` are a file path, an email address,
-- "email Sarah about the divorce". Claiming the audit log holds no personal
-- data would be false, so "just keep everything" is not available.
--
-- The resolution is REDACTION, not deletion, and it is narrow:
--
--   * `trust_ledger` gains `body_digest`. New records hash prev_hash ++ the
--     body's digest (chain v2) instead of the body itself, so the body can be
--     dropped later and every link still verifies. Existing records keep chain
--     v1 and verify exactly as they always did — no history is rewritten.
--     A v1 record therefore CANNOT be erased without making the chain
--     unverifiable from that point, and the code refuses to pretend otherwise
--     (see trust/ledger.py and tests/privacy/test_ledger_redaction.py).
--
--   * `permission_decisions` gains `redacted_at`. Its trigger is relaxed to
--     permit EXACTLY ONE transition — a redaction that blanks the content
--     columns and stamps `redacted_at`. Every other UPDATE and every DELETE is
--     still refused.
--
-- What survives a redaction is deliberate: that an action of a given kind
-- happened, when, and that nothing was inserted, removed or reordered around
-- it. What goes is what the action was *about*.
--
-- ── What this migration does NOT claim ───────────────────────────────────────
--
-- Nothing here deletes anything from backups. The brief is explicit that
-- immediate backup deletion must not be claimed unless implemented, and it is
-- not. `deletion_requests.backup_residue_until` records when the residue is
-- expected to age out under the retention schedule, so the Privacy Centre can
-- tell a user the truth rather than implying the data is gone everywhere.
--
-- Additive. Idempotent. Rollback: rollback/026_privacy_down.sql.
-- =============================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Transparent memory
--
-- Every column here answers a question a user might reasonably ask about a
-- memory IRA is holding about them. They are nullable/defaulted so existing
-- rows keep working — an old memory simply has less known about it, which is
-- the truth.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── What 017_temporal_memory.sql already owns ────────────────────────────────
--
-- That migration added `confidence`, `sensitivity` and `deleted_at` to this
-- table years before this phase, and this migration must not redefine them.
--
-- `sensitivity` is the collision worth spelling out, because it is the same
-- WORD for a different AXIS:
--
--     017 (existing):  a CONFIDENTIALITY level — public | shared | private | secret
--     Phase 5 (here):  a CATEGORY             — health | financial | family | …
--
-- Both are legitimate and both are wanted. "Private" and "health" are not
-- alternatives; a memory is one of each. So Phase 5 adds
-- `sensitivity_category` and leaves `sensitivity` completely alone.
--
-- (This was originally written as `ADD COLUMN IF NOT EXISTS sensitivity`, which
-- silently did nothing on a real database while adding a second, contradictory
-- CHECK constraint. It passed locally only because the 026 rollback had dropped
-- 017's column first — a second bug, since fixed. CI on a clean migration chain
-- caught both. `test_026_does_not_redefine_columns_an_earlier_migration_owns`
-- now makes this class of mistake fail before it reaches a database.)
--
-- `confidence` is likewise 017's: REAL, default 1.0, constrained 0..1 by
-- `memory_confidence_range`. Phase 5 reads it and does not redeclare it.
-- `deleted_at` is 017's too, with the same meaning Phase 5 wants.

-- Where it came from. `source_type` already exists (init.sql); this adds the
-- pointer back to the specific conversation/message/document.
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS source_reference TEXT;

-- WHY it was kept. Free text written by the storing code, shown to the user.
-- A memory nobody can explain is one the user cannot meaningfully judge.
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS storage_reason TEXT NOT NULL DEFAULT '';

-- The CATEGORY the memory falls in — distinct from 017's confidentiality level
-- (see above). Drives the Constitution's `sensitivity_in` condition (Phase 4)
-- and the Privacy Centre's per-category switches.
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS sensitivity_category TEXT
    NOT NULL DEFAULT 'normal'
    CHECK (sensitivity_category IN
           ('normal', 'personal', 'health', 'financial', 'family', 'credential'));

-- When it should disappear on its own. NULL = no expiry, which is a choice the
-- user should be able to see and change rather than an invisible default.
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

-- When it was last actually used to answer something. A memory never used in a
-- year is one the user can delete without losing anything.
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;

-- Which connectors may see this memory. Empty array = none, which is the
-- fail-closed default: a memory is not shared with a connector unless the user
-- said so.
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS allowed_connectors TEXT[] NOT NULL DEFAULT '{}';

-- Whether this memory may be sent to an external model provider. Defaults to
-- 'never' — the strict end. Phase 8's model gateway reads it.
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS external_provider_policy TEXT NOT NULL DEFAULT 'never'
    CHECK (external_provider_policy IN ('never', 'anonymised', 'allowed'));

-- Soft delete. Already added by 017 with the same meaning; this ADD is a
-- deliberate no-op kept so the column's role in the Privacy Centre is visible
-- here rather than only in a migration from a different era.
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

-- Approval state. 'pending' exists because memory_mode='ask' (Phase 3's
-- default) means IRA proposes a memory and the user decides.
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS review_state TEXT NOT NULL DEFAULT 'accepted'
    CHECK (review_state IN ('pending', 'accepted', 'rejected'));

CREATE INDEX IF NOT EXISTS memory_live_idx
    ON memory_embeddings (user_id, created_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS memory_expiry_idx
    ON memory_embeddings (expires_at) WHERE expires_at IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS memory_review_idx
    ON memory_embeddings (user_id, review_state) WHERE review_state = 'pending';

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Retention settings — per user, tenant-scoped
--
-- Every default here is the privacy-preserving one. `memory_mode` defaults to
-- 'ask' to match Phase 3's companion default: memory that accumulates without
-- the user choosing it is the behaviour the Privacy Centre exists to undo.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS retention_settings (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id                UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id                  UUID REFERENCES users(id) ON DELETE CASCADE,

    memory_mode              TEXT NOT NULL DEFAULT 'ask'
                                 CHECK (memory_mode IN ('off', 'ask', 'automatic')),
    -- NULL = keep until deleted. A number = days.
    conversation_retention_days INTEGER CHECK (conversation_retention_days IS NULL
                                               OR conversation_retention_days > 0),
    memory_retention_days       INTEGER CHECK (memory_retention_days IS NULL
                                               OR memory_retention_days > 0),
    -- Raw voice audio. 0 means "do not retain after transcription", and it is
    -- the default: the brief lists exactly that as a rule users want.
    voice_retention_hours       INTEGER NOT NULL DEFAULT 0
                                 CHECK (voice_retention_hours >= 0),

    -- Categories the user has switched off entirely. A memory whose sensitivity
    -- is in here is never stored.
    disabled_categories      TEXT[] NOT NULL DEFAULT '{}',
    -- The blunt instrument, and it must exist: memory off entirely.
    memory_enabled           BOOLEAN NOT NULL DEFAULT TRUE,

    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, user_id)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Deletion and export requests
--
-- Requests rather than immediate operations, because erasure spans several
-- stores and some of them are not instantaneous. A row here is the record of
-- what was asked for, what was actually reached, and what was not — which is
-- what lets the Privacy Centre report honestly instead of showing a tick.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS deletion_requests (
    id                    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id               UUID REFERENCES users(id) ON DELETE SET NULL,

    scope                 TEXT NOT NULL
                              CHECK (scope IN ('memories', 'conversations', 'voice',
                                               'everything', 'account')),
    status                TEXT NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending', 'running', 'completed',
                                                'partial', 'failed')),
    requested_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at          TIMESTAMPTZ,

    -- Per-store outcome: {"postgres": {"deleted": 12}, "redis": {"error": ...}}.
    -- A store that could not be reached appears here as an error rather than
    -- being omitted, so "completed" cannot quietly mean "completed where it
    -- happened to work".
    store_results         JSONB NOT NULL DEFAULT '{}',

    -- How many audit records could NOT be redacted (chain v1). Surfaced to the
    -- user rather than hidden — see ADR-0004.
    unredactable_records  INTEGER NOT NULL DEFAULT 0,

    -- When backup residue is expected to age out. NOT a deletion; a date.
    -- Nothing in this build deletes from backups, and the Privacy Centre says
    -- so using this field rather than implying the data is gone everywhere.
    backup_residue_until  TIMESTAMPTZ,
    note                  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS export_requests (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id       UUID REFERENCES users(id) ON DELETE SET NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at  TIMESTAMPTZ,
    -- Object-storage key, never a public URL: an export is the user's entire
    -- history and must not be reachable by anyone who guesses a link.
    artifact_key  TEXT,
    byte_size     BIGINT,
    note          TEXT NOT NULL DEFAULT ''
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Privacy events — already created in 023, now actually used.
--    Add the index the Privacy Centre's timeline needs.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS privacy_events_recent_idx
    ON privacy_events (tenant_id, at DESC);

CREATE INDEX IF NOT EXISTS retention_settings_tenant_idx ON retention_settings (tenant_id);
CREATE INDEX IF NOT EXISTS deletion_requests_tenant_idx  ON deletion_requests (tenant_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS export_requests_tenant_idx    ON export_requests (tenant_id, requested_at DESC);

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Row-Level Security on the new customer-data tables
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t TEXT;
    scoped TEXT[] := ARRAY[
        'retention_settings', 'deletion_requests', 'export_requests'
    ];
BEGIN
    FOREACH t IN ARRAY scoped LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format($f$
            CREATE POLICY tenant_isolation ON %I
                USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        $f$, t);
    END LOOP;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. Chain v2 on the trust ledger
--
-- `body_digest` is NULL for every existing record, which is exactly how
-- verify_chain tells v1 from v2. It is NOT backfilled: computing it would
-- require re-canonicalising each body and would produce a value that looks like
-- a v2 record while the stored `hash` is still v1 — the chain would then fail
-- to verify. Old records stay v1 and verify the way they always have.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE trust_ledger ADD COLUMN IF NOT EXISTS body_digest TEXT;

-- ── A defect this phase found, fixed here ────────────────────────────────────
--
-- 023 added `tenant_id` to trust_ledger, backfilled it, and set it NOT NULL —
-- with no default. `trust/ledger.py:append_record` does not supply it, so
-- EVERY ledger write has failed since that migration with a not-null violation.
--
-- It went unnoticed because the callers are deliberately fail-soft: a
-- momentarily-unreachable database must not lose a legitimate action, so the
-- exception is caught, logged as "ledger unavailable", and the action proceeds.
-- Nothing broke. The audit trail was simply always empty — the same failure
-- shape as the invalid ledger kinds Phase 4 found.
--
-- The fix is a DEFAULT rather than threading a tenant through ~20 call sites in
-- this phase: IRA Private is single-tenant, and this is the value 023 backfilled
-- every existing row with, so the default reproduces exactly what those rows
-- already say. Cloud writes will pass an explicit tenant when Phase 7 wires
-- connectors through the gateway; `append_record` now accepts one.
--
-- The UUID is uuid5(IRA_UUID_NAMESPACE, "tenant:private") — the same value
-- 023 uses and core/identity/principal.py derives at runtime.
ALTER TABLE trust_ledger
    ALTER COLUMN tenant_id SET DEFAULT 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid;

-- The redaction-aware append-only guard.
--
-- trust_ledger previously shared `lifeos_events_no_mutate()` with lifeos_events.
-- It gets its own function here rather than relaxing the shared one, which
-- would silently make lifeos_events mutable too.
--
-- Exactly one UPDATE is permitted: replacing `record` with the redaction
-- tombstone on a chain-v2 row. Everything else — any other column change, any
-- un-redaction, any DELETE, and any redaction of a v1 row — is refused.
CREATE OR REPLACE FUNCTION trust_ledger_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'trust_ledger is append-only: DELETE is not permitted';
    END IF;

    -- A v1 row has no body_digest, so erasing its content would make the chain
    -- unverifiable from that point. Refused; the application reports the
    -- residue instead of pretending.
    IF OLD.body_digest IS NULL THEN
        RAISE EXCEPTION
            'trust_ledger seq=% is a chain-v1 record: its content is inside its '
            'hash, so it cannot be redacted without breaking the chain', OLD.seq;
    END IF;

    IF NEW.record IS DISTINCT FROM '{"__redacted__": true}'::jsonb THEN
        RAISE EXCEPTION
            'trust_ledger is append-only: the only permitted update is redacting '
            '`record` to the tombstone';
    END IF;

    IF NEW.seq        IS DISTINCT FROM OLD.seq
    OR NEW.user_id    IS DISTINCT FROM OLD.user_id
    OR NEW.action_id  IS DISTINCT FROM OLD.action_id
    OR NEW.task_id    IS DISTINCT FROM OLD.task_id
    OR NEW.kind       IS DISTINCT FROM OLD.kind
    OR NEW.prev_hash  IS DISTINCT FROM OLD.prev_hash
    OR NEW.hash       IS DISTINCT FROM OLD.hash
    OR NEW.body_digest IS DISTINCT FROM OLD.body_digest
    OR NEW.at         IS DISTINCT FROM OLD.at THEN
        RAISE EXCEPTION
            'trust_ledger redaction may only change `record`; every other column '
            'is part of the proof';
    END IF;

    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trust_ledger_immutable ON trust_ledger;
DROP TRIGGER IF EXISTS trust_ledger_redactable ON trust_ledger;
CREATE TRIGGER trust_ledger_redactable
    BEFORE UPDATE OR DELETE ON trust_ledger
    FOR EACH ROW EXECUTE FUNCTION trust_ledger_guard();

-- ─────────────────────────────────────────────────────────────────────────────
-- 7. Redaction on permission_decisions
--
-- Same shape, simpler: the content columns are blanked and `redacted_at` is
-- stamped. The decision facts — what was allowed, at what risk, when, and why
-- in the coded sense — survive, because those are what the log exists to prove
-- and they are not personal data.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE permission_decisions ADD COLUMN IF NOT EXISTS redacted_at TIMESTAMPTZ;

CREATE OR REPLACE FUNCTION permission_decisions_append_only()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'permission_decisions is append-only: DELETE is not permitted. The '
            'decision log is evidence of what IRA was allowed to do at a point '
            'in time.';
    END IF;

    -- The one permitted transition: an un-redacted row becoming redacted.
    IF OLD.redacted_at IS NOT NULL THEN
        RAISE EXCEPTION 'permission_decisions row % is already redacted', OLD.id;
    END IF;
    IF NEW.redacted_at IS NULL THEN
        RAISE EXCEPTION
            'permission_decisions is append-only: UPDATE is not permitted except '
            'to redact. The decision log is evidence of what IRA was allowed to '
            'do at a point in time.';
    END IF;

    IF NEW.resource <> '' OR NEW.purpose <> '' OR NEW.reason <> '' THEN
        RAISE EXCEPTION
            'a permission_decisions redaction must clear resource, purpose and '
            'reason — those are where the personal data is';
    END IF;

    IF NEW.effect IS DISTINCT FROM OLD.effect
    OR NEW.risk   IS DISTINCT FROM OLD.risk
    OR NEW.domain IS DISTINCT FROM OLD.domain
    OR NEW.action IS DISTINCT FROM OLD.action
    OR NEW.decided_at IS DISTINCT FROM OLD.decided_at THEN
        RAISE EXCEPTION
            'a redaction may only clear content columns; the decision itself is '
            'what the log exists to prove';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

COMMIT;
