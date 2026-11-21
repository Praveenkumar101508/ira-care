-- =============================================================================
-- 041_biometric_claim_telemetry.sql — Phase 3B, Batch 1 follow-up
--
-- Restores the security telemetry that `038`'s attribution constraint gave up,
-- in the form the owner specified rather than by reversing that constraint.
--
-- ── What 038 traded away, and why ────────────────────────────────────────────
--
-- `038` added `CHECK (user_id IS NULL OR tenant_id IS NOT NULL)` because a row
-- naming a person with no tenant cannot be redacted: PostgreSQL requires an
-- UPDATE's resulting row to satisfy the SELECT policy, and a row with both
-- attribution columns cleared satisfies no arm of it. So the row naming the
-- person would outlive their deletion request.
--
-- The cost, recorded as **R-49**: a biometric match that fails BEFORE a tenant
-- is resolved could no longer record who was being claimed. That is exactly the
-- event an investigation into credential stuffing needs, and losing it was the
-- least-bad option available at the time — not a good one.
--
-- ── The resolution ───────────────────────────────────────────────────────────
--
-- The detail an investigation needs is "the same identity was claimed 400 times
-- in an hour", which is a **correlation** question. Correlation needs a stable
-- token, not a name. A salted hash is a stable token that is not a directly
-- identifying reference:
--
--   * it supports counting, grouping and rate analysis;
--   * it is not a user reference, so no erasure request has to reach it;
--   * it cannot be read back into a username without the pepper, which lives
--     outside the database.
--
-- So a pre-tenant failure records `claim_hash`, the session and device context
-- it already had, and now a `failure_reason`. It still records **no** user_id
-- and **no** tenant_id, so `038`'s constraint is untouched and the erasure
-- guarantee is unchanged.
--
-- ── What must never be stored here ───────────────────────────────────────────
--
-- Not the raw claimed username. Not a voiceprint, embedding, template or
-- sample. Not raw audio. `038` already excludes the biometric material; the
-- CHECK below makes the username exclusion structural rather than a rule
-- somebody has to remember:
--
--     claim_hash ~ '^[a-f0-9]{64}$'
--
-- A raw username cannot satisfy that pattern, so "we stored the username in the
-- hash column" fails at write time instead of being discovered in a review.
-- The hash is computed by the authentication path, which is application work;
-- this migration provides the column and the shape it must have.
--
-- ── Salting ──────────────────────────────────────────────────────────────────
--
-- The application must hash with a deployment-wide pepper held in the secret
-- store, NOT with a value in this database. An unsalted hash of a username is
-- reversible by dictionary attack in seconds, which would make this column a
-- username column with extra steps. `key_version` records which pepper was used
-- so rotating it does not silently break correlation across the rotation.
--
-- Rollback: rollback/041_biometric_claim_telemetry_down.sql
-- =============================================================================

ALTER TABLE biometric_audit NO FORCE ROW LEVEL SECURITY;

ALTER TABLE biometric_audit ADD COLUMN IF NOT EXISTS claim_hash          TEXT;
ALTER TABLE biometric_audit ADD COLUMN IF NOT EXISTS claim_hash_version  INTEGER;
ALTER TABLE biometric_audit ADD COLUMN IF NOT EXISTS failure_reason      TEXT;
ALTER TABLE biometric_audit ADD COLUMN IF NOT EXISTS device_fingerprint  TEXT;

DO $$
BEGIN
    -- Structural refusal of a raw username in the hash column.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'biometric_audit_claim_hash_is_a_hash') THEN
        ALTER TABLE biometric_audit ADD CONSTRAINT biometric_audit_claim_hash_is_a_hash
            CHECK (claim_hash IS NULL OR claim_hash ~ '^[a-f0-9]{64}$');
    END IF;

    -- A hash without its pepper version cannot be compared after a rotation,
    -- so it is correlation data that silently stops correlating.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'biometric_audit_claim_hash_has_version') THEN
        ALTER TABLE biometric_audit ADD CONSTRAINT biometric_audit_claim_hash_has_version
            CHECK ((claim_hash IS NULL) = (claim_hash_version IS NULL));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'biometric_audit_failure_reason_check') THEN
        ALTER TABLE biometric_audit ADD CONSTRAINT biometric_audit_failure_reason_check
            CHECK (failure_reason IS NULL OR failure_reason IN (
                'below_threshold',
                'no_enrolment',
                'revoked_enrolment',
                'unknown_claim',
                'rate_limited',
                'malformed_sample'));
    END IF;

    -- A successful match has no failure reason. Without this the column drifts
    -- into a free-text note field, and a note field beside an audit row is
    -- where somebody eventually writes the username.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'biometric_audit_failure_reason_only_on_failure') THEN
        ALTER TABLE biometric_audit ADD CONSTRAINT biometric_audit_failure_reason_only_on_failure
            CHECK (result IS NOT TRUE OR failure_reason IS NULL);
    END IF;
