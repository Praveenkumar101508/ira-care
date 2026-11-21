-- =============================================================================
-- 065_totp_enrolment_is_per_tenant.sql — Phase 4 batch 08
--
-- A user who belongs to two tenants can enrol a second factor in ONE of them.
-- In the other they are not asked for a factor, and cannot turn one on.
--
-- ── How it got here ─────────────────────────────────────────────────────────
--
-- `036` closed R-26 by retiring `totp_secrets PRIMARY KEY (username)` — a
-- GLOBAL unique on a name customers choose, which meant two tenants could not
-- both hold a user called `alice`. That was right.
--
-- In the same migration it created:
--
--     CREATE UNIQUE INDEX totp_secrets_user_key
--         ON totp_secrets (user_id) WHERE user_id IS NOT NULL AND revoked_at IS NULL;
--
-- and its comment says why: *"one enrolment per user"*. At the time that was
-- the model — one person, one account, one enrolment.
--
-- It stopped being the model two batches later. `035` gave a user many
-- `tenant_memberships`; `055` made invitations the way that happens; and
-- `cloud_login.py` raises `TenantSelectionRequired` rather than refusing,
-- precisely so a member of several organisations can sign in. Nothing revisited
-- the index, because nothing connected "a user may now be in two tenants" to
-- "this index says they may have one enrolment."
--
-- So R-26 was closed by replacing a global unique with a narrower global
-- unique, and the narrower one outlived the assumption it rested on.
--
-- ── What is wrong today, stated exactly ─────────────────────────────────────
--
-- Measured, not inferred (see tests/readiness/test_phase_4_acceptance.py):
--
--   * enrolling the same `user_id` in a second tenant is REFUSED by this index;
--   * `check_login_second_factor` under a principal bound to that second tenant
--     returns `not_enrolled`, because the row is invisible through
--     `totp_secrets`' RLS (`user_id = app.user_id AND tenant_id = app.tenant_id`);
--   * so login into the second tenant proceeds with NO second factor.
--
-- Everything except the index is already per-tenant: the column, the RLS
-- policies, and `user_mfa_keys`, which `030`/D-17 keyed per (tenant, user)
-- deliberately. The index is the one thing that is not.
--
-- ── What this migration does NOT decide ─────────────────────────────────────
--
-- It makes per-tenant enrolment POSSIBLE. It does not decide whether a user who
-- has enrolled in tenant A should be COMPELLED to present a factor in tenant B
-- before enrolling there. That is a policy question with two defensible answers
-- and different security and product consequences — recorded as **D-20** in
-- `OPEN_DECISIONS.md` rather than settled by a migration.
--
-- Widening, so no existing row can violate it: any set of rows unique on
-- `user_id` is unique on `(tenant_id, user_id)`.
--
-- Rollback: rollback/065_totp_enrolment_is_per_tenant_down.sql
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'totp_secrets' AND column_name = 'tenant_id') THEN
        RAISE EXCEPTION '065 requires totp_secrets.tenant_id (036)';
    END IF;
END $$;

-- The new index goes on FIRST. Dropping the old one first would leave a window
-- — inside this transaction, but a window in the written order all the same —
-- in which nothing constrains enrolments at all, and a reader auditing this
-- file should not have to reason about whether that matters.
CREATE UNIQUE INDEX IF NOT EXISTS totp_secrets_tenant_user_key
    ON totp_secrets (tenant_id, user_id)
    WHERE user_id IS NOT NULL AND revoked_at IS NULL;

DROP INDEX IF EXISTS totp_secrets_user_key;

COMMENT ON INDEX totp_secrets_tenant_user_key IS
    'One live enrolment per user PER TENANT. 036 scoped this to the user alone, '
    'when a user could only be in one tenant; 035 and 055 made that false. See D-20 '
    'for whether an enrolment in one tenant should compel a factor in another.';

COMMIT;
