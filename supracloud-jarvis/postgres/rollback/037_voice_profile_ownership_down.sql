-- Rollback for 037_voice_profile_ownership.sql (Phase 3B Batch 1B).
--
-- ── Rollback window ──────────────────────────────────────────────────────────
--
-- This is safe ONLY while `migration_status = 'PLAINTEXT'` for every row —
-- i.e. before the encryption worker has run. Once a template has been
-- encrypted, `embedding` (the plaintext column) is stale and
-- `template_ciphertext` is the only real copy; dropping it would destroy the
-- biometric template.
--
-- The guard below refuses in that case rather than silently destroying data.

DO $$
DECLARE encrypted_count INTEGER;
BEGIN
    SELECT count(*) INTO encrypted_count
      FROM voice_profiles
     WHERE migration_status IN ('ENCRYPTED', 'ENCRYPTING');
    IF encrypted_count > 0 THEN
        RAISE EXCEPTION
            'Refusing rollback: % voice profile(s) hold encrypted templates. '
            'Dropping template_ciphertext would destroy the only copy.',
            encrypted_count
        USING HINT = 'Decrypt back to `embedding` first, or roll forward instead.';
    END IF;
END
$$;

DROP VIEW IF EXISTS voice_migration_status;

DROP POLICY IF EXISTS user_private ON voice_profiles;
ALTER TABLE voice_profiles NO FORCE ROW LEVEL SECURITY;
ALTER TABLE voice_profiles DISABLE ROW LEVEL SECURITY;

DROP INDEX IF EXISTS voice_profiles_active_user_key;
DROP INDEX IF EXISTS voice_profiles_user_version_key;
DROP INDEX IF EXISTS idx_voice_profiles_migration_status;

ALTER TABLE voice_profiles DROP CONSTRAINT IF EXISTS voice_profiles_migration_status_check;
ALTER TABLE voice_profiles DROP CONSTRAINT IF EXISTS voice_profiles_encrypted_is_complete;
ALTER TABLE voice_profiles DROP CONSTRAINT IF EXISTS voice_profiles_deletion_status_check;
ALTER TABLE voice_profiles DROP CONSTRAINT IF EXISTS voice_profiles_revoked_has_reason;

ALTER TABLE voice_profiles DROP COLUMN IF EXISTS raw_audio_retained;
ALTER TABLE voice_profiles DROP COLUMN IF EXISTS deleted_at;
ALTER TABLE voice_profiles DROP COLUMN IF EXISTS deletion_status;
ALTER TABLE voice_profiles DROP COLUMN IF EXISTS migration_status;
ALTER TABLE voice_profiles DROP COLUMN IF EXISTS encrypted_at;
ALTER TABLE voice_profiles DROP COLUMN IF EXISTS encryption_key_version;
ALTER TABLE voice_profiles DROP COLUMN IF EXISTS template_ciphertext;
ALTER TABLE voice_profiles DROP COLUMN IF EXISTS revocation_reason;
ALTER TABLE voice_profiles DROP COLUMN IF EXISTS revoked_at;
ALTER TABLE voice_profiles DROP COLUMN IF EXISTS consent_reference;
ALTER TABLE voice_profiles DROP COLUMN IF EXISTS consent_granted_at;
ALTER TABLE voice_profiles DROP COLUMN IF EXISTS enrollment_version;
ALTER TABLE voice_profiles DROP COLUMN IF EXISTS enrolled_in_tenant_id;
ALTER TABLE voice_profiles DROP COLUMN IF EXISTS user_id;

-- Restore the global owner_name uniqueness, re-introducing the collision.
DROP INDEX IF EXISTS idx_voice_profiles_owner_name;
ALTER TABLE voice_profiles ADD CONSTRAINT voice_profiles_owner_name_key UNIQUE (owner_name);
