-- =============================================================================
-- 059_external_identities.sql — Phase 2 batch 10, federated sign-in
--
-- `034` said this table would arrive with OIDC and deliberately did not create
-- it: "External identity is unique by (issuer, subject), never by email
-- (CLOUD-ADR-0008). The dedicated `external_identities` table arrives with OIDC
-- in Phase 2 — it is NOT created here." This is that table, in the shape the
-- ADR specified.
--
-- ── The one rule this table exists to enforce ───────────────────────────────
--
-- An identity is `(issuer, subject)`. It is NEVER an email address.
--
-- Email-based linking is the repeatedly-exploited pattern CLOUD-ADR-0008 names:
-- anyone who can obtain an email claim at one provider attaches to an existing
-- account that happens to share the address. The provider need not even be
-- dishonest — an IdP that lets a user set an unverified `email` claim, or one
-- that reuses a subject after an account is deleted, is enough. So the UNIQUE
-- constraint is on `(issuer, subject)` and there is no unique constraint on
-- email here at all: two rows may carry the same address, because two people
-- may genuinely control the same address at different providers, and deciding
-- what that means is a human's job rather than a merge the system performs.
--
-- ── issuer is stored, not assumed ───────────────────────────────────────────
--
-- `subject` is only unique WITHIN an issuer. Two providers can both emit
-- subject "12345", and treating those as one identity is the same defect as
-- email linking with an extra step. The unique index therefore names both, and
-- `issuer` is the URL the provider publishes rather than a friendly name: two
-- deployments of the same product are two issuers.
--
-- ── Why there is no credential column ───────────────────────────────────────
--
-- Nothing here authenticates anybody. The provider does that, and this table
-- records WHO the result maps to. There is no secret to leak, which is why this
-- table's policies can be laxer than `058`'s without being weaker — the worst a
-- reader learns is that an account exists at a provider, and `users` already
-- tells them that.
--
-- ── Access model: possession of the identity ────────────────────────────────
--
-- Sign-in reads this table BEFORE any user is known — resolving `(issuer,
-- subject)` to a user is the whole point — so a policy keyed on `app.user_id`
-- would be unreadable at exactly the moment it is needed, the same bind `054`
-- and `058` hit. The policy therefore keys on the identity the caller has just
-- proved at the provider: `app.oidc_issuer` and `app.oidc_subject`. A
-- connection sees exactly the row for the identity it has presented, and an
-- unbound one sees nothing.
--
-- The account's OWN view is separate and keys on `app.user_id`, because listing
-- and unlinking are done by an authenticated person about themselves. Those are
-- two different questions, so they are two different policies — combined with
-- OR, which is correct here: a caller satisfying either genuinely may see the
-- row, and neither predicate is a widening of the other.
--
-- Rollback: rollback/059_external_identities_down.sql
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS external_identities (
    id                    uuid        PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id               uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- The provider's issuer URL, verbatim. Not a friendly name: two
    -- deployments of the same product are two issuers.
    issuer                text        NOT NULL,
    -- The provider's subject identifier. Unique only WITHIN an issuer.
    subject               text        NOT NULL,
    -- A human label for the provider ("google", "okta"). Presentation only —
    -- nothing authorises on it.
    provider              text        NOT NULL DEFAULT '',
    -- The address the provider asserted, recorded for display and for support.
    -- It is NOT an identity and nothing links on it. Deliberately nullable:
    -- plenty of providers assert no email at all.
    claimed_email         text,
    -- Whether the PROVIDER said it had verified that address. Recorded so the
    -- distinction survives; still never a linking key.
    claimed_email_verified boolean    NOT NULL DEFAULT FALSE,
    created_at            timestamptz NOT NULL DEFAULT NOW(),
    last_authenticated_at timestamptz
);

ALTER TABLE external_identities DROP CONSTRAINT IF EXISTS external_identities_issuer_not_blank;
ALTER TABLE external_identities ADD CONSTRAINT external_identities_issuer_not_blank
    CHECK (length(btrim(issuer)) > 0);

ALTER TABLE external_identities DROP CONSTRAINT IF EXISTS external_identities_subject_not_blank;
ALTER TABLE external_identities ADD CONSTRAINT external_identities_subject_not_blank
    CHECK (length(btrim(subject)) > 0);

