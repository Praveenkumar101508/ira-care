-- =============================================================================
-- 053_mfa_key_material.sql — P2-05, encryption at rest for TOTP seeds
--
-- The key hierarchy:
--
--     tenant KEK  (the root wrapping key, already in tenant_data_keys)
--          ↓  wraps
--     per-USER MFA DEK        ← this migration's `user_mfa_keys`
--          ↓  encrypts
--     the TOTP seed           ← `totp_secrets.secret_ciphertext`
--
-- ── Why a new table and not a `user_id` column on tenant_data_keys ──────────
--
-- Three reasons, and the third is decisive.
--
-- 1. `tenant_data_keys` is keyed `(tenant_id, purpose, key_version)` and carries
--    `one_active_key_per_tenant_purpose`, a unique index whose whole meaning is
--    "one active key per tenant and purpose". Adding a user dimension changes
--    what that invariant asserts for every existing caller — `active_dek` reads
--    it with no user in hand — which is weakening its semantics to fit a new
--    tenant, not generalising it.
--
-- 2. Its rotation semantics are the opposite of what MFA needs.
--    `rotate_tenant` RETIRES and then DESTROYS the old key. An MFA DEK must
--    survive rotation by being re-wrapped, because destroying it destroys every
--    second factor sealed under it.
--
-- 3. **Its RLS policy is tenant-only.** `tenant_isolation` predicates on
--    `app.tenant_id` alone, which is correct for connector credentials and
--    fatal here: any tenant-bound connection could read the wrapped key for
--    every member. That hands a tenant administrator the unwrapping capability
--    through the back door and undoes the property `052` exists to protect.
--
-- So this table gets the `052` treatment — tenant AND user, ANDed, four
-- per-command policies, forced.
--
-- ── What rotation does, and does not, do ────────────────────────────────────
--
-- When the tenant root key rotates, the wrapped DEKs are RE-WRAPPED under the
-- new KEK. The DEK itself does not change and **no TOTP seed is decrypted or
-- re-encrypted**. That is the whole reason for the middle layer: re-sealing
-- every seed on every rotation would mean holding every plaintext second factor
-- in memory during routine key maintenance, and an interruption halfway would
-- leave rows nobody can open.
--
-- ── Migration of existing plaintext, and why NOT NULL is not set here ───────
--
-- `secret` becomes NULLABLE but is not dropped and is not cleared. Rows migrate
-- one at a time, in the application, in the order write-ciphertext → verify →
-- clear-plaintext. A migration that cleared plaintext in SQL would be
-- destroying the only readable copy before anything had been proven readable.
--
-- Rollback: rollback/053_mfa_key_material_down.sql
-- =============================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Per-user MFA key material
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_mfa_keys (
    tenant_id           uuid        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             uuid        NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    key_version         integer     NOT NULL,
    -- NULL once shredded. The row survives destruction so that "this key was
    -- destroyed, when, and why" remains answerable — a deleted row cannot say
    -- whether a key ever existed.
    wrapped_dek         bytea,
    wrap_nonce          bytea,
    kek_id              text        NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    retired_at          timestamptz,
    destroyed_at        timestamptz,
    destruction_reason  text,
    PRIMARY KEY (tenant_id, user_id, key_version)
);

-- One live key per user, mirroring `one_active_key_per_tenant_purpose`. This is
-- what makes the concurrent first-enrolment race safe: two simultaneous
-- enrolments both try to mint, one INSERT loses, and the loser re-reads.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_mfa_key_per_user
    ON user_mfa_keys (tenant_id, user_id)
    WHERE retired_at IS NULL AND destroyed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_user_mfa_keys_live
    ON user_mfa_keys (tenant_id, user_id, key_version DESC)
    WHERE destroyed_at IS NULL;

-- A destroyed key must have nothing left to unwrap with. Structural, so
-- "destroyed" cannot be a label a bug sets while the material is still present.
ALTER TABLE user_mfa_keys DROP CONSTRAINT IF EXISTS user_mfa_keys_destroyed_is_empty;
ALTER TABLE user_mfa_keys ADD CONSTRAINT user_mfa_keys_destroyed_is_empty
    CHECK (destroyed_at IS NULL OR (wrapped_dek IS NULL AND wrap_nonce IS NULL));

