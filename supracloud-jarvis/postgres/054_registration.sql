-- =============================================================================
-- 054_registration.sql — Phase 2 batch 2, registration and email verification
--
-- Two tables, and they are separate on purpose.
--
-- ── Why credentials are NOT a column on `users` ─────────────────────────────
--
-- `users` carries **no row-level security**. That is correct and deliberate: it
-- is the pre-tenant identity register, and `_resolve_cloud_principal` has to
-- read it before any tenant is known, so a tenant-keyed policy could not be
-- satisfied. The cost is that every query against `users` can see every column.
--
-- A `password_hash` column there would therefore be readable by every code path
-- that looks up a user — including the identity resolution that runs on every
-- authenticated request, which has no business holding credential material.
-- `user_credentials` is a separate table with FORCE RLS keyed on the user, so
-- reaching a hash requires a connection bound to that user specifically.
--
-- ── The verification tokens table is keyed on the TOKEN, not the user ───────
--
-- Verification is performed by an UNAUTHENTICATED caller presenting a token, so
-- there is no principal to bind and the user is unknown until the token has
-- been looked up. A policy on `app.user_id` therefore cannot be satisfied by
-- the only path that legitimately reads this table.
--
-- The first draft concluded from that that RLS was impossible here and left the
-- table unprotected. `tests/readiness/test_cross_tenant_sweep.py` refused it,
-- and it was right to: a `user_id` column with no policy means isolation rests
-- entirely on every query remembering `WHERE user_id = ...`, with no
-- database-level backstop when one forgets.
--
-- The satisfiable policy keys on the thing the caller actually presents:
--
--     token_hash = current_setting('app.verification_token_hash', true)
--
-- which is STRONGER than the user-keyed policy that could not be written. A
-- connection may see exactly one row — the token it already holds — so a query
-- that forgot its WHERE clause still cannot enumerate anybody's tokens, and an
-- unbound connection sees none at all.
--
-- **The token itself is never stored** — only a SHA-256 hash, exactly as
-- `user_sessions.token_hash` does it, and for the reason recorded there: *"a
-- session table that stores usable tokens turns a read of this table into
-- account takeover."*
--
-- ── What this migration deliberately does NOT do ────────────────────────────
--
-- It does not create tenants, workspaces or memberships. A verified account is
-- an *identity*, not an *authorisation*: after verification the user resolves
-- to nothing, because `_resolve_cloud_principal` requires a membership that
-- grants access and there is none. Tenant provisioning is the next batch, and
-- keeping it out of this one means a verified-but-unprovisioned account is a
-- representable, tested state rather than an accident.
--
-- Rollback: rollback/054_registration_down.sql
-- =============================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Credentials
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_credentials (
    user_id       uuid        PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    -- passlib's bcrypt output. The algorithm is recorded rather than assumed so
    -- a future rehash-on-login can tell what it is looking at.
    password_hash text        NOT NULL,
    algorithm     text        NOT NULL DEFAULT 'bcrypt',
    created_at    timestamptz NOT NULL DEFAULT NOW(),
    updated_at    timestamptz NOT NULL DEFAULT NOW()
);

ALTER TABLE user_credentials DROP CONSTRAINT IF EXISTS user_credentials_hash_is_not_plaintext;
-- A bcrypt hash always starts `$2`. This does not make a weak hash strong; it
-- makes the one catastrophic mistake — writing the password itself into the
-- column — impossible rather than merely unlikely.
ALTER TABLE user_credentials ADD CONSTRAINT user_credentials_hash_is_not_plaintext
    CHECK (password_hash LIKE '$%' AND length(password_hash) >= 20);

ALTER TABLE user_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_credentials FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS user_credential_select ON user_credentials;
DROP POLICY IF EXISTS user_credential_insert ON user_credentials;
DROP POLICY IF EXISTS user_credential_update ON user_credentials;
DROP POLICY IF EXISTS user_credential_delete ON user_credentials;

