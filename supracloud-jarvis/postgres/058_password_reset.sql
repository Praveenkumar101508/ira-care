-- =============================================================================
-- 058_password_reset.sql — Phase 2 batch 9, password reset
--
-- `054` gave an account a password and a way to prove its address. This is the
-- way back when the password is the thing that was lost — and it is the single
-- most attacked endpoint in any authentication system, because it is the one
-- that hands out a credential to somebody who could not present one.
--
-- ── The same possession model as 054 and 055 ────────────────────────────────
--
-- SHA-256 of a 256-bit random token, and the policies key on
-- `app.reset_token_hash`. A connection sees exactly the row whose secret it has
-- already presented; an unbound one sees nothing. That is stronger than a user
-- predicate here for the reason `054` recorded: the legitimate reader is
-- UNAUTHENTICATED — the whole point is that they cannot sign in — so there is
-- no `app.user_id` to bind, and a policy demanding one would make the row
-- unreadable at exactly the moment the flow needs it.
--
-- SHA-256 rather than bcrypt, again matching `054` and `055` and deliberately
-- unlike `056`: the token is 256 bits of randomness, so there is no dictionary
-- to slow down, and a slow hash on an unauthenticated endpoint is a
-- denial-of-service lever on the one endpoint an attacker can already reach
-- without a credential.
--
-- ── Single use is enforced by the write, not by a read ──────────────────────
--
-- `token_is_unused` is GENERATED, so "is this token still spendable" is the
-- database's answer, and the consuming UPDATE is conditional on it. Two
-- simultaneous presentations of the same link cannot both set a password.
--
-- ── One open token per user ─────────────────────────────────────────────────
--
-- A partial unique index, matching `tenant_invitations_one_open_per_email`.
-- Requesting a reset twice must not leave two live links: a user who requests
-- again because the first mail went astray has, in their mind, replaced it —
-- and an attacker who can trigger a request should not be able to accumulate
-- valid tokens. Superseding is an UPDATE that consumes the old row, so the
-- index is a guard rather than the mechanism.
--
-- ── What this table does NOT do ─────────────────────────────────────────────
--
-- It does not record the requester's address, and it holds no tenant. A reset
-- is an account-level operation that happens BEFORE any tenant is selected —
-- the same reason `user_credentials` and `email_verification_tokens` carry no
-- tenant, pinned in `PRE_TENANT_TABLES`. Giving it one would make the row
-- unreadable to the unauthenticated caller who has to use it.
--
-- Rollback: rollback/058_password_reset_down.sql
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id           uuid        PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id      uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- SHA-256 of the token. Never the token.
    token_hash   text        NOT NULL,
    -- The address the link was sent to. Recorded for the reason `054` records
    -- it: changing the account's address must invalidate a token minted for the
    -- old one, or a link sitting in a mailbox the user no longer controls still
    -- sets the password on the account.
    email        text        NOT NULL,
    expires_at   timestamptz NOT NULL,
    consumed_at  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT NOW()
);

ALTER TABLE password_reset_tokens DROP CONSTRAINT IF EXISTS password_reset_tokens_hash_is_not_plaintext;
-- A SHA-256 hex digest is 64 characters. A token stored in the clear is a
-- bearer credential for the account, so this makes it unrepresentable rather
-- than merely discouraged — the same guard `056` puts on its bcrypt prefix.
ALTER TABLE password_reset_tokens ADD CONSTRAINT password_reset_tokens_hash_is_not_plaintext
    CHECK (token_hash ~ '^[0-9a-f]{64}$');

ALTER TABLE password_reset_tokens DROP COLUMN IF EXISTS token_is_unused;
ALTER TABLE password_reset_tokens
    ADD COLUMN token_is_unused boolean
    GENERATED ALWAYS AS (consumed_at IS NULL) STORED;

CREATE UNIQUE INDEX IF NOT EXISTS password_reset_tokens_token_hash_key
    ON password_reset_tokens (token_hash);

