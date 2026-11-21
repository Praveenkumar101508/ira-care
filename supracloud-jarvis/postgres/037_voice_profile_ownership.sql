-- =============================================================================
-- 037_voice_profile_ownership.sql — Phase 3B Batch 1B
--
-- Voiceprints move from an owner-name string to an immutable user_id, gain
-- versioned enrolment, consent, revocation, and the columns an encrypted
-- biometric template needs.
--
-- ── What is stored here ──────────────────────────────────────────────────────
--
-- A biometric template used for identification. Under GDPR that is a special
-- category of personal data. Today it is:
--
--   * keyed on `owner_name`, a mutable display string;
--   * UNIQUE on that string, so two tenants collide exactly as TOTP does;
--   * stored as TEXT, **unencrypted**;
--   * with no consent record, no version, no revocation and no RLS.
--
-- ── Versioned enrolment, and why it is not an overwrite ──────────────────────
--
-- Re-enrolment currently overwrites the template. That destroys the previous
-- one, so a compromised enrolment cannot be revoked — there is nothing left to
-- revoke, and nothing to audit against. `enrollment_version` plus `revoked_at`
-- makes re-enrolment additive: the old version is retained, marked revoked, and
-- refused at match time.
--
-- The partial unique index enforces **one ACTIVE profile per user**, which is
-- the property that matters. Any number of revoked versions may coexist.
--
-- ── Consent is referenced, not assumed ───────────────────────────────────────
--
-- `consent_granted_at` and `consent_reference` are NOT NULL-able only once the
-- consent surface exists (Phase 12). Recording them now means the migration
-- that adds the constraint has somewhere to put the data, and it means an
-- enrolment written today is distinguishable from one written with recorded
-- consent.
--
-- ── This migration does NOT encrypt ──────────────────────────────────────────
--
-- Same position as 036: SQL cannot reach the KMS/DEK path, so this expands and
-- the application worker encrypts. `migration_status` starts at `PLAINTEXT` and
-- says so. **No claim that voiceprints are encrypted is supportable until
-- `voice_migration_status.contract_ready` is true**, and the view reports the
-- number of rows that are not.
--
-- `embedding` — the plaintext template — is NOT dropped here.
--
-- ── Deletion is hard, never redaction ────────────────────────────────────────
--
-- `deletion_status` exists because a redacted biometric template is still a
-- biometric template with fields blanked. The target is verified hard deletion
-- plus crypto-shred of the key material, and the column records which of those
-- has happened rather than implying both.
--
-- Rollback: rollback/037_voice_profile_ownership_down.sql
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Expand
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS user_id UUID
    REFERENCES users(id) ON DELETE CASCADE;

-- Optional ACTIVE-tenant context. It records where the enrolment happened; it
-- does NOT replace user ownership, and it is deliberately nullable — a
-- voiceprint belongs to a person, not to a tenant, and a person may leave the
-- tenant they enrolled in without their biometric ownership changing.
ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS enrolled_in_tenant_id UUID
    REFERENCES tenants(id) ON DELETE SET NULL;

ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS enrollment_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS consent_granted_at TIMESTAMPTZ;
ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS consent_reference  TEXT;
ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS revoked_at         TIMESTAMPTZ;
ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS revocation_reason  TEXT;

ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS template_ciphertext    BYTEA;
ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS encryption_key_version INTEGER;
ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS encrypted_at           TIMESTAMPTZ;
ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS migration_status       TEXT NOT NULL DEFAULT 'PLAINTEXT';

ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS deletion_status TEXT NOT NULL DEFAULT 'live';
ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS deleted_at      TIMESTAMPTZ;