-- Keyed on the USER only, with no tenant predicate — and that is not an
-- oversight. Credentials exist before any tenant does: a registered user has no
-- membership until the next batch provisions one, so a tenant predicate would
-- make every credential unreadable at exactly the moment it is needed.
--
-- Four per-command policies rather than one FOR ALL, matching 052 and 053.
-- Permissive policies combine with OR, so the count is asserted below: an extra
-- policy here can only widen access to password material.
CREATE POLICY user_credential_select ON user_credentials FOR SELECT
    USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

CREATE POLICY user_credential_insert ON user_credentials FOR INSERT
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

CREATE POLICY user_credential_update ON user_credentials FOR UPDATE
    USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

CREATE POLICY user_credential_delete ON user_credentials FOR DELETE
    USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON user_credentials TO ira_app;

-- REVOKED from ira_worker, and the revoke is REQUIRED — not belt and braces.
--
-- `provisioning/001_database_roles.sql` sets ALTER DEFAULT PRIVILEGES for
-- `ira_schema_owner` granting `arwd` on every new table to BOTH `ira_app` and
-- `ira_worker`. So a table is readable by the worker the moment it exists, and
-- withholding a grant achieves nothing: privilege here is opt-OUT, not opt-in.
--
-- This was found by the assertion at the bottom of this file failing on the
-- first run, not by reading the provisioning script — which is the point of
-- asserting the end state rather than the statements that were issued.
--
-- A background job has no reason to hold a password hash. If a future sweeper
-- genuinely needs one of these tables, it can be granted deliberately.
REVOKE ALL ON user_credentials FROM ira_worker;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Email verification tokens
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS email_verification_tokens (
    id           uuid        PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id      uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- SHA-256 of the token. Never the token. See the header.
    token_hash   text        NOT NULL,
    -- The address this token proves. Recorded so that changing the account's
    -- email invalidates tokens issued for the old one: without it, a token
    -- minted for alice@old could verify alice@new, which is the wrong address
    -- being marked verified.
    email        text        NOT NULL,
    expires_at   timestamptz NOT NULL,
    consumed_at  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS email_verification_tokens_hash_key
    ON email_verification_tokens (token_hash);

-- Finding the live token for a user, for the resend path and for expiry sweeps.
CREATE INDEX IF NOT EXISTS idx_email_verification_live
    ON email_verification_tokens (user_id) WHERE consumed_at IS NULL;

ALTER TABLE email_verification_tokens
    DROP CONSTRAINT IF EXISTS email_verification_tokens_hash_is_a_hash;
-- SHA-256 hex is exactly 64 characters. A token written here by mistake would
-- almost certainly not be, so the shape is checked rather than trusted.
ALTER TABLE email_verification_tokens ADD CONSTRAINT email_verification_tokens_hash_is_a_hash
    CHECK (token_hash ~ '^[0-9a-f]{64}$');

ALTER TABLE email_verification_tokens
    DROP CONSTRAINT IF EXISTS email_verification_tokens_expiry_is_after_issue;
ALTER TABLE email_verification_tokens ADD CONSTRAINT email_verification_tokens_expiry_is_after_issue
    CHECK (expires_at > created_at);

ALTER TABLE email_verification_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_verification_tokens FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS verification_token_select ON email_verification_tokens;
DROP POLICY IF EXISTS verification_token_insert ON email_verification_tokens;
DROP POLICY IF EXISTS verification_token_update ON email_verification_tokens;
DROP POLICY IF EXISTS verification_token_delete ON email_verification_tokens;

-- Keyed on the presented token — see the header. `NULLIF(..., '')` so an unset
-- GUC is NULL rather than the empty string, and `token_hash = NULL` is NULL
-- rather than true: an unbound connection matches nothing.
CREATE POLICY verification_token_select ON email_verification_tokens FOR SELECT
    USING (token_hash = NULLIF(current_setting('app.verification_token_hash', true), ''));

CREATE POLICY verification_token_insert ON email_verification_tokens FOR INSERT
    WITH CHECK (token_hash = NULLIF(current_setting('app.verification_token_hash', true), ''));

CREATE POLICY verification_token_update ON email_verification_tokens FOR UPDATE
    USING (token_hash = NULLIF(current_setting('app.verification_token_hash', true), ''))
    WITH CHECK (token_hash = NULLIF(current_setting('app.verification_token_hash', true), ''));

CREATE POLICY verification_token_delete ON email_verification_tokens FOR DELETE
    USING (token_hash = NULLIF(current_setting('app.verification_token_hash', true), ''));

GRANT SELECT, INSERT, UPDATE, DELETE ON email_verification_tokens TO ira_app;
REVOKE ALL ON email_verification_tokens FROM ira_worker;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Assert the end state
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    n_policies bigint;
    forced     boolean;
BEGIN
    SELECT relforcerowsecurity INTO forced
      FROM pg_class WHERE oid = 'public.user_credentials'::regclass;
    IF NOT forced THEN
        RAISE EXCEPTION '054 FAILED: user_credentials is not FORCE ROW LEVEL SECURITY.';
    END IF;

    SELECT count(*) INTO n_policies
      FROM pg_policy WHERE polrelid = 'public.user_credentials'::regclass;
    IF n_policies <> 4 THEN
        RAISE EXCEPTION
            '054 FAILED: expected 4 policies on user_credentials, found %.', n_policies
            USING HINT =
                'Permissive policies combine with OR, so an extra one can only '
                'widen access — here, to password hashes.';
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_policy
         WHERE polrelid = 'public.user_credentials'::regclass
           AND NOT (coalesce(pg_get_expr(polqual, polrelid), '') ||
                    coalesce(pg_get_expr(polwithcheck, polrelid), ''))
               LIKE '%app.user_id%'
    ) THEN
        RAISE EXCEPTION
            '054 FAILED: a user_credentials policy does not constrain app.user_id.';
    END IF;

    -- This one failed on the first run and found the real rule: default
    -- privileges grant every new table to `ira_worker`, so withholding a GRANT
    -- withholds nothing and an explicit REVOKE is what does the work.
    IF has_table_privilege('ira_worker', 'user_credentials', 'SELECT') THEN
        RAISE EXCEPTION
            '054 FAILED: ira_worker can read user_credentials. A background job '
            'has no reason to reach a password hash. Note that NOT granting is '
            'not enough here — ALTER DEFAULT PRIVILEGES grants every new table '
            'to ira_worker, so an explicit REVOKE is required.';
    END IF;
    IF has_table_privilege('ira_worker', 'email_verification_tokens', 'SELECT') THEN
        RAISE EXCEPTION
            '054 FAILED: ira_worker can read email_verification_tokens.';
    END IF;

    SELECT relforcerowsecurity INTO forced
      FROM pg_class WHERE oid = 'public.email_verification_tokens'::regclass;
    IF NOT forced THEN
        RAISE EXCEPTION
            '054 FAILED: email_verification_tokens is not FORCE ROW LEVEL SECURITY.';
    END IF;

    SELECT count(*) INTO n_policies
      FROM pg_policy WHERE polrelid = 'public.email_verification_tokens'::regclass;
    IF n_policies <> 4 THEN
        RAISE EXCEPTION
            '054 FAILED: expected 4 policies on email_verification_tokens, found %.',
            n_policies;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_policy
         WHERE polrelid = 'public.email_verification_tokens'::regclass
           AND NOT (coalesce(pg_get_expr(polqual, polrelid), '') ||
                    coalesce(pg_get_expr(polwithcheck, polrelid), ''))
               LIKE '%app.verification_token_hash%'
    ) THEN
        RAISE EXCEPTION
            '054 FAILED: a email_verification_tokens policy does not constrain '
            'app.verification_token_hash — it would be readable without holding '
            'the token.';
    END IF;
    IF NOT has_table_privilege('ira_app', 'user_credentials', 'SELECT') THEN
        RAISE EXCEPTION
            '054 FAILED: ira_app cannot read user_credentials — the revoke above '
            'was too wide and authentication would be impossible.';
    END IF;
END
$$;

COMMIT;
