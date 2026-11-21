-- =============================================================================
-- 061_auth_events.sql — Phase 2 batch 11, the authentication audit trail
--
-- Phase 2 built ten batches of authentication and recorded almost none of it.
-- `access_audit_log` covers authorisation — what a principal was allowed to
-- reach — and says nothing about how they came to be that principal. So there
-- is no answer to the questions asked after an incident: when did this account
-- last sign in, from where, did the second factor ever fail, was a recovery code
-- spent, was the password reset, did somebody link a provider.
--
-- ── Append-only, enforced by a trigger ──────────────────────────────────────
--
-- A log that can be edited is not evidence. `038` made `biometric_audit`
-- append-only and Phase 7 did the same for `action_receipts`; this follows both.
-- UPDATE is refused except for the ONE mutation erasure needs — clearing
-- attribution — and DELETE is refused outright.
--
-- ── What is deliberately NOT stored ─────────────────────────────────────────
--
-- No password, no token, no recovery code, no TOTP code, not even a hash of
-- one. An audit trail that records credential material turns a log reader into
-- an authenticator, and log stores are copied, shipped and retained far more
-- widely than credential tables.
--
-- The attempted ADDRESS is not stored in the clear either, and that is the
-- less obvious call. A failed-sign-in log full of addresses is two things at
-- once: a personal-data store nobody remembers to erase, and a list of every
-- address somebody probed. `subject_hash` is an HMAC of the normalised address
-- under the application secret — enough to answer "was this the same account
-- twice", useless to anybody who steals the log.
--
-- ── Nullable tenant and user, on purpose ────────────────────────────────────
--
-- Most authentication happens BEFORE a tenant is selected, and a failed
-- sign-in for an address that does not exist has no user at all. Both columns
-- are therefore nullable, and the RLS below is written so that an unattributed
-- row is readable by nobody through the application — it exists for the
-- operator and for the incident, not for a tenant to browse.
--
-- Rollback: rollback/061_auth_events_down.sql
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS auth_events (
    id           uuid        PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- Both nullable: see the header. A sign-in attempt for an unknown address
    -- has neither.
    user_id      uuid        REFERENCES users(id) ON DELETE SET NULL,
    -- SET NULL, not CASCADE. Deleting a tenant must not delete the record
    -- of how its accounts were accessed: that is history, and an
    -- organisation being wound up is precisely when somebody asks what
    -- happened in it. The event survives, unattributed to a tenant.
    tenant_id    uuid        REFERENCES tenants(id) ON DELETE SET NULL,
    -- What happened, from a fixed vocabulary. A free-text event name would let
    -- a caller write anything into the audit trail, and would make "how many
    -- failed second factors" unanswerable the first time somebody typed a
    -- variant.
    event        text        NOT NULL CHECK (event IN (
                     'sign_in_succeeded',
                     'sign_in_refused',
                     'sign_in_unavailable',
                     'mfa_required',
                     'mfa_succeeded',
                     'mfa_failed',
                     'recovery_code_used',
                     'recovery_codes_issued',
                     'password_reset_requested',
                     'password_reset_completed',
                     'session_revoked',
                     'sessions_revoked_all',
                     'provider_linked',
                     'provider_unlinked',
                     'provider_sign_in',
                     'account_link_required')),
    -- HMAC of the normalised address, never the address. NULL when the attempt
    -- carried none.
    subject_hash text,
    ip_hash      text,
    user_agent   text        NOT NULL DEFAULT '',
    -- A short, bounded note. Deliberately NOT free-form JSON: an unbounded
    -- column on an audit table is where credential material eventually lands,
    -- because somebody logs "the request" to debug something and forgets.
    detail       text        NOT NULL DEFAULT '' CHECK (length(detail) <= 200),
    created_at   timestamptz NOT NULL DEFAULT NOW()
);

ALTER TABLE auth_events DROP CONSTRAINT IF EXISTS auth_events_hashes_are_hashes;
-- Both are HMAC-SHA256 hex, or absent. The CHECK makes a raw address or a raw
-- IP unrepresentable rather than merely discouraged — the same guard `058`
-- puts on its token hash.
ALTER TABLE auth_events ADD CONSTRAINT auth_events_hashes_are_hashes
    CHECK ((subject_hash IS NULL OR subject_hash ~ '^[0-9a-f]{64}$')
       AND (ip_hash      IS NULL OR ip_hash      ~ '^[0-9a-f]{64}$'));

