-- =============================================================================
-- rollback/084_realtime_unknown_end_reason_down.sql
--
-- Narrows `realtime_sessions.end_reason` back to `027`'s eleven values.
--
-- It REFUSES if any row already records `unknown`. Re-adding the narrow
-- constraint on such a table would fail anyway — but with a message about a
-- check constraint, which tells an operator nothing about what is in the way.
--
-- Unlike `083`, this one is genuinely actionable: `realtime_sessions` is not
-- append-only, so the offending rows can be reclassified to whichever of the
-- eleven the operator determines they were, or aged out with the sessions they
-- describe. The refusal says so rather than implying a cleanup that does not
-- exist — and it says what reclassifying costs, which is that a row nobody
-- could classify at the time is being classified now from further away.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    offending BIGINT;
BEGIN
    SELECT count(*) INTO offending
      FROM realtime_sessions WHERE end_reason = 'unknown';

    IF offending > 0 THEN
        RAISE EXCEPTION
            'REFUSING to roll back 084: % realtime_sessions row(s) record '
            'end_reason = ''unknown''. These are sessions whose client sent a '
            'termination reason this server could not classify. Narrowing the '
            'vocabulary would make them illegal. Unlike 083 this is '
            'recoverable: the rows are mutable, so they can be reclassified to '
            'one of 027''s eleven reasons or aged out with their sessions. Be '
            'aware that reclassifying asserts something that was unknown at '
            'the time it happened — prefer ageing them out.',
            offending;
    END IF;
END
$$;

ALTER TABLE realtime_sessions DROP CONSTRAINT IF EXISTS realtime_sessions_end_reason_check;

ALTER TABLE realtime_sessions ADD CONSTRAINT realtime_sessions_end_reason_check
    CHECK (end_reason IN (
        'user_ended', 'user_interrupted', 'idle_timeout',
        'transport_lost', 'reconnect_exhausted',
        'provider_unavailable', 'permission_denied',
        'no_provider_configured', 'server_shutdown',
        'capability_floor_reached', 'error'));

COMMENT ON COLUMN realtime_sessions.end_reason IS
    'Why the session stopped. Controlled vocabulary (027) — never free text.';

COMMIT;
