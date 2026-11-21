-- =============================================================================
-- 062_voiceprint_encryption.sql — Phase 4 batch 02, the per-record key columns
--
-- `037` added the destination columns for an encrypted voice template —
-- `template_ciphertext`, `encryption_key_version`, `encrypted_at`,
-- `migration_status` — and said in its own header that SQL cannot encrypt and
-- the application worker does. The worker was never written, because the key
-- scope was an open decision (D-17): a voiceprint belongs to a PERSON, and
-- sealing one under a tenant key would let a tenant deletion cryptographically
-- destroy a former member's biometric enrolment.
--
-- D-17 is decided. CLOUD-ADR-0012 is the answer, and this migration adds the
-- columns that decision requires — the ones its own Consequences section
-- predicted:
--
--     "The schema 036 and 037 already provide … needs one further column for
--      the wrapped per-record key. That is a migration in the encryption
--      batch, not now."
--
-- This is that batch, and it needs four rather than one: a wrapped key is
-- useless without the nonce that unwraps it and the identifier of the key that
-- wrapped it, and the sealed template needs its own nonce distinct from that.
--
-- ── Per RECORD, not per user ────────────────────────────────────────────────
--
-- ADR-0012 says "a fresh data-encryption key for every record", and gives the
-- reason: "the per-record key is what makes crypto-shredding a single enrolment
-- possible". That is not decoration here. `037` gave this table
-- `enrollment_version` and a per-row `deletion_status`, so one person may hold
-- several enrolments and revoke one of them — and `deletion_status` includes
-- `crypto_shredded` as a distinct outcome from `hard_deleted`. Destroying the
-- key for one row must not touch another, and a per-user key cannot express
-- that.
--
-- This differs from `053`/`user_mfa_keys`, which is per USER. That shape is
-- defensible for a TOTP seed, of which a person has exactly one — per-user and
-- per-record coincide there. They do not coincide here, and where they diverge
-- the ADR governs. The divergence is recorded rather than quietly matched.
--
-- ── What wraps the record key ───────────────────────────────────────────────
--
-- The environment KEK, through `core.connectors.secrets.wrap_dek` — NOT
-- `tenant_data_keys`. ADR-0012 names the `030` registry as "the KEK layer", and
-- for connector credentials it is. For a USER_PRIVATE biometric template it
-- cannot be, and D-17's analysis is why: `tenant_data_keys` carries a
-- tenant-only RLS policy, so any tenant-bound connection can read the material
-- that opens it — handing a tenant administrator the capability that `037`'s
-- policies exist to withhold. `053` reached the same conclusion for MFA. The
-- hierarchy is preserved; only the layer that holds the KEK moves outside the
-- database, which is where ADR-0012 wants key material anyway.
--
-- ── This migration encrypts nothing ─────────────────────────────────────────
--
-- Same honesty `036` and `037` insisted on. It adds columns. `migration_status`
-- stays `PLAINTEXT` for every existing row, `voice_migration_status.contract_
-- ready` stays false, and nothing here permits a claim that voiceprints are
-- encrypted at rest. The worker in `scripts/phase4/encrypt_voiceprints.py` is
-- what changes that, one row at a time, and `063` is what drops the plaintext.
--
-- Rollback: rollback/062_voiceprint_encryption_down.sql
-- =============================================================================

BEGIN;

-- The nonce for the sealed TEMPLATE. `037` added `template_ciphertext` and no
-- nonce, which makes the ciphertext column unusable on its own: AES-256-GCM
-- needs the nonce to open, and reusing one across records is the single worst
-- mistake available in GCM. Absent rather than wrong — nothing ever wrote the
-- column — but it has to exist before anything can.
ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS template_nonce BYTEA;

-- The per-record data-encryption key, wrapped. Never the key itself: ADR-0012
-- says key material never lives in PostgreSQL, and a wrapped key is ciphertext.
ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS wrapped_dek BYTEA;
ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS wrap_nonce  BYTEA;

-- Which KEK wrapped it. Without this a KEK rotation cannot tell which rows it
-- still has to re-wrap, and `secrets._open_secret` refuses rather than guessing
-- — the "decrypt oracle by fallback" it exists to prevent.
ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS kek_id TEXT;

-- ── The completeness constraint, tightened ──────────────────────────────────
--
-- `037` asserted that an ENCRYPTED row carries ciphertext, a key version and a
-- timestamp. That was as much as it could assert, because the columns that
-- actually make a row OPENABLE did not exist yet. They do now, and an ENCRYPTED
-- row missing any of them is a row whose plaintext was destroyed and whose
-- ciphertext nobody can read — which `063` would then happily drop the last
-- copy of.
ALTER TABLE voice_profiles
    DROP CONSTRAINT IF EXISTS voice_profiles_encrypted_is_complete;