-- THE constraint. An identity is (issuer, subject) and nothing else.
CREATE UNIQUE INDEX IF NOT EXISTS external_identities_issuer_subject_key
    ON external_identities (issuer, subject);

-- One identity per issuer per user: a person cannot hold two Google accounts
-- against the same IRA account, because "which one signed in" then has no
-- answer and unlinking one leaves a second nobody expected.
CREATE UNIQUE INDEX IF NOT EXISTS external_identities_one_per_issuer_per_user
    ON external_identities (user_id, issuer);

CREATE INDEX IF NOT EXISTS external_identities_user_idx
    ON external_identities (user_id);

-- DELIBERATELY ABSENT: any unique index on `claimed_email`. Two rows may carry
-- the same address, because two people may genuinely control the same address
-- at different providers. Making that unrepresentable would be enforcing
-- exactly the email-is-identity rule this table exists to refuse.

ALTER TABLE external_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE external_identities FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS external_identity_select ON external_identities;
DROP POLICY IF EXISTS external_identity_insert ON external_identities;
DROP POLICY IF EXISTS external_identity_update ON external_identities;
DROP POLICY IF EXISTS external_identity_delete ON external_identities;

-- Two legitimate readers, so one policy naming both. Sign-in has proved an
-- identity at a provider and does not yet know the user; the account owner
-- knows who they are and is asking about themselves.
CREATE POLICY external_identity_select ON external_identities FOR SELECT
    USING ((issuer  = NULLIF(current_setting('app.oidc_issuer',  true), '')
        AND subject = NULLIF(current_setting('app.oidc_subject', true), ''))
        OR user_id  = NULLIF(current_setting('app.user_id', true), '')::uuid);

