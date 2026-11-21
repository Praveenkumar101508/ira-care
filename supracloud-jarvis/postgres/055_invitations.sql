-- =============================================================================
-- 055_invitations.sql — Phase 2 batch 5, tenant invitations
--
-- Batch 4 founds a tenant with exactly one member and no way to add a second.
-- This is the table that turns a tenant into an organisation.
--
-- ── Only ONE new table ──────────────────────────────────────────────────────
--
-- The membership lifecycle already exists and is not touched. `035` gave
-- `tenant_memberships` the full set — `invited`, `active`, `suspended`,
-- `removed` — plus `invited_by`, `joined_at`, `suspended_at` and `removed_at`,
-- and `040` added the generated `membership_grants_access`. Adding a parallel
-- lifecycle here would be a second answer to "may this person act", which is
-- the shape of defect this schema keeps removing rather than adding.
--
-- Three existing constraints do work this migration therefore does NOT repeat:
--
--   * `tenant_memberships_tenant_id_user_id_key` — UNIQUE (tenant_id, user_id).
--     Two concurrent acceptances of the same invitation cannot create two
--     memberships; the database refuses the second, so the property is
--     structural rather than a race the application has to win.
--   * `tenant_memberships_one_primary_owner` — at most one ACTIVE primary
--     owner per tenant. An invitation cannot mint a rival founder.
--   * `tenant_memberships_status_revoked_agree` — status and revocation cannot
--     disagree.
--
-- ── The invitable roles exclude `primary_owner`, in the schema ──────────────
--
-- `tenant_memberships.role` permits `primary_owner`, because a tenant has one.
-- An INVITATION may not name it. Enforcing that here rather than only in
-- application code means an invitation row asserting `primary_owner` is not a
-- thing that can exist to be accepted later — the founder protection holds even
-- if a future caller forgets to check.
--
-- ── Isolation: tenant-scoped, plus ONE possession-scoped read ───────────────
--
-- Creating, listing and revoking invitations are done by a member of the
-- tenant, so those are ordinary tenant-scoped operations on `app.tenant_id`.
--
-- Acceptance is not. The invitee is authenticated but is NOT yet a member, so
-- there is no tenant to bind at the moment they present their token — and the
-- tenant they will join has to be READ from the invitation rather than supplied
-- by them, or the invitation becomes a way to name a tenant.
--
-- So there is one extra policy, for SELECT only, keyed on what the caller has
-- already presented:
--
--     token_hash = current_setting('app.invitation_token_hash')
--
-- exactly as `054` does for email verification, and for the same reason: a
-- connection may see the single row whose secret it holds, an unbound one sees
-- none, and a query that forgot its WHERE clause still enumerates nothing.
-- `invitation_connection` then binds `app.tenant_id` FROM THAT ROW, after which
-- the ordinary tenant policies govern every write.
--
-- The token itself is never stored — only its SHA-256, as `user_sessions` and
-- `054` both do, and for the reason recorded there: a table holding usable
-- tokens turns a read of that table into account takeover. Here it would be
-- unauthorised admission to somebody else's organisation.
--
-- Rollback: rollback/055_invitations_down.sql
-- =============================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 0. A tenant display name that is readable BEFORE a tenant is selected
--
-- Tenant selection has to show the user which organisations they belong to,
-- and it happens before any tenant is bound — so the name must come from a
-- table with no RLS. `organisations.name` is the customer's name and is
-- tenant-scoped, which is correct for it and useless here: an unbound read
-- returns nothing.
--
-- `tenants.name` cannot be used either. It is globally UNIQUE (pinned by
-- `data/__init__.py`'s `ON CONFLICT (name)`), so batch 4 deliberately fills it
-- with `cloud-<uuid>` rather than customer text — feeding a customer-supplied
-- name to a globally unique column turns a shared name into a failure that
-- discloses the other customer.
--
-- So: a separate, deliberately NON-UNIQUE column. Two organisations may both
-- be called "Acme", exactly as 033 says they may.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS display_name text;

-- Backfill from the organisation, which is where batch 4 put the customer's
-- text. `organisations` is FORCE RLS and this script binds no GUCs, so an
-- unbound read matches ZERO rows silently — the backfill would appear to
-- succeed having copied nothing. That is the P2-05 shape exactly, so FORCE is
-- lifted deliberately for the statement and restored immediately.
DO $$
BEGIN
    IF to_regclass('public.organisations') IS NOT NULL THEN
        EXECUTE 'ALTER TABLE organisations NO FORCE ROW LEVEL SECURITY';
        UPDATE tenants t SET display_name = o.name
          FROM organisations o
         WHERE o.tenant_id = t.id AND t.display_name IS NULL;
        EXECUTE 'ALTER TABLE organisations FORCE ROW LEVEL SECURITY';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS tenant_invitations (
    id           uuid        PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- Derived from the bound transaction, never passed. Same shape as
    -- `organisations` and `workspaces` (033/039), so the value the row carries
    -- and the value the policy checks are one expression rather than two
    -- copies that can disagree.
    tenant_id    uuid        NOT NULL
                 DEFAULT (NULLIF(current_setting('app.tenant_id', true), ''))::uuid
                 REFERENCES tenants(id) ON DELETE CASCADE,
    -- The identity invited. An invitation is bound to an ADDRESS, not merely to
    -- a tenant: `accept_invitation` requires the accepting account's verified
    -- address to match, so a leaked link cannot be redeemed by whoever finds it.
    email        text        NOT NULL,
    -- The same generated normalisation `users` uses, so "does this address
    -- match" has one definition. `normalise_email` lowercases only the domain,
    -- because the local part is case-sensitive.
    normalised_email text GENERATED ALWAYS AS (normalise_email(email)) STORED,
    -- NOT `primary_owner` — see the header.
    role         text        NOT NULL DEFAULT 'member',
    token_hash   text        NOT NULL,
    invited_by   uuid        NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    expires_at   timestamptz NOT NULL,
    accepted_at  timestamptz,
    accepted_by  uuid        REFERENCES users(id) ON DELETE SET NULL,
    revoked_at   timestamptz,
    revoked_by   uuid        REFERENCES users(id) ON DELETE SET NULL,
    created_at   timestamptz NOT NULL DEFAULT NOW()
);

ALTER TABLE tenant_invitations DROP CONSTRAINT IF EXISTS tenant_invitations_role_check;
-- Every role `tenant_memberships.role` permits EXCEPT `primary_owner`. Listed
-- rather than expressed as a subtraction so that a role added to memberships
-- later is not silently invitable.
ALTER TABLE tenant_invitations ADD CONSTRAINT tenant_invitations_role_check
    CHECK (role IN ('owner', 'admin', 'member', 'viewer',
                    'billing_admin', 'security_admin'));

ALTER TABLE tenant_invitations DROP CONSTRAINT IF EXISTS tenant_invitations_token_hash_shape;
-- SHA-256 hex, and nothing else. The column cannot hold a plaintext token by
-- accident: a `secrets.token_urlsafe(32)` value is not 64 hex characters.
ALTER TABLE tenant_invitations ADD CONSTRAINT tenant_invitations_token_hash_shape
    CHECK (token_hash ~ '^[0-9a-f]{64}$');

ALTER TABLE tenant_invitations DROP CONSTRAINT IF EXISTS tenant_invitations_expiry_after_creation;
ALTER TABLE tenant_invitations ADD CONSTRAINT tenant_invitations_expiry_after_creation
    CHECK (expires_at > created_at);

ALTER TABLE tenant_invitations DROP CONSTRAINT IF EXISTS tenant_invitations_acceptance_is_complete;
-- Accepted means both columns, or neither. A row recording WHEN it was accepted
-- but not BY WHOM cannot be audited, and one recording who but not when cannot
-- be expired.
ALTER TABLE tenant_invitations ADD CONSTRAINT tenant_invitations_acceptance_is_complete
    CHECK ((accepted_at IS NULL) = (accepted_by IS NULL));

ALTER TABLE tenant_invitations DROP CONSTRAINT IF EXISTS tenant_invitations_not_both_outcomes;
-- An invitation cannot be both accepted and revoked. Without this, revoking an
-- already-accepted invitation would produce a row that reads as either.
ALTER TABLE tenant_invitations ADD CONSTRAINT tenant_invitations_not_both_outcomes
    CHECK (accepted_at IS NULL OR revoked_at IS NULL);

-- Generated, so "is this invitation still open" is the database's answer and
-- cannot be re-implemented differently by a caller — the same reasoning that
-- made `membership_grants_access` generated in 040.
ALTER TABLE tenant_invitations DROP COLUMN IF EXISTS invitation_is_open;
ALTER TABLE tenant_invitations
    ADD COLUMN invitation_is_open boolean
    GENERATED ALWAYS AS (accepted_at IS NULL AND revoked_at IS NULL) STORED;

CREATE UNIQUE INDEX IF NOT EXISTS tenant_invitations_token_hash_key
    ON tenant_invitations (token_hash);

-- At most one OPEN invitation per address per tenant. Partial, so a superseded
-- or accepted invitation does not block a new one — and so re-inviting somebody
-- who declined is possible without deleting audit history.
CREATE UNIQUE INDEX IF NOT EXISTS tenant_invitations_one_open_per_email
    ON tenant_invitations (tenant_id, normalised_email)
    WHERE invitation_is_open;

CREATE INDEX IF NOT EXISTS tenant_invitations_tenant_idx
    ON tenant_invitations (tenant_id, created_at DESC);

ALTER TABLE tenant_invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_invitations FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_invitation_select ON tenant_invitations;
DROP POLICY IF EXISTS tenant_invitation_insert ON tenant_invitations;
DROP POLICY IF EXISTS tenant_invitation_update ON tenant_invitations;
DROP POLICY IF EXISTS tenant_invitation_delete ON tenant_invitations;
DROP POLICY IF EXISTS tenant_invitation_bearer_select ON tenant_invitations;

CREATE POLICY tenant_invitation_select ON tenant_invitations FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY tenant_invitation_insert ON tenant_invitations FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY tenant_invitation_update ON tenant_invitations FOR UPDATE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY tenant_invitation_delete ON tenant_invitations FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- The acceptance read, and ONLY a read. The invitee has no tenant bound yet, so
-- the four policies above match nothing for them; this one matches exactly the
-- row whose token they already hold. It is deliberately SELECT-only: every
-- write during acceptance happens after `invitation_connection` has bound
-- `app.tenant_id` from this row, so the tenant policies govern them.
--
-- Permissive policies combine with OR, so this WIDENS access — which is why it
-- is keyed on a 256-bit secret and nothing else. An unbound connection has NULL
-- and matches no row.
CREATE POLICY tenant_invitation_bearer_select ON tenant_invitations FOR SELECT
    USING (token_hash = NULLIF(current_setting('app.invitation_token_hash', true), ''));

GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_invitations TO ira_app;

-- REQUIRED, not decorative. `provisioning/001_database_roles.sql` sets ALTER
-- DEFAULT PRIVILEGES granting `arwd` on every new table to BOTH `ira_app` and
-- `ira_worker`, so privilege here is opt-OUT. A background job has no reason to
-- hold invitation tokens, and 054 found this exact thing by asserting it.
REVOKE ALL ON tenant_invitations FROM ira_worker;

-- ─────────────────────────────────────────────────────────────────────────────
-- Assert the end state
--
-- The statements above say what was issued; these say what is true afterwards.
-- 054's `ira_worker` REVOKE was found by an assertion like this failing, not by
-- reading the provisioning script.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    n integer;
BEGIN
    IF to_regclass('public.tenant_invitations') IS NULL THEN
        RAISE EXCEPTION '055: tenant_invitations was not created';
    END IF;

    IF NOT (SELECT relforcerowsecurity FROM pg_class
             WHERE oid = 'public.tenant_invitations'::regclass) THEN
        RAISE EXCEPTION '055: tenant_invitations is not FORCE ROW LEVEL SECURITY';
    END IF;

    SELECT count(*) INTO n FROM pg_policy
     WHERE polrelid = 'public.tenant_invitations'::regclass;
    IF n <> 5 THEN
        RAISE EXCEPTION
            '055: tenant_invitations has % policies, expected exactly 5 '
            '(4 tenant-scoped + 1 bearer SELECT). Permissive policies combine '
            'with OR, so an extra one can only widen access to invitation '
            'tokens.', n;
    END IF;

    -- Every policy must constrain SOMETHING. Four on the tenant, one on the
    -- token hash; a policy constraining neither would be a USING (true) that
    -- the OR would let decide the outcome.
    SELECT count(*) INTO n FROM pg_policy
     WHERE polrelid = 'public.tenant_invitations'::regclass
       AND coalesce(pg_get_expr(polqual, polrelid), '')
        || coalesce(pg_get_expr(polwithcheck, polrelid), '')
           NOT LIKE '%app.tenant_id%'
       AND coalesce(pg_get_expr(polqual, polrelid), '')
        || coalesce(pg_get_expr(polwithcheck, polrelid), '')
           NOT LIKE '%app.invitation_token_hash%';
    IF n <> 0 THEN
        RAISE EXCEPTION
            '055: % tenant_invitations policy(ies) constrain neither '
            'app.tenant_id nor app.invitation_token_hash', n;
    END IF;

    -- Only ONE policy may be keyed on possession, and it must be SELECT.
    -- A bearer-keyed UPDATE or DELETE would let the holder of a link mutate
    -- the row that authorises them.
    SELECT count(*) INTO n FROM pg_policy
     WHERE polrelid = 'public.tenant_invitations'::regclass
       AND coalesce(pg_get_expr(polqual, polrelid), '')
        || coalesce(pg_get_expr(polwithcheck, polrelid), '')
           LIKE '%app.invitation_token_hash%'
       AND polcmd <> 'r';
    IF n <> 0 THEN
        RAISE EXCEPTION
            '055: % bearer-keyed policy(ies) on tenant_invitations are not '
            'SELECT. Possession of a link must not authorise a write.', n;
    END IF;

    IF has_table_privilege('ira_worker', 'tenant_invitations', 'SELECT') THEN
        RAISE EXCEPTION
            '055: ira_worker can read tenant_invitations. Default privileges '
            'grant every new table to the worker, so the REVOKE is required '
            'rather than belt-and-braces.';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'tenants' AND column_name = 'display_name') THEN
        RAISE EXCEPTION '055: tenants.display_name is missing';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                JOIN pg_attribute a ON a.attrelid = i.indrelid
                                   AND a.attnum = ANY(i.indkey)
               WHERE i.indrelid = 'public.tenants'::regclass
                 AND i.indisunique AND a.attname = 'display_name') THEN
        RAISE EXCEPTION
            '055: tenants.display_name has a UNIQUE index. It holds customer '
            'text, so uniqueness would make two customers with the same name '
            'a failure that discloses the other one.';
    END IF;

    IF NOT has_table_privilege('ira_app', 'tenant_invitations', 'SELECT') THEN
        RAISE EXCEPTION '055: ira_app cannot read tenant_invitations';
    END IF;

    -- The founder protection, asserted against the CONSTRAINT DEFINITION.
    --
    -- The first version of this attempted the INSERT and expected a
    -- check_violation. It failed with `new row violates row-level security
    -- policy` instead: this script binds no GUCs, so the INSERT policy denies
    -- before the CHECK is ever evaluated. That refusal is correct but it is the
    -- WRONG refusal — the assertion would have passed identically with the role
    -- constraint deleted, proving only that RLS is on. A guard that cannot
    -- distinguish the thing it guards from an unrelated denial is not a guard.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.tenant_invitations'::regclass
           AND conname  = 'tenant_invitations_role_check'
    ) THEN
        RAISE EXCEPTION '055: the invitable-role constraint is missing';
    END IF;

    IF (SELECT pg_get_constraintdef(oid) FROM pg_constraint
         WHERE conrelid = 'public.tenant_invitations'::regclass
           AND conname = 'tenant_invitations_role_check') LIKE '%primary_owner%'
    THEN
        RAISE EXCEPTION
            '055: primary_owner is an invitable role. An invitation must not '
            'be able to mint a rival founder — tenant_memberships_one_primary_'
            'owner would then refuse the acceptance, which surfaces as a '
            'mysterious failure at redemption rather than a refusal at issue.';
    END IF;

    -- And the positive half: the constraint must still permit the roles an
    -- invitation is FOR. Excluding primary_owner by excluding everything would
    -- satisfy the assertion above and break invitations entirely.
    IF (SELECT count(*) FROM unnest(ARRAY['owner','admin','member','viewer',
                                          'billing_admin','security_admin']) r
         WHERE (SELECT pg_get_constraintdef(oid) FROM pg_constraint
                 WHERE conrelid = 'public.tenant_invitations'::regclass
                   AND conname = 'tenant_invitations_role_check')
               NOT LIKE '%' || r || '%') <> 0 THEN
        RAISE EXCEPTION
            '055: the invitable-role constraint no longer permits every '
            'non-founder tenant role';
    END IF;
END $$;

COMMIT;
