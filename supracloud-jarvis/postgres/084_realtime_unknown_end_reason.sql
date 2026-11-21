-- =============================================================================
-- 084_realtime_unknown_end_reason.sql — Phase 14 P14-01b, finding X
--
-- One value added to `realtime_sessions.end_reason`'s controlled vocabulary:
-- `unknown`.
--
-- ── What was wrong ──────────────────────────────────────────────────────────
--
-- `POST /realtime/sessions/{id}/end` accepts a reason string from the client.
-- When that string was not one of `027`'s eleven values, the route recorded the
-- session as `user_ended` — and `user_ended` is not a neutral placeholder. The
-- application derives the session STATE from the reason, and `user_ended` is on
-- the clean side of that line, so a session that a client reported as lost was
-- stored, durably, as a user hanging up. It then never appeared in any count of
-- failed sessions, and the string the client actually sent was discarded.
--
-- P13-01 found this and deliberately did not fix it, because the obvious repair
-- is worse than it looks: falling back to `error` would record a FAILURE for a
-- reason code that might simply be newer than this server. Both available
-- answers assert something nobody knows.
--
-- ── Why a new value rather than a wider column ──────────────────────────────
--
-- `027` constrains this column for the reason it states there: an unenumerated
-- reason would be rejected by the database at the worst possible moment, which
-- is while recording that something already went wrong. Dropping the CHECK to
-- let the client's raw string through would fix the truthfulness problem by
-- creating a free-text column fed directly from a request body, on a table
-- whose whole design is about disclosing exactly what it stores.
--
-- So the vocabulary gains one word with a precise meaning: *the session ended
-- and this server could not classify how.* It is not `error` — no failure has
-- been established — and it is not `user_ended` — no clean hang-up has been
-- established either. The application places it on the abnormal side, so an
-- unknown termination is never counted as clean, while keeping it separable
-- from the known failures so an operator counting outages is not handed the
-- sessions whose reason code was merely unfamiliar.
--
-- ── Reversibility ───────────────────────────────────────────────────────────
--
-- This destroys nothing and reverses cleanly on a deployment that has never
-- recorded `unknown`. Unlike `083`, `realtime_sessions` is not append-only, so
-- the guard in `rollback/084` names a condition an operator can genuinely act
-- on rather than a door that has closed: those rows can be reclassified, or the
-- sessions they describe can be aged out.
--
-- Rollback: rollback/084_realtime_unknown_end_reason_down.sql
-- =============================================================================

BEGIN;

ALTER TABLE realtime_sessions DROP CONSTRAINT IF EXISTS realtime_sessions_end_reason_check;

ALTER TABLE realtime_sessions ADD CONSTRAINT realtime_sessions_end_reason_check
    CHECK (end_reason IN (
        'user_ended', 'user_interrupted', 'idle_timeout',
        'transport_lost', 'reconnect_exhausted',
        'provider_unavailable', 'permission_denied',
        'no_provider_configured', 'server_shutdown',
        'capability_floor_reached', 'error',
        -- P14-01b. The session ended and the reason given was not one of the
        -- above. Abnormal, and NOT a claim that anything failed.
        'unknown'));

COMMENT ON COLUMN realtime_sessions.end_reason IS
    'Why the session stopped. Controlled vocabulary (027, 084) — never free '
    'text, because this column is written while recording that something has '
    'already gone wrong and a rejected INSERT there loses the record. '
    '''unknown'' means this server could not classify the reason the client '
    'sent: it is on the abnormal side, so it is never counted as a clean '
    'hang-up, and it is distinct from ''error'', so it is never counted as an '
    'established failure either.';

COMMIT;
