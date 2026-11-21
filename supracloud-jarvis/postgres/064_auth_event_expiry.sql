-- =============================================================================
-- 064_auth_event_expiry.sql — Phase 4 batch 06, expiring an append-only log
--
-- `061` made `auth_events` append-only for EVERY role: no DELETE policy, DELETE
-- revoked from `ira_app`, and a trigger that raises regardless of who asks. The
-- reasoning holds — "the first thing an intruder does is remove the entry that
-- shows them arriving" — and nothing here weakens it.
--
-- But a log that can never shrink is not a retention policy either. It is an
-- unbounded personal-data store that grows until somebody deletes it by hand,
-- under pressure, without a record. Phase 2's closure listed the retention job
-- as missing and this is it.
--
-- ── What this migration does NOT add ────────────────────────────────────────
--
-- No DELETE policy. No grant to `ira_app`. No SECURITY DEFINER function that
-- would let the application reach deletion through a side door — that is the
-- same capability wearing a different name, and an intruder holding the
-- application role would find it in a minute.
--
-- Expiry stays a deliberate privileged act: the table OWNER disables the
-- trigger, deletes, and re-enables, inside one transaction.
-- `ira/scripts/phase4/expire_auth_events.py` is that act written down so it is
-- performed the same way twice and leaves the same evidence each time.
--
-- ── What this migration DOES add ────────────────────────────────────────────
--
-- One new event value, `log_expired`, so the deletion is recorded IN the log
-- it truncates. Without it the operator path would be indistinguishable from
-- the tampering the trigger exists to prevent: rows gone, nothing said.
--
-- The record is written AFTER the delete, with `created_at = NOW()`, which puts
-- it outside the window that was just expired and therefore beyond the reach of
-- the pass that created it.
--
-- Rollback: rollback/064_auth_event_expiry_down.sql
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'auth_events') THEN
        RAISE EXCEPTION '064 requires auth_events (061)';
    END IF;
END $$;

-- Widening a CHECK means replacing it. The new list is the old sixteen plus
-- one; it is spelled out in full rather than assembled, because a constraint
-- built by string manipulation is a constraint nobody can read in a diff.
ALTER TABLE auth_events DROP CONSTRAINT IF EXISTS auth_events_event_check;
ALTER TABLE auth_events ADD CONSTRAINT auth_events_event_check CHECK (
    event = ANY (ARRAY[
        'sign_in_succeeded', 'sign_in_refused', 'sign_in_unavailable',
        'mfa_required', 'mfa_succeeded', 'mfa_failed',
        'recovery_code_used', 'recovery_codes_issued',
        'password_reset_requested', 'password_reset_completed',
        'session_revoked', 'sessions_revoked_all',
        'provider_linked', 'provider_unlinked', 'provider_sign_in',
        'account_link_required',
        -- New in 064. Written by the operator expiry path, never by the
        -- application: `ira_app` can INSERT it, which is deliberate — the row
        -- is a claim about a deletion, and a forged claim with no matching
        -- absence of rows is detectable, whereas a missing claim is not.
        'log_expired'
    ])
);

-- An index on the column the expiry predicate uses. Without it the operator
-- path scans the whole log to find its window, which is exactly the situation
-- (a very large table) in which somebody reaches for TRUNCATE instead.
CREATE INDEX IF NOT EXISTS auth_events_created_idx ON auth_events (created_at);

COMMENT ON INDEX auth_events_created_idx IS
    'Supports the 064 operator expiry window. See scripts/phase4/expire_auth_events.py.';

COMMIT;
