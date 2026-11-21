-- =============================================================================
-- rollback/083_biometric_consent_reason_down.sql
--
-- Narrows `biometric_audit.failure_reason` back to `041`'s six values.
--
-- It REFUSES if any row already records `consent_missing`. Re-adding the narrow
-- constraint on such a table would fail anyway — but with a message about a
-- check constraint, which tells an operator nothing about what is in the way.
--
-- And there is no way round it, which the refusal says rather than implying a
-- cleanup that does not exist. `041`'s `biometric_audit_refuse_mutation` makes
-- this table append-only: DELETE is refused, and an UPDATE that changes
-- `failure_reason` is refused. Those rows can be redacted and not removed, so
-- on a deployment that has recorded one, 083 is permanent.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    offending BIGINT;
BEGIN
    SELECT count(*) INTO offending
      FROM biometric_audit WHERE failure_reason = 'consent_missing';

    IF offending > 0 THEN
        RAISE EXCEPTION
            'REFUSING to roll back 083: % biometric_audit row(s) record '
            'failure_reason = ''consent_missing''. These are refused voice '
            'verifications where an enrolled voiceprint had no recorded '
            'consent. Narrowing the vocabulary would make them illegal. '
            'There is no cleanup that unblocks this: 041 makes biometric_audit '
            'append-only, so the rows cannot be deleted and their '
            'failure_reason cannot be amended — only attribution can be '
            'redacted. On this deployment 083 is permanent. That is what an '
            'append-only audit trail costs, and it is the intended trade.',
            offending;
    END IF;
END
$$;

ALTER TABLE biometric_audit DROP CONSTRAINT IF EXISTS biometric_audit_failure_reason_check;

ALTER TABLE biometric_audit ADD CONSTRAINT biometric_audit_failure_reason_check
    CHECK (failure_reason IS NULL OR failure_reason IN (
        'below_threshold',
        'no_enrolment',
        'revoked_enrolment',
        'unknown_claim',
        'rate_limited',
        'malformed_sample'));

COMMENT ON COLUMN biometric_audit.failure_reason IS
    'Why a verification attempt was refused. Controlled vocabulary (041) — '
    'never free text.';

COMMIT;
