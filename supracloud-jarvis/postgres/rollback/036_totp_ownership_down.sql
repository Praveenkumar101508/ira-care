-- Rollback for 036_totp_ownership.sql (Phase 3B Batch 1A).
--
-- Same rollback window as 037: safe only while every row is still PLAINTEXT.
-- Once a secret has been encrypted, `secret` is stale and dropping
-- `secret_ciphertext` destroys the only valid copy — which for a second factor
-- means locking every migrated user out of their own account.

DO $$
DECLARE encrypted_count INTEGER;
BEGIN
    SELECT count(*) INTO encrypted_count
      FROM totp_secrets
     WHERE migration_status IN ('ENCRYPTED', 'ENCRYPTING');
    IF encrypted_count > 0 THEN
        RAISE EXCEPTION
            'Refusing rollback: % TOTP secret(s) are encrypted. Dropping '
            'secret_ciphertext would lock those users out.', encrypted_count
        USING HINT = 'Decrypt back to `secret` first, or roll forward instead.';
    END IF;
END
$$;

DROP VIEW IF EXISTS totp_migration_status;

DROP POLICY IF EXISTS tenant_isolation ON tenant_mfa_policy;
DROP TRIGGER IF EXISTS trg_tenant_mfa_policy_updated_at ON tenant_mfa_policy;
DROP TABLE IF EXISTS tenant_mfa_policy;

DROP POLICY IF EXISTS user_private ON totp_secrets;
ALTER TABLE totp_secrets NO FORCE ROW LEVEL SECURITY;
ALTER TABLE totp_secrets DISABLE ROW LEVEL SECURITY;

DROP INDEX IF EXISTS totp_secrets_user_key;
DROP INDEX IF EXISTS idx_totp_secrets_migration_status;

ALTER TABLE totp_secrets DROP CONSTRAINT IF EXISTS totp_secrets_migration_status_check;
ALTER TABLE totp_secrets DROP CONSTRAINT IF EXISTS totp_secrets_encrypted_is_complete;

ALTER TABLE totp_secrets DROP COLUMN IF EXISTS revoked_at;
-- `id` is dropped after the primary key is moved back to username, below.
ALTER TABLE totp_secrets DROP COLUMN IF EXISTS id;
ALTER TABLE totp_secrets DROP COLUMN IF EXISTS migration_status;
ALTER TABLE totp_secrets DROP COLUMN IF EXISTS encrypted_at;
ALTER TABLE totp_secrets DROP COLUMN IF EXISTS encryption_key_version;
ALTER TABLE totp_secrets DROP COLUMN IF EXISTS secret_ciphertext;
-- Restore the username primary key. This RE-INTRODUCES the cross-tenant
-- collision (R-26) — which is what rolling this migration back means, and why
-- rolling back rather than forward should be a deliberate, temporary choice.
DROP INDEX IF EXISTS idx_totp_secrets_username;
ALTER TABLE totp_secrets DROP COLUMN IF EXISTS user_id;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'totp_secrets' AND c.contype = 'p'
    ) THEN
        ALTER TABLE totp_secrets ADD CONSTRAINT totp_secrets_pkey PRIMARY KEY (username);
    END IF;
END
$$;