CREATE INDEX IF NOT EXISTS auth_events_user_idx
    ON auth_events (user_id, created_at DESC) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS auth_events_subject_idx
    ON auth_events (subject_hash, created_at DESC) WHERE subject_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS auth_events_tenant_idx
    ON auth_events (tenant_id, created_at DESC) WHERE tenant_id IS NOT NULL;

-- ── Append-only ─────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION auth_events_append_only()
RETURNS trigger
LANGUAGE plpgsql
-- REQUIRED. An unpinned search_path lets whoever can create objects in an
-- earlier schema shadow something this body resolves, and the shadowed version
-- runs with this function's rights. `060`'s trigger learned this from a guard.
SET search_path = public, pg_temp
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'auth_events rows are not deletable'
            USING HINT =
                'This is the record of how accounts were accessed. A log that '
                'can be deleted is not evidence — the first thing an intruder '
                'does is remove the entry that shows them arriving.';
    END IF;

    -- The ONE permitted mutation: erasure clearing attribution. Everything
    -- else about the row is the evidence and may not move.
    IF NEW.event        IS DISTINCT FROM OLD.event
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.detail     IS DISTINCT FROM OLD.detail THEN
        RAISE EXCEPTION
            'auth_events is append-only: only attribution may be cleared'
            USING HINT =
                'Erasure may NULL user_id, tenant_id, subject_hash and ip_hash, '
                'and blank user_agent. WHAT happened and WHEN is the evidence '
                'and does not change.';
    END IF;

    -- Attribution may only be REMOVED, never rewritten to somebody else.
    -- `tenant_id` is in this set rather than the one above because deleting a
    -- tenant SETs it NULL, and that must be permitted — but pointing an event
    -- at a DIFFERENT tenant is forging the trail.
    IF (NEW.user_id      IS NOT NULL AND NEW.user_id      IS DISTINCT FROM OLD.user_id)
       OR (NEW.tenant_id    IS NOT NULL AND NEW.tenant_id    IS DISTINCT FROM OLD.tenant_id)
       OR (NEW.subject_hash IS NOT NULL AND NEW.subject_hash IS DISTINCT FROM OLD.subject_hash)
       OR (NEW.ip_hash      IS NOT NULL AND NEW.ip_hash      IS DISTINCT FROM OLD.ip_hash) THEN
        RAISE EXCEPTION
            'auth_events attribution may be cleared, not reassigned'
            USING HINT =
                'Pointing an event at a different account is forging the audit '
                'trail, which is worse than deleting it: a deleted row is '
                'visibly missing.';
    END IF;

    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS auth_events_append_only_trg ON auth_events;
CREATE TRIGGER auth_events_append_only_trg
    BEFORE UPDATE OR DELETE ON auth_events
    FOR EACH ROW EXECUTE FUNCTION auth_events_append_only();

ALTER TABLE auth_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth_events FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS auth_event_select ON auth_events;
DROP POLICY IF EXISTS auth_event_insert ON auth_events;
DROP POLICY IF EXISTS auth_event_update ON auth_events;
DROP POLICY IF EXISTS auth_event_redaction_select ON auth_events;

-- A person reads their OWN authentication history and nobody else's. Not the
-- tenant's: "when did my colleague last sign in, and from where" is the
-- movement log `057` refused to hand out, and it is no more theirs to read
-- because it lives in an audit table.
--
-- An UNATTRIBUTED row — a failed sign-in for an address that matched no
-- account — has no `user_id`, so it matches nothing here and is readable by
-- nobody through the application. That is deliberate: those rows exist for the
-- operator and for an incident, and a tenant able to browse them could
-- enumerate which addresses had been tried.
CREATE POLICY auth_event_select ON auth_events FOR SELECT
    USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

