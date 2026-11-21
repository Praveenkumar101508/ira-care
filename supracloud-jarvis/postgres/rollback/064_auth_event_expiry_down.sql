-- =============================================================================
-- rollback/064_auth_event_expiry_down.sql
--
-- Restores 061's sixteen-value event CHECK and drops the index 064 added.
--
-- ── The part that is not obvious ────────────────────────────────────────────
--
-- The narrow constraint CANNOT be validated. If any `log_expired` row exists,
-- a normal `ADD CONSTRAINT` re-checks every row, finds it, and the rollback
-- fails — and the row cannot be removed first, because `auth_events` is
-- append-only for every role including this one. A rollback that requires
-- deleting audit history in order to run is not a rollback anybody should
-- execute during an incident.
--
-- So the constraint comes back `NOT VALID`: enforced for every future INSERT
-- and UPDATE, not retroactively applied to rows already written. That is the
-- honest outcome — going backwards cannot unwrite history, and pretending
-- otherwise would mean either failing or quietly deleting evidence.
--
-- Running `VALIDATE CONSTRAINT` afterwards is left to the operator, and it will
-- succeed only on a log that never expired anything.
-- =============================================================================

BEGIN;

ALTER TABLE auth_events DROP CONSTRAINT IF EXISTS auth_events_event_check;
ALTER TABLE auth_events ADD CONSTRAINT auth_events_event_check CHECK (
    event = ANY (ARRAY[
        'sign_in_succeeded', 'sign_in_refused', 'sign_in_unavailable',
        'mfa_required', 'mfa_succeeded', 'mfa_failed',
        'recovery_code_used', 'recovery_codes_issued',
        'password_reset_requested', 'password_reset_completed',
        'session_revoked', 'sessions_revoked_all',
        'provider_linked', 'provider_unlinked', 'provider_sign_in',
        'account_link_required'
    ])
) NOT VALID;

DROP INDEX IF EXISTS auth_events_created_idx;

-- The append-only trigger must survive the rollback. It is 061's, not 064's,
-- and 064 never touched it — but the operator path disables it transiently, so
-- asserting it here costs nothing and catches a rollback run while a failed
-- expiry left the table open.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgrelid = 'auth_events'::regclass
                      AND tgname = 'auth_events_append_only_trg'
                      AND tgenabled <> 'D') THEN
        RAISE EXCEPTION
            'auth_events_append_only_trg is missing or DISABLED — the log is '
            'writable. Re-enable it before continuing.';
    END IF;
END $$;

COMMIT;