-- Raw audio is a SEPARATE artefact from the template and needs separate
-- consent. This column records whether any was retained; the default is the
-- honest answer for existing rows, which retained none.
ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS raw_audio_retained BOOLEAN NOT NULL DEFAULT FALSE;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'voice_profiles_migration_status_check') THEN
        ALTER TABLE voice_profiles ADD CONSTRAINT voice_profiles_migration_status_check
            CHECK (migration_status IN ('PLAINTEXT', 'ENCRYPTING', 'ENCRYPTED', 'FAILED'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'voice_profiles_encrypted_is_complete') THEN
        ALTER TABLE voice_profiles ADD CONSTRAINT voice_profiles_encrypted_is_complete
            CHECK (
                migration_status <> 'ENCRYPTED'
                OR (template_ciphertext IS NOT NULL
                    AND encryption_key_version IS NOT NULL
                    AND encrypted_at IS NOT NULL)
            );
    END IF;

    -- Deletion states are explicit and ordered. `redacted` is NOT among them:
    -- redaction is not a deletion mechanism for biometric data.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'voice_profiles_deletion_status_check') THEN
        ALTER TABLE voice_profiles ADD CONSTRAINT voice_profiles_deletion_status_check
            CHECK (deletion_status IN ('live', 'pending_deletion', 'hard_deleted', 'crypto_shredded'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'voice_profiles_revoked_has_reason') THEN
        ALTER TABLE voice_profiles ADD CONSTRAINT voice_profiles_revoked_has_reason
            CHECK (revoked_at IS NULL OR revocation_reason IS NOT NULL);
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Backfill ownership — strictly through the identity map
-- ─────────────────────────────────────────────────────────────────────────────

-- FORCE lifted for the backfill, restored in section 4. Same reasoning as 036:
-- without it a re-run silently matches zero rows and leaves a biometric
-- template with no owner. Full note in `035_memberships.sql` section 0.
ALTER TABLE voice_profiles NO FORCE ROW LEVEL SECURITY;

UPDATE voice_profiles v
   SET user_id = m.user_id
  FROM legacy_identity_map m
 WHERE m.legacy_username = v.owner_name
   AND m.resolution = 'MAPPED'
   AND v.user_id IS NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Constrain
--
--    One ACTIVE profile per user. Revoked and deleted versions are retained —
--    that is what makes revocation meaningful and re-enrolment auditable.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Retiring the owner_name unique constraint ────────────────────────────────
--
-- Same reasoning as 036's primary key. `UNIQUE (owner_name)` is GLOBAL, so two
-- tenants cannot both hold a user called `alice` — the second enrolment is
-- refused by a constraint on a display string. Adding `user_id` without
-- removing this leaves the collision exactly where it was.
--
-- `owner_name` keeps an ordinary index for compatibility reads, and no
-- uniqueness.
ALTER TABLE voice_profiles DROP CONSTRAINT IF EXISTS voice_profiles_owner_name_key;
DROP INDEX IF EXISTS voice_profiles_owner_name_key;
CREATE INDEX IF NOT EXISTS idx_voice_profiles_owner_name ON voice_profiles (owner_name);

CREATE UNIQUE INDEX IF NOT EXISTS voice_profiles_active_user_key
    ON voice_profiles (user_id)
    WHERE user_id IS NOT NULL AND revoked_at IS NULL AND deletion_status = 'live';

CREATE UNIQUE INDEX IF NOT EXISTS voice_profiles_user_version_key
    ON voice_profiles (user_id, enrollment_version) WHERE user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_voice_profiles_migration_status
    ON voice_profiles (migration_status) WHERE migration_status <> 'ENCRYPTED';

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Row-level security — USER_PRIVATE
--
--    Predicate on `app.user_id`. A tenant administrator has no route to a
--    member's biometric template, by construction rather than by policy
--    documentation.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE voice_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE voice_profiles FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_private ON voice_profiles;
CREATE POLICY user_private ON voice_profiles
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Verification view — counts only, never a template
-- ─────────────────────────────────────────────────────────────────────────────

-- **`security_invoker` is load-bearing, not a style choice.** A view defaults to
-- running as its OWNER, and the owner is subject to FORCE ROW LEVEL SECURITY
-- like anybody else. Without this setting the view evaluates with the migration
-- role's (unbound) context, matches zero rows, and reports
-- `contract_ready = true` on a table full of plaintext — to every caller,
-- including a superuser, because the caller's privileges never enter into it.
-- That was the state of this view as originally written; it was found by
-- reading it as a non-superuser owner.
CREATE OR REPLACE VIEW voice_migration_status
WITH (security_invoker = true) AS
SELECT
    count(*)                                                AS total_rows,
    count(*) FILTER (WHERE user_id IS NULL)                 AS unresolved_owner,
    count(*) FILTER (WHERE migration_status = 'PLAINTEXT')  AS still_plaintext,
    count(*) FILTER (WHERE migration_status = 'FAILED')     AS encryption_failed,
    count(*) FILTER (WHERE migration_status = 'ENCRYPTED')  AS encrypted,
    count(*) FILTER (WHERE consent_granted_at IS NULL)      AS missing_consent,
    (count(*) FILTER (WHERE user_id IS NULL) = 0
     AND count(*) FILTER (WHERE migration_status <> 'ENCRYPTED') = 0) AS contract_ready
FROM voice_profiles
WHERE deletion_status = 'live';

COMMENT ON VIEW voice_migration_status IS
    'Counts only — never a biometric template. Until contract_ready is true, '
    'this system MUST NOT claim voiceprints are encrypted at rest.';

COMMENT ON COLUMN voice_profiles.owner_name IS
    'LEGACY. A mutable display string, never a security principal. user_id is '
    'the owner. Dropped once contract_ready is true.';
COMMENT ON COLUMN voice_profiles.embedding IS
    'LEGACY PLAINTEXT biometric template. Replaced by template_ciphertext. NOT '
    'dropped here — no row is encrypted yet.';
COMMENT ON COLUMN voice_profiles.enrolled_in_tenant_id IS
    'Where the enrolment happened. Context only — it does not confer ownership, '
    'and it is nullable because a person outlives their membership.';
COMMENT ON COLUMN voice_profiles.deletion_status IS
    'Hard deletion or cryptographic deletion. Redaction is deliberately not a '
    'valid state: a redacted biometric template is still a biometric template.';