-- One OPEN token per user. Consumed rows are not covered, so the history stays.
CREATE UNIQUE INDEX IF NOT EXISTS password_reset_tokens_one_open_per_user
    ON password_reset_tokens (user_id) WHERE token_is_unused;

ALTER TABLE password_reset_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE password_reset_tokens FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS password_reset_token_select ON password_reset_tokens;
DROP POLICY IF EXISTS password_reset_token_insert ON password_reset_tokens;
DROP POLICY IF EXISTS password_reset_token_update ON password_reset_tokens;
DROP POLICY IF EXISTS password_reset_token_delete ON password_reset_tokens;

-- ── Two ways in, and the split between them is the whole security argument ──
--
-- Possession of the token governs everything that MINTS or SPENDS one. Reading
-- and cancelling additionally accept `app.reset_user_id`, and that asymmetry is
-- deliberate rather than convenient.
--
-- Why any widening at all: issuing a reset must retire the token already
-- outstanding for that user, or a second request leaves two live links. The
-- issuer holds the NEW secret and has never seen the old one, so it cannot
-- address that row by possession. And a `DELETE ... WHERE user_id = $1` has to
-- SEE the row to filter on it — PostgreSQL applies SELECT policies to the rows
-- a DELETE reads — so widening DELETE alone achieves exactly nothing. That was
-- tried first and silently deleted zero rows.
--
-- What the widening costs, stated plainly: an actor who can execute SQL as
-- `ira_app` with a GUC of their choosing can learn that a reset is pending for
-- a chosen user, and can cancel it. Both are real. Neither lets them USE it:
-- the stored value is a SHA-256 of a 256-bit random token, and reading a hash
-- is not holding its preimage. The cancellation is a denial of service already
-- available to anybody who can trigger a reset request for that address, since
-- that request supersedes the pending token too.
--
-- What the widening must NEVER cover is INSERT and UPDATE, and the reason is
-- concrete rather than precautionary. An UPDATE accepting `app.reset_user_id`
-- in its USING clause would let a caller point a victim's pending row at a hash
-- whose preimage THEY chose — the WITH CHECK would be satisfied, because the
-- new row's `token_hash` equals their own bound GUC. That is not an information
-- leak, it is account takeover. The assertions at the bottom of this file
-- enforce the split.
CREATE POLICY password_reset_token_select ON password_reset_tokens FOR SELECT
    USING (token_hash = NULLIF(current_setting('app.reset_token_hash', true), '')
        OR user_id    = NULLIF(current_setting('app.reset_user_id',    true), '')::uuid);

CREATE POLICY password_reset_token_insert ON password_reset_tokens FOR INSERT
    WITH CHECK (token_hash = NULLIF(current_setting('app.reset_token_hash', true), ''));

CREATE POLICY password_reset_token_update ON password_reset_tokens FOR UPDATE
    USING (token_hash = NULLIF(current_setting('app.reset_token_hash', true), ''))
    WITH CHECK (token_hash = NULLIF(current_setting('app.reset_token_hash', true), ''));