-- Writing is what the application does on every authentication path, including
-- paths with no user and no tenant yet, so the INSERT policy cannot demand
-- either. It is not a hole: an attacker able to run arbitrary SQL as `ira_app`
-- can already do worse than write a log line, and refusing unattributed writes
-- would mean the failed-sign-in events — the ones that matter most — could not
-- be recorded at all.
CREATE POLICY auth_event_insert ON auth_events FOR INSERT
    WITH CHECK (true);

-- UPDATE exists ONLY for erasure clearing attribution, and the trigger above is
-- what makes that true. The policy scopes it to the subject's own rows so one
-- account's erasure cannot blank another's history.
--
-- `WITH CHECK (user_id IS NULL)` rather than `(true)`: the only permitted
-- update is the one that REMOVES attribution, so the policy says so instead of
-- leaving the trigger as the sole statement of it. The trigger already refuses
-- reassignment; this makes the write policy agree, and two independent
-- expressions of the same rule is the shape `061` uses everywhere else.
CREATE POLICY auth_event_update ON auth_events FOR UPDATE
    USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id IS NULL);

-- ── The policy that exists because of how PostgreSQL checks an UPDATE ───────
--
-- PostgreSQL applies SELECT policies to the NEW row of an UPDATE, not only to
-- the row it reads. A row updated out of its own SELECT visibility is refused
-- outright — SQLSTATE 42501, "new row violates row-level security policy",
-- naming no policy because the check is derived from the SELECT ones.
--
-- The redaction above sets `user_id` to NULL, and `auth_event_select` keys on
-- `user_id`. So the erasure updates the row out of visibility and PostgreSQL
-- refuses it. Measured, not assumed: without this policy
-- `test_an_erasure_de_attributes_the_history` fails with exactly that error,
-- and `erase_audit` reports the audit store as FAILED — loudly, which is the
-- one merciful part.
--
-- This is the same lesson `058` recorded from the other side. There, a
-- DELETE-only widening silently matched nothing because SELECT policies gate
-- the rows a DELETE READS. Here they gate the row an UPDATE WRITES. Both times
-- the answer is the same: a widening has to include SELECT, or it is not a
-- widening.
--
-- ── What this costs, stated plainly ─────────────────────────────────────────
--
-- Inside a transaction that has bound `app.audit_redaction` to the account it
-- is erasing, ALREADY-unattributed rows become readable — the failed sign-ins
-- for addresses that matched no account. An actor able to execute SQL as
-- `ira_app` with a chosen GUC could therefore learn how many such attempts
-- there were and which of them share a `subject_hash`.
--
-- They could not learn WHICH ADDRESS: the hash is an HMAC under the application
-- secret, which is not in the database. They could not modify or delete
-- anything: this policy is SELECT-only, `auth_event_update` still keys on
-- `app.user_id`, and there is no DELETE policy at all. The alternative was to
-- leave an erased account named in its own authentication history, which is
-- the residue erasure exists to remove.
--
-- Narrowed as far as the mechanism allows: the GUC must MATCH `app.user_id`,
-- so a caller cannot flip a generic "redaction" flag without also binding the
-- account it claims to be erasing.
CREATE POLICY auth_event_redaction_select ON auth_events FOR SELECT
    USING (user_id IS NULL
           AND NULLIF(current_setting('app.audit_redaction', true), '') IS NOT NULL
           AND NULLIF(current_setting('app.audit_redaction', true), '')
             = NULLIF(current_setting('app.user_id', true), ''));

-- No DELETE policy at all. Under FORCE RLS that means no row is deletable
-- through the application, whatever the trigger says — two independent
-- refusals, because this is the table an intruder most wants to edit.

GRANT SELECT, INSERT, UPDATE ON auth_events TO ira_app;
REVOKE DELETE ON auth_events FROM ira_app;

-- REQUIRED, and opt-OUT: default privileges grant every new table to
-- ira_worker. A job that can read this can reconstruct who signed in from
-- where; a job that can write it can forge the record.
REVOKE ALL ON auth_events FROM ira_worker;

DO $$
DECLARE
    n integer;
