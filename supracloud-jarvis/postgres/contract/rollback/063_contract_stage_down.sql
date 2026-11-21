-- =============================================================================
-- contract/rollback/063_contract_stage_down.sql — reverse 063
--
-- Puts the plaintext COLUMNS back. It cannot put the plaintext back.
--
-- ── The one rollback in this chain that is honestly incomplete ──────────────
--
-- Every other reversal here restores the state it removed. This one restores a
-- shape. `063` dropped `totp_secrets.secret` and `voice_profiles.embedding`,
-- and the values that were in them are gone — they were empty by then, because
-- the gate refused unless every row was already encrypted, but the columns come
-- back NULL either way and nothing in this script can populate them.
--
-- That is not a defect to fix here. It is what a contract stage IS, and the
-- honest thing is to say so rather than to leave an operator expecting a
-- restore. The data is recoverable from the CIPHERTEXT, which `063` was careful
-- not to touch, and the route is:
--
--     scripts/phase2/mfa_migrate.py       (TOTP)
--     scripts/phase4/encrypt_voiceprints.py decrypt --confirm   (voice)
--
-- Run those AFTER this script, not before. Before it there is no column for
-- them to write to, which is precisely the interaction `rollback/062`'s HINT
-- runs into: it tells an operator to decrypt back to plaintext first, and after
-- `063` that instruction has nowhere to land. Reverse this one, then decrypt,
-- then reverse `062`.
--
-- ── Why there is no refusal here ────────────────────────────────────────────
--
-- Unlike `062`, `061` or `060`, running this destroys nothing. It ADDS two
-- nullable columns. The dangerous direction was forward, and that is where the
-- guard is.
--
-- The one thing worth asserting is that the ciphertext is still present when
-- this runs — because if it is not, then the plaintext is gone AND the
-- ciphertext is gone, and re-adding an empty column would quietly present a
-- schema that looks whole around data that no longer exists.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_orphaned bigint;
BEGIN
    IF to_regclass('public.voice_profiles') IS NULL THEN
        RETURN;
    END IF;

    -- Counted with FORCE lifted, for the reason `063` states at length: these
    -- tables are USER_PRIVATE, this script binds no GUCs, and an unbound
    -- connection matches zero rows. A count of zero here would report "no
    -- orphaned rows" for a database in which every row is orphaned.
    EXECUTE 'ALTER TABLE voice_profiles NO FORCE ROW LEVEL SECURITY';
    SELECT count(*) INTO n_orphaned FROM voice_profiles
     WHERE migration_status = 'ENCRYPTED'
       AND template_ciphertext IS NULL
       AND deletion_status = 'live';
    EXECUTE 'ALTER TABLE voice_profiles FORCE ROW LEVEL SECURITY';

    IF n_orphaned > 0 THEN
        RAISE EXCEPTION
            '063-down REFUSING: % voice row(s) claim ENCRYPTED and hold no '
            'ciphertext.', n_orphaned
            USING HINT =
                'Re-adding the plaintext column would present a whole-looking '
                'schema around templates that no longer exist in either form. '
                'Establish what happened to the ciphertext first — this script '
                'cannot recover it and neither can the decrypt worker.';
    END IF;
END $$;

-- Nullable, and no default. `037` had `embedding` NOT NULL; restoring that
-- constraint would fail immediately on every row, because there is nothing to
-- put in it. A column that is back but empty is the truth.
ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS embedding text;
ALTER TABLE totp_secrets   ADD COLUMN IF NOT EXISTS secret    text;

-- ── The constraints that went with the columns ─────────────────────────────
--
-- PostgreSQL drops a CHECK when a column it references is dropped, so `063`
-- destroyed `totp_secrets_encrypted_is_complete` (which names `secret`) and
-- `totp_secrets_has_a_secret` (which is entirely about it) as a side effect it
-- never mentioned. Re-adding the columns without them leaves a schema that
-- looks restored and has lost two guarantees — including the one that stops a
-- row existing with no readable secret in either form, which is a user locked
-- out with nothing to recover from.
--
-- Found by running the corpus after a reversal, not by reading this file: three
-- tests failed on constraints nobody had noticed were gone.
--
-- These definitions are `053`'s, restored verbatim. Duplicating them here is
-- the cost of a reversal that is honest about what the forward step destroyed;
-- the alternative is a rollback that silently under-restores.
ALTER TABLE totp_secrets DROP CONSTRAINT IF EXISTS totp_secrets_encrypted_is_complete;
ALTER TABLE totp_secrets ADD CONSTRAINT totp_secrets_encrypted_is_complete
    CHECK (
        migration_status <> 'ENCRYPTED'
        OR (secret_ciphertext IS NOT NULL
            AND secret_nonce IS NOT NULL
            AND encryption_key_version IS NOT NULL
            AND encrypted_at IS NOT NULL
            AND secret IS NULL)
    );

ALTER TABLE totp_secrets DROP CONSTRAINT IF EXISTS totp_secrets_has_a_secret;
ALTER TABLE totp_secrets ADD CONSTRAINT totp_secrets_has_a_secret
    CHECK (secret IS NOT NULL OR secret_ciphertext IS NOT NULL);

COMMENT ON COLUMN voice_profiles.embedding IS
    'Restored by rollback/063 and EMPTY. The templates live in '
    'template_ciphertext; run scripts/phase4/encrypt_voiceprints.py decrypt '
    '--confirm to repopulate this column.';
COMMENT ON COLUMN totp_secrets.secret IS
    'Restored by rollback/063 and EMPTY. The seeds live in secret_ciphertext; '
    'run scripts/phase2/mfa_migrate.py to repopulate this column.';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'voice_profiles' AND column_name = 'embedding') THEN
        RAISE EXCEPTION '063-down: voice_profiles.embedding was not restored';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'totp_secrets' AND column_name = 'secret') THEN
        RAISE EXCEPTION '063-down: totp_secrets.secret was not restored';
    END IF;
    -- The ciphertext is the only remaining copy. A reversal that took it would
    -- destroy what it was meant to make recoverable.
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'voice_profiles' AND column_name = 'template_ciphertext') THEN
        RAISE EXCEPTION '063-down destroyed template_ciphertext';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'totp_secrets' AND column_name = 'secret_ciphertext') THEN
        RAISE EXCEPTION '063-down destroyed secret_ciphertext';
    END IF;
    IF NOT (SELECT relforcerowsecurity FROM pg_class
             WHERE oid = 'public.voice_profiles'::regclass) THEN
        RAISE EXCEPTION
            '063-down left voice_profiles without FORCE ROW LEVEL SECURITY';
    END IF;
    -- The two CHECKs the forward step destroyed as a side effect.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'totp_secrets_has_a_secret') THEN
        RAISE EXCEPTION
            '063-down: totp_secrets_has_a_secret was not restored. Without it a '
            'row can exist holding no secret in either form, which is a user '
            'locked out with nothing to recover from.';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'totp_secrets_encrypted_is_complete') THEN
        RAISE EXCEPTION
            '063-down: totp_secrets_encrypted_is_complete was not restored';
    END IF;
END $$;

COMMIT;