END
$$;

-- The correlation index: "how many attempts claimed this identity recently".
CREATE INDEX IF NOT EXISTS idx_biometric_audit_claim
    ON biometric_audit (claim_hash, created_at DESC) WHERE claim_hash IS NOT NULL;

-- ── The append-only trigger must know about the new columns ──────────────────
--
-- `038`'s trigger lists the evidential columns explicitly and refuses any UPDATE
-- that changes one. Columns added later are NOT in that list, so without this
-- they would be quietly mutable on an append-only table — the failure being that
-- a new column looks protected and is not.
--
-- `claim_hash` is evidential: it is what makes a repeated claim countable, and a
-- rewrite would break the correlation the column exists to support. It is
-- deliberately NOT redactable either, because it is not a user reference —
-- there is nothing in it for an erasure request to reach.
CREATE OR REPLACE FUNCTION biometric_audit_refuse_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.tenant_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM tenants WHERE id = OLD.tenant_id) THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'biometric_audit is append-only: DELETE is refused'
            USING HINT = 'Audit evidence is retained and redacted, never deleted. '
                         'To remove attribution, clear user_id. The whole trail '
                         'goes only when the tenant itself is deleted.';
    END IF;

    IF NEW.id           IS DISTINCT FROM OLD.id
       OR NEW.session_id         IS DISTINCT FROM OLD.session_id
       OR NEW.similarity         IS DISTINCT FROM OLD.similarity
       OR NEW.threshold          IS DISTINCT FROM OLD.threshold
       OR NEW.result             IS DISTINCT FROM OLD.result
       OR NEW.source             IS DISTINCT FROM OLD.source
       OR NEW.created_at         IS DISTINCT FROM OLD.created_at
       OR NEW.event_type         IS DISTINCT FROM OLD.event_type
       OR NEW.claim_hash         IS DISTINCT FROM OLD.claim_hash
       OR NEW.claim_hash_version IS DISTINCT FROM OLD.claim_hash_version
       OR NEW.failure_reason     IS DISTINCT FROM OLD.failure_reason
    THEN
        RAISE EXCEPTION 'biometric_audit is append-only: evidential columns cannot change'
            USING HINT = 'Only redaction is permitted — clearing user_id, '
                         'tenant_id or workspace_id to NULL.';
    END IF;

    IF (NEW.user_id      IS NOT NULL AND NEW.user_id      IS DISTINCT FROM OLD.user_id)
       OR (NEW.tenant_id    IS NOT NULL AND NEW.tenant_id    IS DISTINCT FROM OLD.tenant_id)
       OR (NEW.workspace_id IS NOT NULL AND NEW.workspace_id IS DISTINCT FROM OLD.workspace_id)
    THEN
        RAISE EXCEPTION 'biometric_audit: attribution may be cleared, never reassigned'
            USING HINT = 'Set the column to NULL to redact. Pointing it at a '
                         'different subject would falsify the record.';
    END IF;

    RETURN NEW;
END
$$;

ALTER TABLE biometric_audit FORCE ROW LEVEL SECURITY;

COMMENT ON COLUMN biometric_audit.claim_hash IS
    'SHA-256 of the claimed identity, salted with a deployment pepper held '
    'OUTSIDE this database. Correlation token, not a user reference: it answers '
    '"the same identity was claimed N times" without naming anybody, so no '
    'erasure request has to reach it. Never the raw username — the CHECK on its '
    'format refuses one.';
COMMENT ON COLUMN biometric_audit.claim_hash_version IS
    'Which pepper produced claim_hash. Correlation across a pepper rotation is '
    'impossible without it, and a hash that silently stops correlating looks '
    'like an attack stopping.';
COMMENT ON COLUMN biometric_audit.failure_reason IS
    'Enumerated, never free text. A free-text note beside an audit row is where '
    'somebody eventually writes the username.';