-- Writing a link requires BOTH: the identity being linked AND the user it is
-- being linked to. That is the security property of the whole batch expressed
-- as a policy — a caller who has proved an identity cannot attach it to a user
-- of their choosing, and a caller acting as a user cannot invent an identity
-- they have not proved.
CREATE POLICY external_identity_insert ON external_identities FOR INSERT
    WITH CHECK (issuer  = NULLIF(current_setting('app.oidc_issuer',  true), '')
            AND subject = NULLIF(current_setting('app.oidc_subject', true), '')
            AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

-- UPDATE exists for `last_authenticated_at` and the recorded claims. The
-- identity columns are pinned by a trigger below, because a policy cannot
-- express "these columns may not change".
CREATE POLICY external_identity_update ON external_identities FOR UPDATE
    USING (issuer  = NULLIF(current_setting('app.oidc_issuer',  true), '')
       AND subject = NULLIF(current_setting('app.oidc_subject', true), ''))
    WITH CHECK (issuer  = NULLIF(current_setting('app.oidc_issuer',  true), '')
            AND subject = NULLIF(current_setting('app.oidc_subject', true), ''));

-- Unlinking is the account owner's operation, so it keys on the user.
CREATE POLICY external_identity_delete ON external_identities FOR DELETE
    USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

-- ── The identity columns are immutable ──────────────────────────────────────
--
-- A policy cannot say "you may update this row but not these columns", and
-- `user_id`, `issuer` and `subject` are exactly the columns whose mutation is
-- account takeover: repointing an existing link at another user hands that
-- account to whoever holds the identity. So a trigger refuses it outright.
CREATE OR REPLACE FUNCTION external_identity_pin_key()
RETURNS trigger
LANGUAGE plpgsql
-- REQUIRED, and caught by `test_every_function_pins_its_search_path` rather
-- than remembered. An unpinned search_path lets whoever can create objects in
-- an earlier schema shadow a function or operator this body resolves, and the
-- shadowed version then runs with this function's rights. `pg_temp` is last
-- because a temporary schema is writable by any session.
SET search_path = public, pg_temp
AS $$
BEGIN
    IF NEW.user_id IS DISTINCT FROM OLD.user_id
       OR NEW.issuer IS DISTINCT FROM OLD.issuer
       OR NEW.subject IS DISTINCT FROM OLD.subject THEN
        RAISE EXCEPTION
            'external_identities: user_id, issuer and subject are immutable'
            USING HINT =
                'Repointing a link is account takeover: it hands the target '
                'account to whoever controls the identity. Delete the link and '
                'create a new one, which requires proving both sides again.';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS external_identity_pin_key_trg ON external_identities;
CREATE TRIGGER external_identity_pin_key_trg
    BEFORE UPDATE ON external_identities
    FOR EACH ROW EXECUTE FUNCTION external_identity_pin_key();

GRANT SELECT, INSERT, UPDATE, DELETE ON external_identities TO ira_app;

-- REQUIRED, and opt-OUT: default privileges grant every new table to
-- ira_worker. A background job has no business reading who signs in from where.
REVOKE ALL ON external_identities FROM ira_worker;

DO $$
DECLARE
    n integer;
BEGIN
    IF to_regclass('public.external_identities') IS NULL THEN
        RAISE EXCEPTION '059: external_identities was not created';
    END IF;
    IF NOT (SELECT relforcerowsecurity FROM pg_class
             WHERE oid = 'public.external_identities'::regclass) THEN
        RAISE EXCEPTION '059: external_identities is not FORCE ROW LEVEL SECURITY';
    END IF;

    SELECT count(*) INTO n FROM pg_policy
     WHERE polrelid = 'public.external_identities'::regclass;
    IF n <> 4 THEN
        RAISE EXCEPTION
            '059: external_identities has % policies, expected exactly 4', n;
    END IF;

    -- The INSERT policy must name the user AND the identity. One that named
    -- only the identity would let a proved sign-in attach itself to any account;
    -- one that named only the user would let an account claim an identity it
    -- never proved. Both are account takeover from opposite directions.
    SELECT count(*) INTO n FROM pg_policy
     WHERE polrelid = 'public.external_identities'::regclass
       AND polcmd = 'a'
       AND (coalesce(pg_get_expr(polwithcheck, polrelid), '') NOT LIKE '%app.user_id%'
         OR coalesce(pg_get_expr(polwithcheck, polrelid), '') NOT LIKE '%app.oidc_issuer%'
         OR coalesce(pg_get_expr(polwithcheck, polrelid), '') NOT LIKE '%app.oidc_subject%');
    IF n <> 0 THEN
        RAISE EXCEPTION
            '059: the external_identities INSERT policy does not require BOTH '
            'the proved identity and the target user. Naming only one side '
            'lets a caller link an identity to an account they do not control, '
            'or claim an identity they never proved.';
    END IF;

    -- The identity key must be unique on (issuer, subject) and NOT on email.
    IF to_regclass('public.external_identities_issuer_subject_key') IS NULL THEN
        RAISE EXCEPTION
            '059: the (issuer, subject) unique index is missing. Without it the '
            'same provider identity can map to several accounts.';
    END IF;

    SELECT count(*) INTO n FROM pg_index i
      JOIN pg_class c ON c.oid = i.indexrelid
     WHERE i.indrelid = 'public.external_identities'::regclass
       AND i.indisunique
       AND pg_get_indexdef(i.indexrelid) LIKE '%claimed_email%';
    IF n <> 0 THEN
        RAISE EXCEPTION
            '059: a UNIQUE index covers claimed_email. Email is not an identity '
            '(CLOUD-ADR-0008) — two people may control the same address at '
            'different providers, and enforcing otherwise is the '
            'account-merging hazard this table exists to refuse.';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgrelid = 'public.external_identities'::regclass
                      AND tgname = 'external_identity_pin_key_trg') THEN
        RAISE EXCEPTION
            '059: the immutability trigger is missing. Without it an UPDATE can '
            'repoint a link at another user, which hands that account to '
            'whoever controls the identity.';
    END IF;

    IF has_table_privilege('ira_worker', 'external_identities', 'SELECT') THEN
        RAISE EXCEPTION
            '059: ira_worker can read external_identities. Default privileges '
            'grant every new table to the worker, so the REVOKE is required.';
    END IF;
    IF NOT has_table_privilege('ira_app', 'external_identities', 'SELECT') THEN
        RAISE EXCEPTION '059: ira_app cannot read external_identities';
    END IF;
END $$;

COMMIT;
