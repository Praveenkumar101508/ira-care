-- =============================================================================
-- rollback/062_voiceprint_encryption_down.sql — reverse 062
--
-- Refuses while any row holds a wrapped key.
--
-- ── Why that is the line ────────────────────────────────────────────────────
--
-- These columns are not metadata. `wrapped_dek` and `wrap_nonce` ARE the key
-- that opens a biometric template, and `template_nonce` is required to use it.
-- Dropping them destroys the only route to the plaintext — a crypto-shred of
-- every enrolment at once, performed by a schema rollback, with no record that
-- it was intended.
--
-- That is worse than the usual rollback hazard, and worse in a specific way: it
-- succeeds. The row survives, `template_ciphertext` survives, `migration_status`
-- still reads ENCRYPTED, and every count in `voice_migration_status` looks
-- exactly as it did. Nothing reports a loss until somebody tries to
-- authenticate and the template will not open — by which time the plaintext
-- column `063` dropped is gone too.
--
-- So the guard is on the FIRST wrapped key, not on some subset. There is no
-- harmless number of biometric enrolments to make permanently unopenable.
--
-- A row whose `deletion_status` is `crypto_shredded` is excluded, and only
-- that: its key is already destroyed deliberately, `062`'s
-- `voice_profiles_shredded_holds_no_key` guarantees it holds none, and the
-- tombstone is the point.
--
-- Counted with FORCE lifted, because this script binds no GUCs and
-- `voice_profiles` is USER_PRIVATE under FORCE RLS — an unbound connection
-- matches ZERO rows, so the guard would report an empty table on every
-- deployment in existence. That is the P2-05 shape, and here it would destroy
-- every key while reporting there were none.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_keys bigint;
BEGIN
    IF to_regclass('public.voice_profiles') IS NULL THEN
        RETURN;   -- nothing to reverse
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'voice_profiles'
                      AND column_name = 'wrapped_dek') THEN
        RETURN;   -- already reversed
    END IF;

    EXECUTE 'ALTER TABLE voice_profiles NO FORCE ROW LEVEL SECURITY';
    SELECT count(*) INTO n_keys FROM voice_profiles
     WHERE wrapped_dek IS NOT NULL
       AND deletion_status <> 'crypto_shredded';
    EXECUTE 'ALTER TABLE voice_profiles FORCE ROW LEVEL SECURITY';

    IF n_keys > 0 THEN
        RAISE EXCEPTION
            '062-down REFUSING: % voice enrolment(s) hold a wrapped key.', n_keys
            USING HINT =
                'Dropping these columns destroys the only route to those '
                'biometric templates — a crypto-shred of every enrolment at '
                'once, performed silently by a schema change. Nothing would '
                'report it: the rows survive, the ciphertext survives, and '
                'migration_status still reads ENCRYPTED. Decrypt back to '
                'plaintext first (scripts/phase4/encrypt_voiceprints.py '
                'decrypt), or shred the enrolments deliberately and record '
                'where that decision was taken, then re-run.';
    END IF;
END $$;

-- Restore `037`'s constraint before dropping the columns its replacement names,
-- rather than after. A table that is briefly under NEITHER constraint is a
-- window in which an ENCRYPTED row with no ciphertext is representable, and
-- rollbacks are exactly when somebody is writing to a database by hand.
ALTER TABLE voice_profiles
    DROP CONSTRAINT IF EXISTS voice_profiles_encrypted_is_complete;

ALTER TABLE voice_profiles ADD CONSTRAINT voice_profiles_encrypted_is_complete
    CHECK (
        migration_status <> 'ENCRYPTED'
        OR (template_ciphertext IS NOT NULL
            AND encryption_key_version IS NOT NULL
            AND encrypted_at IS NOT NULL)
    );

ALTER TABLE voice_profiles DROP CONSTRAINT IF EXISTS voice_profiles_shredded_holds_no_key;

DROP INDEX IF EXISTS idx_voice_profiles_kek;

-- The view goes BEFORE the columns, and the order is not cosmetic. `062`'s
-- view reads `wrapped_dek`, so PostgreSQL refuses to drop the column while it
-- exists — `ERROR: cannot drop column wrapped_dek … because other objects
-- depend on it`. Found by running this script rather than by reading it.
--
-- The tempting fix is `DROP COLUMN … CASCADE`, and it is the wrong one: CASCADE
-- would drop whatever else happens to depend on the column, which on a
-- deployed database is not a set this script knows. Dropping the one dependency
-- by name, first, is the version that cannot take something unnamed with it.
DROP VIEW IF EXISTS voice_migration_status;

ALTER TABLE voice_profiles DROP COLUMN IF EXISTS kek_id;
ALTER TABLE voice_profiles DROP COLUMN IF EXISTS wrap_nonce;
ALTER TABLE voice_profiles DROP COLUMN IF EXISTS wrapped_dek;
ALTER TABLE voice_profiles DROP COLUMN IF EXISTS template_nonce;

-- `037`'s view, restored exactly — recreated rather than replaced, because the
-- two columns 062 appended have to go and `CREATE OR REPLACE` can add columns
-- at the end but never remove them.
CREATE VIEW voice_migration_status
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

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'voice_profiles' AND column_name = 'wrapped_dek') THEN
        RAISE EXCEPTION '062-down: wrapped_dek survived';
    END IF;
    -- The things 062 must NOT have taken with it. A rollback of an encryption
    -- migration is most likely to destroy the data it was protecting.
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'voice_profiles' AND column_name = 'embedding') THEN
        RAISE EXCEPTION '062-down destroyed the plaintext template column';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'voice_profiles' AND column_name = 'template_ciphertext') THEN
        RAISE EXCEPTION '062-down destroyed template_ciphertext, which 037 added';
    END IF;
    IF to_regclass('public.voice_migration_status') IS NULL THEN
        RAISE EXCEPTION '062-down left no contract-gate view';
    END IF;
    IF to_regclass('public.user_mfa_keys') IS NULL THEN
        RAISE EXCEPTION '062-down destroyed user_mfa_keys';
    END IF;
END $$;

COMMIT;
