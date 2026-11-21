-- =============================================================================
-- 036_totp_ownership.sql — Phase 3B Batch 1A
--
-- TOTP secrets move from a username string to an immutable user_id, and gain
-- the columns an encrypted-at-rest secret needs.
--
-- ── The finding ──────────────────────────────────────────────────────────────
--
--     totp_secrets: PRIMARY KEY (username)
--
-- With one owner this is fine. With two tenants it is an **authentication
-- bypass**, not a privacy problem. Two tenants may each hold a user called
-- `alice`. There is one row for `alice`. Tenant B's alice enrols over — or
-- authenticates against — tenant A's alice's second factor.
--
-- That is why this table is first in Phase 3B, ahead of every content table.
-- A content table leaking is serious; a shared second factor is an incident.
--
-- ── What this migration does NOT do ──────────────────────────────────────────
--
-- **It does not encrypt anything.** Renaming a column, or copying a value into
-- one called `secret_ciphertext`, does not make it ciphertext. Envelope
-- encryption needs the KMS/DEK path in `core/connectors/keyring.py`, which is
-- application code and cannot run inside SQL.
--
-- So this migration EXPANDS: it adds the destination columns and a
-- `migration_status` that starts at `PLAINTEXT` and tells the truth. The
-- application worker (`scripts/phase3b/encrypt_secrets.py`) performs the
-- actual encryption and flips the status. Until it has run and been verified,
-- **nothing here claims the secrets are encrypted**, and the verification view
-- at the end reports exactly how many still are not.
--
-- `secret` — the plaintext column — is NOT dropped. Dropping it before every
-- row is encrypted and every reader has moved would be an outage, and dropping
-- it in the same migration that adds the replacement would leave no rollback.
-- It is dropped in a later phase, once `contract_ready` is true.
--
-- ── Ambiguity blocks ─────────────────────────────────────────────────────────
--
-- `user_id` is resolved strictly through `legacy_identity_map`. A username that
-- did not resolve leaves `user_id` NULL and the row unconstrained — visible,
-- reported, and **blocking the contract stage**. Guessing an owner for a
-- second-factor secret would be the worst possible silent failure.
--
-- Rollback: rollback/036_totp_ownership_down.sql
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Expand
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE totp_secrets ADD COLUMN IF NOT EXISTS user_id UUID
    REFERENCES users(id) ON DELETE CASCADE;

-- Encryption destination. Empty until the application worker fills it.
ALTER TABLE totp_secrets ADD COLUMN IF NOT EXISTS secret_ciphertext     BYTEA;
ALTER TABLE totp_secrets ADD COLUMN IF NOT EXISTS encryption_key_version INTEGER;
ALTER TABLE totp_secrets ADD COLUMN IF NOT EXISTS encrypted_at          TIMESTAMPTZ;
ALTER TABLE totp_secrets ADD COLUMN IF NOT EXISTS migration_status      TEXT NOT NULL DEFAULT 'PLAINTEXT';
ALTER TABLE totp_secrets ADD COLUMN IF NOT EXISTS id                    UUID NOT NULL DEFAULT uuid_generate_v4();
ALTER TABLE totp_secrets ADD COLUMN IF NOT EXISTS revoked_at            TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'totp_secrets_migration_status_check') THEN
        ALTER TABLE totp_secrets ADD CONSTRAINT totp_secrets_migration_status_check
            CHECK (migration_status IN ('PLAINTEXT', 'ENCRYPTING', 'ENCRYPTED', 'FAILED'));
    END IF;

    -- ENCRYPTED means all three encryption fields are present. Without this,
    -- "encrypted" is a label a bug can set; with it, the claim is structural.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'totp_secrets_encrypted_is_complete') THEN
        ALTER TABLE totp_secrets ADD CONSTRAINT totp_secrets_encrypted_is_complete
            CHECK (
                migration_status <> 'ENCRYPTED'
                OR (secret_ciphertext IS NOT NULL
                    AND encryption_key_version IS NOT NULL
                    AND encrypted_at IS NOT NULL)
            );
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Backfill ownership — strictly through the identity map
-- ─────────────────────────────────────────────────────────────────────────────

-- FORCE is lifted for the backfill. On a first run section 4 has not enabled
-- RLS yet and this changes nothing; on a re-run it is the difference between
-- backfilling and **silently matching zero rows**, because the migration binds
-- no `app.user_id` and `user_id = NULL` is NULL rather than true. A silent
-- no-op here leaves a second factor with no owner and no error. Restored in
-- section 4. Full reasoning in `035_memberships.sql` section 0.
ALTER TABLE totp_secrets NO FORCE ROW LEVEL SECURITY;

UPDATE totp_secrets t
   SET user_id = m.user_id
  FROM legacy_identity_map m
 WHERE m.legacy_username = t.username
   AND m.resolution = 'MAPPED'
   AND t.user_id IS NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Constrain what CAN be constrained now
--
--    `user_id` cannot be NOT NULL while an unresolved row exists — that is the
--    block working, not a gap. The contract stage (a later migration) applies
--    NOT NULL once the quarantine is empty.
--
--    The unique index below IS applied now, because it is what actually closes
--    the collision: one enrolment per user, and the old username PK stops being
--    the thing that identifies a secret.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE UNIQUE INDEX IF NOT EXISTS totp_secrets_user_key
    ON totp_secrets (user_id) WHERE user_id IS NOT NULL AND revoked_at IS NULL;