-- Retirement. Paired with the SELECT policy above: a DELETE that filters on a
-- column has to be able to read it, so these two widen together or not at all.
CREATE POLICY password_reset_token_delete ON password_reset_tokens FOR DELETE
    USING (token_hash = NULLIF(current_setting('app.reset_token_hash', true), '')
        OR user_id    = NULLIF(current_setting('app.reset_user_id',    true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON password_reset_tokens TO ira_app;

-- REQUIRED, and opt-OUT: default privileges grant every new table to
-- ira_worker. A background job holding password-reset tokens can take over any
-- account it likes.
REVOKE ALL ON password_reset_tokens FROM ira_worker;

DO $$
DECLARE
    n integer;
BEGIN
    IF to_regclass('public.password_reset_tokens') IS NULL THEN
        RAISE EXCEPTION '058: password_reset_tokens was not created';
    END IF;
    IF NOT (SELECT relforcerowsecurity FROM pg_class
             WHERE oid = 'public.password_reset_tokens'::regclass) THEN
        RAISE EXCEPTION '058: password_reset_tokens is not FORCE ROW LEVEL SECURITY';
    END IF;

    SELECT count(*) INTO n FROM pg_policy
     WHERE polrelid = 'public.password_reset_tokens'::regclass;
    IF n <> 4 THEN
        RAISE EXCEPTION
            '058: password_reset_tokens has % policies, expected exactly 4. '
            'Permissive policies combine with OR, so a fifth can only widen '
            'access to a credential that resets any password.', n;
    END IF;

    -- EVERY policy names possession. A policy that dropped it entirely would
    -- make the table reachable on a user id alone, and this table is the
    -- account.
    SELECT count(*) INTO n FROM pg_policy
     WHERE polrelid = 'public.password_reset_tokens'::regclass
       AND coalesce(pg_get_expr(polqual, polrelid), '')
        || coalesce(pg_get_expr(polwithcheck, polrelid), '')
           NOT LIKE '%app.reset_token_hash%';
    IF n <> 0 THEN
        RAISE EXCEPTION
            '058: % password_reset_tokens policy(ies) do not name '
            'app.reset_token_hash', n;
    END IF;

    -- `app.reset_user_id` reaches SELECT and DELETE — reading and cancelling —
    -- and must NEVER reach INSERT or UPDATE. This is the assertion that keeps
    -- the split from eroding, and the failure it prevents is concrete: an
    -- UPDATE accepting that GUC in its USING clause lets a caller point a
    -- victim's pending row at a hash whose preimage they chose, which is
    -- account takeover rather than an information leak.
    SELECT count(*) INTO n FROM pg_policy
     WHERE polrelid = 'public.password_reset_tokens'::regclass
       AND polcmd IN ('a', 'w')   -- INSERT, UPDATE
       AND coalesce(pg_get_expr(polqual, polrelid), '')
        || coalesce(pg_get_expr(polwithcheck, polrelid), '')
           LIKE '%app.reset_user_id%';
    IF n <> 0 THEN
        RAISE EXCEPTION
            '058: % password_reset_tokens INSERT/UPDATE policy(ies) accept '
            'app.reset_user_id. A user id is guessable where a token hash is '
            'not, so this lets a caller point somebody else''s pending reset '
            'at a hash whose preimage they chose — account takeover, not a '
            'leak. Minting and spending stay possession-only.', n;
    END IF;

    IF has_table_privilege('ira_worker', 'password_reset_tokens', 'SELECT') THEN
        RAISE EXCEPTION
            '058: ira_worker can read password_reset_tokens. Default '
            'privileges grant every new table to the worker, so the REVOKE is '
            'required — a job that can read this table can take over any '
            'account.';
    END IF;
    IF NOT has_table_privilege('ira_app', 'password_reset_tokens', 'SELECT') THEN
        RAISE EXCEPTION '058: ira_app cannot read password_reset_tokens';
    END IF;

    -- Asserted against the constraint rather than by attempting an INSERT: this
    -- script binds no GUCs, so the INSERT policy would deny first and the
    -- assertion would pass with the CHECK deleted. That mistake was made in
    -- `055` and caught by its first run.
    IF (SELECT pg_get_constraintdef(oid) FROM pg_constraint
         WHERE conrelid = 'public.password_reset_tokens'::regclass
           AND conname = 'password_reset_tokens_hash_is_not_plaintext') IS NULL THEN
        RAISE EXCEPTION '058: the plaintext guard is missing';
    END IF;

    IF to_regclass('public.password_reset_tokens_one_open_per_user') IS NULL THEN
        RAISE EXCEPTION
            '058: the one-open-token-per-user index is missing. Without it a '
            'user can accumulate live reset links.';
    END IF;
END $$;

COMMIT;
