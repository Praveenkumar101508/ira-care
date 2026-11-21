-- =============================================================================
-- 083_biometric_consent_reason.sql — Phase 12 P12-04, finding I
--
-- One value added to `biometric_audit.failure_reason`'s controlled vocabulary:
-- `consent_missing`.
--
-- ── Why the vocabulary is extended rather than worked around ────────────────
--
-- `041` enumerated six reasons and constrained the column to them, and its own
-- comment says why: *"without this the column drifts into a free-text note
-- field, and a note field beside an audit row is where somebody eventually
-- writes the username."* That reasoning is right, so P12-04 does not smuggle a
-- new reason past it — a refusal this constraint rejects is dropped by
-- `_record_biometric_attempt`'s non-fatal `except`, which would leave the
-- refused attempt with no audit row at all. Silently. That is the exact failure
-- the reason column exists to prevent, arriving through the mechanism meant to
-- protect it.
--
-- ── Why the existing six are not enough ─────────────────────────────────────
--
-- P12-04 makes `is_owner_authenticated` consult consent before it loads a
-- template, and consent has three refusing states. Two already have a word:
--
--     no profile enrolled          -> no_enrolment        (041)
--     consent withdrawn            -> revoked_enrolment   (041)
--     enrolled, consent NEVER recorded -> nothing fits
--
-- The third is the state finding I actually left reachable: `037` shipped
-- `consent_granted_at` in Phase 3B and nothing ever wrote it, so every existing
-- enrolment is in it. Recording that as `no_enrolment` would be false — a
-- profile exists and its template is loadable — and recording it as
-- `revoked_enrolment` would be worse, because it asserts a withdrawal nobody
-- made. An operator reading either would look in the wrong place.
--
-- ── Reversible until first use, and then never ──────────────────────────────
--
-- Unlike `081` and `082` this destroys nothing, and on a deployment that has
-- never recorded `consent_missing` it reverses cleanly — `rollback/083` narrows
-- the vocabulary and the cycle composes.
--
-- Once one such row exists the reversal is refused **permanently**, and that is
-- worth stating precisely because the obvious reading is wrong. It is not
-- "clear the rows and retry": `041`'s `biometric_audit_refuse_mutation` trigger
-- refuses DELETE outright, and refuses any UPDATE that changes
-- `failure_reason`. The rows can be redacted — attribution cleared to NULL —
-- and nothing else. They go only when the tenant itself goes.
--
-- So the guard in `rollback/083` is not a speed bump in front of a cleanup
-- somebody can perform. It is the accurate report of a door that closed the
-- first time this system recorded why it refused to trust a voice. That is what
-- an append-only audit trail means, and narrowing a vocabulary underneath one
-- is not a schema migration.
--
-- Rollback: rollback/083_biometric_consent_reason_down.sql
-- =============================================================================

BEGIN;

ALTER TABLE biometric_audit DROP CONSTRAINT IF EXISTS biometric_audit_failure_reason_check;

ALTER TABLE biometric_audit ADD CONSTRAINT biometric_audit_failure_reason_check
    CHECK (failure_reason IS NULL OR failure_reason IN (
        'below_threshold',
        'no_enrolment',
        'revoked_enrolment',
        'unknown_claim',
        'rate_limited',
        'malformed_sample',
        -- P12-04. Enrolled, and no consent was ever recorded.
        'consent_missing'));

COMMENT ON COLUMN biometric_audit.failure_reason IS
    'Why a verification attempt was refused. Controlled vocabulary (041, 083) — '
    'never free text: a note field beside an audit row is where a username '
    'eventually gets written. consent_missing and revoked_enrolment are '
    'distinct on purpose; a withdrawal nobody made is not the same fact as a '
    'consent nobody collected.';

COMMIT;