-- ── Retiring the username PRIMARY KEY ────────────────────────────────────────
--
-- Adding `user_id` is not sufficient on its own. While `PRIMARY KEY (username)`
-- remains, the collision is still there in its original form: two tenants
-- cannot both hold a user called `alice`, because the second enrolment violates
-- a GLOBAL primary key on the name.
--
-- So the key moves to the surrogate `id` added above, and the username keeps no
-- uniqueness of its own. It survives only as a compatibility column for readers
-- that have not migrated, and a compatibility column must not carry a
-- constraint that shapes the data model.
--
-- This is the step that actually closes R-26. Everything before it is
-- preparation.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'totp_secrets' AND c.contype = 'p'
          AND pg_get_constraintdef(c.oid) = 'PRIMARY KEY (username)'
    ) THEN
        ALTER TABLE totp_secrets DROP CONSTRAINT totp_secrets_pkey;
        ALTER TABLE totp_secrets ADD  CONSTRAINT totp_secrets_pkey PRIMARY KEY (id);
    END IF;
END
$$;

-- One enrolment per (user, username) during the transition, so a legacy reader
-- keyed on the name still finds one row — but two DIFFERENT users may share a
-- name, which is the whole point.
CREATE INDEX IF NOT EXISTS idx_totp_secrets_username ON totp_secrets (username);

CREATE INDEX IF NOT EXISTS idx_totp_secrets_migration_status
    ON totp_secrets (migration_status) WHERE migration_status <> 'ENCRYPTED';

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Row-level security — USER_PRIVATE
--
--    A tenant admin must not be able to read another person's second factor,
--    so the predicate is on `app.user_id`, NOT on `app.tenant_id`. A
--    tenant-only predicate would let any admin in the tenant read every
--    member's TOTP secret, which is exactly the shape of the bug being fixed.
--
--    Rows with a NULL user_id are visible to nobody: `NULL = <uuid>` is NULL,
--    not true. Unresolved rows are therefore inert rather than public — the
--    fail-closed direction.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE totp_secrets ENABLE ROW LEVEL SECURITY;
ALTER TABLE totp_secrets FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_private ON totp_secrets;
CREATE POLICY user_private ON totp_secrets
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Tenant MFA policy — stored SEPARATELY from the secret
--
--    A tenant can require MFA of its members. It must never be able to read
--    their secrets. Keeping the requirement in a different table with a
--    different classification is what makes that structural rather than a rule
--    somebody has to remember.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tenant_mfa_policy (
    tenant_id     UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    mfa_required  BOOLEAN NOT NULL DEFAULT FALSE,
    grace_period_days INTEGER NOT NULL DEFAULT 0 CHECK (grace_period_days >= 0),
    updated_by    UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE tenant_mfa_policy
    ALTER COLUMN tenant_id SET DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid;

ALTER TABLE tenant_mfa_policy ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_mfa_policy FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON tenant_mfa_policy;
CREATE POLICY tenant_isolation ON tenant_mfa_policy
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP TRIGGER IF EXISTS trg_tenant_mfa_policy_updated_at ON tenant_mfa_policy;
CREATE TRIGGER trg_tenant_mfa_policy_updated_at
    BEFORE UPDATE ON tenant_mfa_policy
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. Contract-readiness view
--
--    Reports what remains, and reports NO SECRET VALUE. Counts only.
--    `contract_ready` is the gate a later migration checks before applying
--    NOT NULL and dropping the plaintext column.
-- ─────────────────────────────────────────────────────────────────────────────

-- **`security_invoker` is load-bearing, not a style choice.** A view defaults to
-- running as its OWNER, and the owner is subject to FORCE ROW LEVEL SECURITY
-- like anybody else. Without this setting the view evaluates with the migration
-- role's (unbound) context, matches zero rows, and reports
-- `contract_ready = true` on a table full of plaintext — to every caller,
-- including a superuser, because the caller's privileges never enter into it.
-- That was the state of this view as originally written; it was found by
-- reading it as a non-superuser owner.
CREATE OR REPLACE VIEW totp_migration_status
WITH (security_invoker = true) AS
SELECT
    count(*)                                            AS total_rows,
    count(*) FILTER (WHERE user_id IS NULL)             AS unresolved_owner,
    count(*) FILTER (WHERE migration_status = 'PLAINTEXT') AS still_plaintext,
    count(*) FILTER (WHERE migration_status = 'FAILED')    AS encryption_failed,
    count(*) FILTER (WHERE migration_status = 'ENCRYPTED')  AS encrypted,
    (count(*) FILTER (WHERE user_id IS NULL) = 0
     AND count(*) FILTER (WHERE migration_status <> 'ENCRYPTED') = 0) AS contract_ready
FROM totp_secrets
WHERE revoked_at IS NULL;

COMMENT ON VIEW totp_migration_status IS
    'Counts only — never a secret value. contract_ready gates the later '
    'migration that applies NOT NULL and drops the plaintext column.';

COMMENT ON COLUMN totp_secrets.username IS
    'LEGACY. Not a security principal. Retained for compatibility during the '
    'transition; user_id is the owner. Dropped once contract_ready is true.';
COMMENT ON COLUMN totp_secrets.secret IS
    'LEGACY PLAINTEXT. Replaced by secret_ciphertext. Still populated during '
    'the transition; NOT dropped here because no row is encrypted yet.';
COMMENT ON COLUMN totp_secrets.migration_status IS
    'PLAINTEXT until the application worker encrypts this row. This column '
    'exists so the state is honest rather than assumed.';
