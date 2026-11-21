-- =============================================================================
-- rollback/055_invitations_down.sql — reverse 055_invitations.sql
--
-- Refuses while any invitation has been ACCEPTED.
--
-- ── Why acceptance is the line, and not simply "any row" ────────────────────
--
-- Dropping this table does not remove anybody's access: acceptance creates a
-- row in `tenant_memberships`, and that is where authority lives. So the loss
-- is not access — it is the RECORD of how somebody came to have it.
--
-- That record is the only answer to "who let this person into the tenant, and
-- when". `tenant_memberships.invited_by` survives, but the invitation carries
-- the address the invitation was sent to and the moment it was redeemed, which
-- is what distinguishes "the admin added them" from "somebody redeemed a link
-- that had been sitting in a mailbox for a week". Discarding that silently
-- while the membership it produced keeps working is the kind of quiet gap an
-- audit finds later and cannot reconstruct.
--
-- OPEN invitations are different and are NOT a reason to refuse. An unredeemed
-- invitation grants nothing; dropping the table simply means those links stop
-- working, which is the same outcome as revoking them. Refusing on open rows
-- would make this script unusable on any live deployment for no safety gain.
--
-- The count is taken with FORCE lifted, because this script binds no GUCs — and
-- under FORCE RLS a table read on an unbound connection matches ZERO rows
-- silently. That is the P2-05 defect exactly: the guard would pass while every
-- accepted invitation was about to be destroyed. Lifted deliberately for the
-- count, restored immediately.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_accepted bigint;
BEGIN
    IF to_regclass('public.tenant_invitations') IS NULL THEN
        RETURN;   -- already reversed; nothing to do
    END IF;

    EXECUTE 'ALTER TABLE tenant_invitations NO FORCE ROW LEVEL SECURITY';
    SELECT count(*) INTO n_accepted
      FROM tenant_invitations WHERE accepted_at IS NOT NULL;
    EXECUTE 'ALTER TABLE tenant_invitations FORCE ROW LEVEL SECURITY';

    IF n_accepted > 0 THEN
        RAISE EXCEPTION
            '055-down REFUSING: % invitation(s) have been accepted.', n_accepted
            USING HINT =
                'Those rows are the record of how their holders came to be '
                'members of a tenant. The memberships themselves survive in '
                'tenant_memberships, so dropping this table does not remove '
                'anybody''s access — it removes the audit trail for access '
                'that keeps working, which cannot be reconstructed. Open and '
                'revoked invitations do not block this; accepted ones do. If '
                'you genuinely intend it, archive those rows first and record '
                'why.';
    END IF;
END $$;

DROP POLICY IF EXISTS tenant_invitation_bearer_select ON tenant_invitations;
DROP POLICY IF EXISTS tenant_invitation_delete ON tenant_invitations;
DROP POLICY IF EXISTS tenant_invitation_update ON tenant_invitations;
DROP POLICY IF EXISTS tenant_invitation_insert ON tenant_invitations;
DROP POLICY IF EXISTS tenant_invitation_select ON tenant_invitations;

DROP INDEX IF EXISTS tenant_invitations_tenant_idx;
DROP INDEX IF EXISTS tenant_invitations_one_open_per_email;
DROP INDEX IF EXISTS tenant_invitations_token_hash_key;

DROP TABLE IF EXISTS tenant_invitations;

-- The display name goes with it. It is derivable again from
-- `organisations.name` on re-apply, so nothing is lost that the backfill
-- cannot reconstruct.
ALTER TABLE tenants DROP COLUMN IF EXISTS display_name;

DO $$
BEGIN
    IF to_regclass('public.tenant_invitations') IS NOT NULL THEN
        RAISE EXCEPTION '055-down: tenant_invitations still exists';
    END IF;
    -- 055 creates nothing else, so nothing else may have gone with it. Asserted
    -- because a rollback that quietly took a neighbouring table with it is the
    -- failure this pair of scripts exists to make impossible.
    IF to_regclass('public.tenant_memberships') IS NULL THEN
        RAISE EXCEPTION
            '055-down destroyed tenant_memberships. The invitation table is '
            'the only thing 055 created.';
    END IF;
    IF to_regclass('public.user_credentials') IS NULL THEN
        RAISE EXCEPTION '055-down destroyed user_credentials';
    END IF;
END $$;

COMMIT;
