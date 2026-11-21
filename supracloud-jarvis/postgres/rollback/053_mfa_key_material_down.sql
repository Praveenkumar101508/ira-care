-- =============================================================================
-- rollback/053_mfa_key_material_down.sql
--
-- Reverses 053: drops `user_mfa_keys` and the seed's nonce column, and restores
-- 036's completeness check.
--
-- ── It REFUSES once anything is actually encrypted ──────────────────────────
--
-- Dropping `user_mfa_keys` destroys the only key material that can open an
-- encrypted TOTP seed. If any row is ENCRYPTED, this is not a rollback — it is
-- permanently locking every one of those users out of their own second factor,
-- with no recovery path, because the plaintext was cleared once the ciphertext
-- was verified.
--
-- So the guard is on ENCRYPTED rows rather than on key rows: a key with nothing
-- sealed under it is disposable, and a key with something sealed under it is
-- the only copy. Counted with FORCE lifted, because this script binds no GUCs
-- and would otherwise count zero under the very policies it is removing.
--
-- The legitimate use is unwinding an upgrade before the migration worker has
-- encrypted anything.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_encrypted bigint;
BEGIN
    IF to_regclass('public.totp_secrets') IS NOT NULL THEN
        EXECUTE 'ALTER TABLE totp_secrets NO FORCE ROW LEVEL SECURITY';
        SELECT count(*) INTO n_encrypted
          FROM totp_secrets WHERE migration_status = 'ENCRYPTED';

        IF n_encrypted > 0 THEN
            RAISE EXCEPTION
                '053-down REFUSING: % TOTP secret(s) are encrypted.', n_encrypted
                USING HINT =
                    'Dropping user_mfa_keys destroys the only material that can '
                    'open them, and their plaintext was cleared once the '
                    'ciphertext verified — so this would lock those users out '
                    'of their second factor permanently, not roll anything '
                    'back. Decrypt them first (scripts/phase2/mfa_migrate.py '
                    '--decrypt) and re-run.';
        END IF;
    END IF;
END
$$;

DROP TABLE IF EXISTS user_mfa_keys;

ALTER TABLE totp_secrets DROP CONSTRAINT IF EXISTS totp_secrets_has_a_secret;
ALTER TABLE totp_secrets DROP CONSTRAINT IF EXISTS totp_secrets_encrypted_is_complete;
ALTER TABLE totp_secrets DROP COLUMN IF EXISTS secret_nonce;

-- Every remaining row holds plaintext (the guard above proved none is
-- encrypted), so NOT NULL can be restored — 036's state exactly.
UPDATE totp_secrets SET secret = '' WHERE secret IS NULL;
ALTER TABLE totp_secrets ALTER COLUMN secret SET NOT NULL;

-- 036's check, restored verbatim.
ALTER TABLE totp_secrets ADD CONSTRAINT totp_secrets_encrypted_is_complete
    CHECK (
        migration_status <> 'ENCRYPTED'
        OR (secret_ciphertext IS NOT NULL
            AND encryption_key_version IS NOT NULL
            AND encrypted_at IS NOT NULL)
    );

-- 052 left the table forced; restore that rather than the state the guard
-- borrowed for counting.
ALTER TABLE totp_secrets ENABLE ROW LEVEL SECURITY;
ALTER TABLE totp_secrets FORCE  ROW LEVEL SECURITY;

DO $$
BEGIN
    IF to_regclass('public.user_mfa_keys') IS NOT NULL THEN
        RAISE EXCEPTION '053-down FAILED: user_mfa_keys still exists.';
    END IF;
    IF NOT (SELECT relforcerowsecurity FROM pg_class
             WHERE oid = 'public.totp_secrets'::regclass) THEN
        RAISE EXCEPTION '053-down FAILED: FORCE RLS was not restored on totp_secrets.';
    END IF;
END
$$;

COMMIT;