-- `crypto_shredded` is exempt, and leaving it out was a defect this migration's
-- own test suite found: a shredded row is ENCRYPTED (it still holds ciphertext)
-- and deliberately has no key, so the two constraints below contradicted each
-- other and a crypto-shred was unrepresentable. The state the table was built
-- to express could not be written.
--
-- The exemption is narrow on purpose. It says "an ENCRYPTED row must be
-- openable UNLESS somebody deliberately destroyed its key and recorded that
-- they did" — which is the difference between a shred and a corruption, and the
-- reason `037` made `crypto_shredded` a distinct `deletion_status` rather than
-- reusing `hard_deleted`.
ALTER TABLE voice_profiles ADD CONSTRAINT voice_profiles_encrypted_is_complete
    CHECK (
        migration_status <> 'ENCRYPTED'
        OR deletion_status = 'crypto_shredded'
        OR (template_ciphertext IS NOT NULL
            AND template_nonce   IS NOT NULL
            AND wrapped_dek      IS NOT NULL
            AND wrap_nonce       IS NOT NULL
            AND kek_id           IS NOT NULL
            AND encryption_key_version IS NOT NULL
            AND encrypted_at           IS NOT NULL)
    );

-- A crypto-shredded row is the mirror image, and needs saying separately: the
-- key is gone and the ciphertext may remain, which is the whole point of
-- shredding. What must NOT remain is a wrapped key, because a shred that left
-- one behind shredded nothing.
ALTER TABLE voice_profiles
    DROP CONSTRAINT IF EXISTS voice_profiles_shredded_holds_no_key;

ALTER TABLE voice_profiles ADD CONSTRAINT voice_profiles_shredded_holds_no_key
    CHECK (
        deletion_status <> 'crypto_shredded'
        OR (wrapped_dek IS NULL AND wrap_nonce IS NULL)
    );

-- Finding the rows still to do, and the rows a KEK rotation must re-wrap.
-- Partial, because once the migration completes the interesting set is empty
-- and an index over every row would be dead weight.
CREATE INDEX IF NOT EXISTS idx_voice_profiles_kek
    ON voice_profiles (kek_id) WHERE kek_id IS NOT NULL;

-- ── The contract gate, restated over the new columns ────────────────────────
--
-- `037` created this view and gated `063` on it. Its `contract_ready` counted
-- only `migration_status`, which was correct then and is not now: a row can be
-- marked ENCRYPTED and still be unopenable if the worker wrote the status
-- before the key. The constraint above makes that unrepresentable going
-- forward; the view is where a human reads the answer, so it says so too.
--
-- Counts only. Never a template, never a key — a view over biometric material
-- is exactly the thing somebody selects `*` from while debugging.
--
-- EXTENDED, not reshaped. The first seven columns keep `037`'s names, order and
-- types, and `security_invoker` stays as `037` set it after finding the view
-- readable as a non-superuser owner. `CREATE OR REPLACE VIEW` cannot rename or
-- reorder columns and would fail rather than silently diverge — which is the
-- correct behaviour, and it is why the new counts are appended.
--
-- `contract_ready`'s EXPRESSION changes, and that is the point of touching the
-- view at all. It has to, for the same reason the constraint above had to: a
-- row can be marked ENCRYPTED and still be unopenable if the worker wrote the
-- status before the key, and `063` reads this flag before destroying the only
-- plaintext.
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
     AND count(*) FILTER (WHERE migration_status <> 'ENCRYPTED') = 0
     AND count(*) FILTER (WHERE migration_status = 'ENCRYPTED'
                            AND wrapped_dek IS NULL) = 0)   AS contract_ready,
    count(*) FILTER (WHERE migration_status = 'ENCRYPTING')  AS in_flight,
    count(*) FILTER (WHERE migration_status = 'ENCRYPTED'
                       AND wrapped_dek IS NULL)              AS encrypted_but_keyless
FROM voice_profiles
WHERE deletion_status = 'live';

COMMENT ON VIEW voice_migration_status IS
    'Counts only — never a biometric template and never a key. Until '
    'contract_ready is true, this system MUST NOT claim voiceprints are '
    'encrypted at rest and 063 MUST NOT drop the plaintext column. 062 added '
    'the requirement that no row claims ENCRYPTED while holding no wrapped '
    'key, because such a row is ciphertext nobody can open.';

DO $$
DECLARE
    n integer;
BEGIN
    FOR n IN
        SELECT 1 FROM unnest(ARRAY['template_nonce','wrapped_dek','wrap_nonce','kek_id']) AS c
         WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'voice_profiles' AND column_name = c)
    LOOP
        RAISE EXCEPTION '062: a per-record key column is missing';
    END LOOP;

    -- The plaintext column MUST still be here. 062 adds capacity; it does not
    -- perform the contract stage, and a migration that quietly took the
    -- plaintext with it would destroy every template in one step.
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'voice_profiles' AND column_name = 'embedding') THEN
        RAISE EXCEPTION
            '062: voice_profiles.embedding is gone. This migration adds the '
            'columns an encrypted template needs; dropping the plaintext is '
            '063, and only once voice_migration_status.contract_ready is true.';
    END IF;

    IF (SELECT count(*) FROM voice_profiles
         WHERE migration_status = 'ENCRYPTED' AND wrapped_dek IS NULL) > 0 THEN
        RAISE EXCEPTION
            '062: a row claims ENCRYPTED but carries no wrapped key. It is '
            'ciphertext nobody can open.';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'voice_profiles_shredded_holds_no_key') THEN
        RAISE EXCEPTION '062: the crypto-shred guard is missing';
    END IF;
END $$;

COMMIT;