-- A live key must actually carry material.
ALTER TABLE user_mfa_keys DROP CONSTRAINT IF EXISTS user_mfa_keys_live_is_complete;
ALTER TABLE user_mfa_keys ADD CONSTRAINT user_mfa_keys_live_is_complete
    CHECK (destroyed_at IS NOT NULL
           OR (wrapped_dek IS NOT NULL AND wrap_nonce IS NOT NULL));

ALTER TABLE user_mfa_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_mfa_keys FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS user_mfa_key_select ON user_mfa_keys;
DROP POLICY IF EXISTS user_mfa_key_insert ON user_mfa_keys;
DROP POLICY IF EXISTS user_mfa_key_update ON user_mfa_keys;
DROP POLICY IF EXISTS user_mfa_key_delete ON user_mfa_keys;

-- ANDed, never ORed — see 052's header. A permissive tenant-only policy beside
-- these would let any tenant-bound connection unwrap every member's MFA DEK,
-- which is precisely the capability this table exists to withhold.
CREATE POLICY user_mfa_key_select ON user_mfa_keys FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid);

CREATE POLICY user_mfa_key_insert ON user_mfa_keys FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid);

CREATE POLICY user_mfa_key_update ON user_mfa_keys FOR UPDATE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid);

CREATE POLICY user_mfa_key_delete ON user_mfa_keys FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON user_mfa_keys TO ira_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON user_mfa_keys TO ira_worker;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. The seed's ciphertext columns
--
--    036 added `secret_ciphertext`, `encryption_key_version`, `encrypted_at`
--    and `migration_status` and left them unused. The nonce was missing, which
--    is why they could not have been used: AES-GCM needs one and it is not
--    derivable.
--
--    `encryption_key_version` now means `user_mfa_keys.key_version`, so
--    (tenant_id, user_id, encryption_key_version) locates the key exactly.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE totp_secrets ADD COLUMN IF NOT EXISTS secret_nonce bytea;

-- Plaintext must be clearable. It is NOT cleared here: rows migrate one at a
-- time in the application, write-then-verify-then-clear, because SQL cannot
-- encrypt and clearing first would destroy the only readable copy.
ALTER TABLE totp_secrets ALTER COLUMN secret DROP NOT NULL;

-- Extend 036's completeness check to the nonce, and add the property that makes
-- "encrypted at rest" true rather than claimed: an ENCRYPTED row holds no
-- plaintext.
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

-- …and the converse: a row that still holds plaintext must not claim otherwise,
-- and a row must always hold ONE readable form. Without this, a bug that
-- cleared `secret` before writing ciphertext would leave a row with no secret
-- at all — a user locked out of their own account with nothing to recover from.
ALTER TABLE totp_secrets DROP CONSTRAINT IF EXISTS totp_secrets_has_a_secret;
ALTER TABLE totp_secrets ADD CONSTRAINT totp_secrets_has_a_secret
    CHECK (secret IS NOT NULL OR secret_ciphertext IS NOT NULL);

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Assert the end state
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    n_policies bigint;
    forced     boolean;
BEGIN
    SELECT relforcerowsecurity INTO forced
      FROM pg_class WHERE oid = 'public.user_mfa_keys'::regclass;
    IF NOT forced THEN
        RAISE EXCEPTION '053 FAILED: user_mfa_keys is not FORCE ROW LEVEL SECURITY.';
    END IF;

    SELECT count(*) INTO n_policies
      FROM pg_policy WHERE polrelid = 'public.user_mfa_keys'::regclass;
    IF n_policies <> 4 THEN
        RAISE EXCEPTION
            '053 FAILED: expected 4 policies on user_mfa_keys, found %.', n_policies
            USING HINT =
                'Permissive policies combine with OR, so an extra one can only '
                'widen access. A tenant-only policy here would let any '
                'tenant-bound connection unwrap every member''s MFA key.';
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_policy
         WHERE polrelid = 'public.user_mfa_keys'::regclass
           AND NOT (coalesce(pg_get_expr(polqual, polrelid), '') ||
                    coalesce(pg_get_expr(polwithcheck, polrelid), ''))
               LIKE '%app.user_id%'
    ) THEN
        RAISE EXCEPTION
            '053 FAILED: a user_mfa_keys policy does not constrain app.user_id.';
    END IF;
END
$$;

COMMIT;
