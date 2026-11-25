-- Rollback of 123_device_admin_factory_reset_approval.sql. Refuses while any factory-reset
-- request exists (waiting, lapsed or approved) or any audit row uses an event 123 added:
-- dropping the approval columns would lose who approved a wipe, and 122's shape cannot
-- hold a request with no signature. Run as a role that sees every row (the rows are
-- behind forced row-level security).
BEGIN;
DO $$
DECLARE n BIGINT;
BEGIN
    SELECT (SELECT count(*) FROM care_device_admin_instructions WHERE requested_at IS NOT NULL
                                                                   OR state <> 'signed')
         + (SELECT count(*) FROM care_device_admin_events
             WHERE event IN ('requested', 'approved', 'approval_refused')) INTO n;
    IF n > 0 THEN
        RAISE EXCEPTION 'Refusing rollback: % factory-reset request(s) or approval event(s) exist.', n;
    END IF;
END $$;

ALTER TABLE care_device_admin_events DROP CONSTRAINT care_device_admin_events_event_check;
ALTER TABLE care_device_admin_events ADD CONSTRAINT care_device_admin_events_event_check
    CHECK (event IN ('issued', 'delivered', 'revoked', 'applied', 'refused', 'restored'));

DROP INDEX IF EXISTS idx_care_device_admin_instructions_pending;
DROP TRIGGER IF EXISTS care_device_admin_instructions_insert_guard ON care_device_admin_instructions;
DROP FUNCTION IF EXISTS care_device_admin_instructions_insert_guard();

-- 122's update guard, exactly as 122 wrote it.
CREATE OR REPLACE FUNCTION care_device_admin_instructions_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    IF (NEW.tenant_id, NEW.workspace_id, NEW.device_id, NEW.action, NEW.nonce, NEW.issued_at,
        NEW.expires_at, NEW.issued_by, NEW.reason, NEW.payload, NEW.signature, NEW.key_id)
       IS DISTINCT FROM
       (OLD.tenant_id, OLD.workspace_id, OLD.device_id, OLD.action, OLD.nonce, OLD.issued_at,
        OLD.expires_at, OLD.issued_by, OLD.reason, OLD.payload, OLD.signature, OLD.key_id) THEN
        RAISE EXCEPTION 'a signed device instruction cannot be changed' USING ERRCODE = '42501';
    END IF;
    IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
        RAISE EXCEPTION 'a revoked device instruction stays revoked' USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END $$;

ALTER TABLE care_device_admin_instructions
    DROP CONSTRAINT care_device_admin_instructions_approval_shape,
    DROP CONSTRAINT care_device_admin_instructions_request_shape,
    DROP CONSTRAINT care_device_admin_instructions_state_shape;

ALTER TABLE care_device_admin_instructions
    ALTER COLUMN nonce      SET NOT NULL,
    ALTER COLUMN issued_at  SET NOT NULL,
    ALTER COLUMN expires_at SET NOT NULL,
    ALTER COLUMN payload    SET NOT NULL,
    ALTER COLUMN signature  SET NOT NULL,
    ALTER COLUMN key_id     SET NOT NULL;

ALTER TABLE care_device_admin_instructions
    DROP COLUMN approved_at,
    DROP COLUMN approved_by,
    DROP COLUMN window_minutes,
    DROP COLUMN approval_deadline,
    DROP COLUMN requested_at,
    DROP COLUMN state;
COMMIT;