BEGIN
    IF to_regclass('public.auth_events') IS NULL THEN
        RAISE EXCEPTION '061: auth_events was not created';
    END IF;
    IF NOT (SELECT relforcerowsecurity FROM pg_class
             WHERE oid = 'public.auth_events'::regclass) THEN
        RAISE EXCEPTION '061: auth_events is not FORCE ROW LEVEL SECURITY';
    END IF;

    SELECT count(*) INTO n FROM pg_policy
     WHERE polrelid = 'public.auth_events'::regclass;
    IF n <> 4 THEN
        RAISE EXCEPTION
            '061: auth_events has % policies, expected exactly 4 (select, '
            'insert, update, redaction-select). A DELETE policy would make the '
            'log deletable, and there must not be one.', n;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_policy
                WHERE polrelid = 'public.auth_events'::regclass
                  AND polcmd = 'd') THEN
        RAISE EXCEPTION
            '061: auth_events has a DELETE policy. The first thing an intruder '
            'does is remove the entry that shows them arriving.';
    END IF;

    -- SELECT must key on the subject, never on the tenant. A tenant-wide read
    -- would hand every administrator their members'' sign-in history, which is
    -- the movement log `057` refused.
    SELECT count(*) INTO n FROM pg_policy
     WHERE polrelid = 'public.auth_events'::regclass
       AND polcmd = 'r'
       AND coalesce(pg_get_expr(polqual, polrelid), '') NOT LIKE '%app.user_id%';
    IF n <> 0 THEN
        RAISE EXCEPTION
            '061: the auth_events SELECT policy does not key on app.user_id';
    END IF;

    -- The redaction widening must reach ONLY rows that are already
    -- unattributed. Without `user_id IS NULL` it would hand every attributed
    -- row to anybody who bound the GUC, which is the tenant-wide read this
    -- table exists not to have.
    IF (SELECT pg_get_expr(polqual, polrelid) FROM pg_policy
         WHERE polrelid = 'public.auth_events'::regclass
           AND polname = 'auth_event_redaction_select') NOT LIKE '%user_id IS NULL%' THEN
        RAISE EXCEPTION
            '061: auth_event_redaction_select does not restrict itself to '
            'already-unattributed rows. It exists so an erasure can update a '
            'row out of its own visibility, not so a bound GUC can read '
            'somebody''s history.';
    END IF;

    -- The UPDATE may only produce an UNATTRIBUTED row. The trigger says the
    -- same thing from the other direction (attribution may be cleared, never
    -- reassigned); a policy that said `WITH CHECK (true)` would leave the
    -- trigger as the only expression of it.
    IF (SELECT pg_get_expr(polwithcheck, polrelid) FROM pg_policy
         WHERE polrelid = 'public.auth_events'::regclass
           AND polname = 'auth_event_update') NOT LIKE '%user_id IS NULL%' THEN
        RAISE EXCEPTION
            '061: the auth_events UPDATE policy permits an update that leaves '
            'the row attributed. The only permitted update is the erasure that '
            'removes attribution.';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgrelid = 'public.auth_events'::regclass
                      AND tgname = 'auth_events_append_only_trg') THEN
        RAISE EXCEPTION
            '061: the append-only trigger is missing. A log that can be edited '
            'is not evidence.';
    END IF;

    IF has_table_privilege('ira_app', 'auth_events', 'DELETE') THEN
        RAISE EXCEPTION
            '061: ira_app holds DELETE on auth_events. The grant and the '
            'missing policy are two refusals and both are wanted.';
    END IF;
    IF has_table_privilege('ira_worker', 'auth_events', 'SELECT') THEN
        RAISE EXCEPTION '061: ira_worker can read auth_events';
    END IF;
    IF NOT has_table_privilege('ira_app', 'auth_events', 'INSERT') THEN
        RAISE EXCEPTION '061: ira_app cannot write auth_events';
    END IF;

    IF (SELECT pg_get_constraintdef(oid) FROM pg_constraint
         WHERE conrelid = 'public.auth_events'::regclass
           AND conname = 'auth_events_hashes_are_hashes') IS NULL THEN
        RAISE EXCEPTION
            '061: the hash-shape guard is missing. Without it a raw address or '
            'a raw IP is storable, and an audit log full of addresses is a '
            'personal-data store nobody remembers to erase.';
    END IF;
END $$;

COMMIT;
