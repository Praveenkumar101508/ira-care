-- Rollback for 041_biometric_claim_telemetry.sql.
--
-- Restores the 038 form of the append-only trigger BEFORE dropping the columns
-- it references — otherwise the function would reference columns that no longer
-- exist and every UPDATE on the table would fail at runtime rather than here,
-- which is the worst place for this to surface.

ALTER TABLE biometric_audit NO FORCE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION biometric_audit_refuse_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.tenant_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM tenants WHERE id = OLD.tenant_id) THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'biometric_audit is append-only: DELETE is refused';
    END IF;

    IF NEW.id          IS DISTINCT FROM OLD.id
       OR NEW.session_id IS DISTINCT FROM OLD.session_id
       OR NEW.similarity IS DISTINCT FROM OLD.similarity
       OR NEW.threshold  IS DISTINCT FROM OLD.threshold
       OR NEW.result     IS DISTINCT FROM OLD.result
       OR NEW.source     IS DISTINCT FROM OLD.source
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.event_type IS DISTINCT FROM OLD.event_type
    THEN
        RAISE EXCEPTION 'biometric_audit is append-only: evidential columns cannot change';
    END IF;

    IF (NEW.user_id      IS NOT NULL AND NEW.user_id      IS DISTINCT FROM OLD.user_id)
       OR (NEW.tenant_id    IS NOT NULL AND NEW.tenant_id    IS DISTINCT FROM OLD.tenant_id)
       OR (NEW.workspace_id IS NOT NULL AND NEW.workspace_id IS DISTINCT FROM OLD.workspace_id)
    THEN
        RAISE EXCEPTION 'biometric_audit: attribution may be cleared, never reassigned';
    END IF;

    RETURN NEW;
END
$$;

DROP INDEX IF EXISTS idx_biometric_audit_claim;

ALTER TABLE biometric_audit DROP CONSTRAINT IF EXISTS biometric_audit_claim_hash_is_a_hash;
ALTER TABLE biometric_audit DROP CONSTRAINT IF EXISTS biometric_audit_claim_hash_has_version;
ALTER TABLE biometric_audit DROP CONSTRAINT IF EXISTS biometric_audit_failure_reason_check;
ALTER TABLE biometric_audit DROP CONSTRAINT IF EXISTS biometric_audit_failure_reason_only_on_failure;

ALTER TABLE biometric_audit DROP COLUMN IF EXISTS claim_hash;
ALTER TABLE biometric_audit DROP COLUMN IF EXISTS claim_hash_version;
ALTER TABLE biometric_audit DROP COLUMN IF EXISTS failure_reason;
ALTER TABLE biometric_audit DROP COLUMN IF EXISTS device_fingerprint;

ALTER TABLE biometric_audit FORCE ROW LEVEL SECURITY;
