-- =============================================================================
-- 123_device_admin_factory_reset_approval.sql — IRA CARE OS: a factory reset needs
-- two deployment administrators (security review S15, threat T43, D-089)
--
-- Until now one deployment administrator, with an ordinary session, could have the
-- server sign FACTORY_RESET for a watch (122). A phished password or a stolen session
-- was enough to wipe a person's safety device. From here:
--
--   * FACTORY_RESET is stored as a REQUEST (state 'pending_approval'): no nonce, no
--     payload, no signature, no issue or expiry time. Nothing a watch can be given.
--   * A DIFFERENT deployment administrator approves it within 30 minutes of the
--     request (approval_deadline). Only then does the server sign it, with a fresh
--     issue time and the requested 1 to 15 minute life (window_minutes), and the row
--     becomes 'signed'. Past the deadline it can never be signed: it lapses unsigned.
--   * The table refuses, whatever the code does: a reset inserted in any state but
--     'pending_approval'; approval by the requester; approval after the deadline;
--     approval of a withdrawn request; approval once either administrator's grant has
--     been revoked since the request; any change to a request other than that one
--     approval or its withdrawal. Raised as insufficient_privilege, as in 122.
--   * MAINTENANCE_WINDOW and RESTORE_RESTRICTIONS are unchanged: signed at once, by
--     one administrator, state 'signed', no approval columns.
--
-- New columns on care_device_admin_instructions:
--   state             'pending_approval' or 'signed' (every row before 123 is 'signed')
--   requested_at      when the request was made (NULL for an instruction that needed no approval)
--   approval_deadline at most 30 minutes after requested_at
--   window_minutes    the life the signed instruction will have, 1 to 15
--   approved_by       the second administrator's account id (no foreign key, as issued_by)
--   approved_at       when they approved, which is when it was signed
-- nonce, issued_at, expires_at, payload, signature and key_id may now be NULL, and are
-- NULL exactly while a request waits (CHECK below). issued_at keeps its default, so an
-- instruction that needs no approval is written exactly as before.
--
-- The audit trail gains three events: 'requested' (a reset asked for), 'approved' (by
-- the second administrator, at which point it is signed) and 'approval_refused' (an
-- administrator's approval was refused, with why). Still insert-only for the runtime.
--
-- No new table: the tenancy manifest, RLS policies, grants and erasure paths of 122
-- cover the new columns unchanged.
--
-- Rollback: rollback/123_device_admin_factory_reset_approval_down.sql
-- =============================================================================

BEGIN;

-- ── 1. The request and approval columns ──────────────────────────────────────

ALTER TABLE care_device_admin_instructions
    ADD COLUMN state             TEXT NOT NULL DEFAULT 'signed'
                                     CHECK (state IN ('pending_approval', 'signed')),
    ADD COLUMN requested_at      TIMESTAMPTZ,
    ADD COLUMN approval_deadline TIMESTAMPTZ,
    ADD COLUMN window_minutes    INTEGER CHECK (window_minutes IS NULL OR window_minutes BETWEEN 1 AND 15),
    ADD COLUMN approved_by       UUID,
    ADD COLUMN approved_at       TIMESTAMPTZ;

ALTER TABLE care_device_admin_instructions
    ALTER COLUMN nonce      DROP NOT NULL,
    ALTER COLUMN issued_at  DROP NOT NULL,
    ALTER COLUMN expires_at DROP NOT NULL,
    ALTER COLUMN payload    DROP NOT NULL,
    ALTER COLUMN signature  DROP NOT NULL,
    ALTER COLUMN key_id     DROP NOT NULL;

ALTER TABLE care_device_admin_instructions
    -- A signed instruction has everything the watch verifies; a waiting request has none
    -- of it, and nothing has happened to it on a watch.
    ADD CONSTRAINT care_device_admin_instructions_state_shape CHECK (
        (state = 'signed'
            AND nonce IS NOT NULL AND issued_at IS NOT NULL AND expires_at IS NOT NULL
            AND payload IS NOT NULL AND signature IS NOT NULL AND key_id IS NOT NULL)
        OR
        (state = 'pending_approval'
            AND nonce IS NULL AND issued_at IS NULL AND expires_at IS NULL
            AND payload IS NULL AND signature IS NULL AND key_id IS NULL
            AND delivered_at IS NULL AND outcome IS NULL
            AND approved_by IS NULL AND approved_at IS NULL
            AND requested_at IS NOT NULL)),
    -- Only a factory reset goes through approval, and its request is complete and short.
    ADD CONSTRAINT care_device_admin_instructions_request_shape CHECK (
        (requested_at IS NULL AND approval_deadline IS NULL AND window_minutes IS NULL)
        OR
        (requested_at IS NOT NULL AND approval_deadline IS NOT NULL AND window_minutes IS NOT NULL
            AND action = 'FACTORY_RESET'
            AND approval_deadline > requested_at
            AND approval_deadline <= requested_at + INTERVAL '30 minutes')),
    -- The approver is somebody else, approved inside the window, and the signed life is
    -- the one requested.
    ADD CONSTRAINT care_device_admin_instructions_approval_shape CHECK (
        ((approved_by IS NULL) = (approved_at IS NULL))
        AND (approved_by IS NULL OR (
                approved_by <> issued_by
                AND requested_at IS NOT NULL
                AND approved_at >= requested_at
                AND approved_at <= approval_deadline))
        AND (requested_at IS NULL OR state = 'pending_approval' OR (
                approved_by IS NOT NULL
                AND expires_at = issued_at + make_interval(mins => window_minutes))));

-- ── 2. The guards ────────────────────────────────────────────────────────────

-- On insert: a factory reset is only ever a request.
CREATE FUNCTION care_device_admin_instructions_insert_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    IF NEW.action = 'FACTORY_RESET' AND NEW.state IS DISTINCT FROM 'pending_approval' THEN
        RAISE EXCEPTION 'a factory reset needs a second deployment administrator''s approval'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER care_device_admin_instructions_insert_guard
    BEFORE INSERT ON care_device_admin_instructions
    FOR EACH ROW EXECUTE FUNCTION care_device_admin_instructions_insert_guard();

-- On update: 122's rules for a signed instruction, plus the one way a request changes
-- into a signed instruction. The revoked-grant check reads only revoked grants, which
-- every connection may see (122's read policy), so it works on the workspace-bound
-- connection the approval runs on.
CREATE OR REPLACE FUNCTION care_device_admin_instructions_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    IF OLD.state = 'pending_approval' AND NEW.state = 'signed' THEN
        IF (NEW.tenant_id, NEW.workspace_id, NEW.device_id, NEW.action, NEW.issued_by, NEW.reason,
            NEW.requested_at, NEW.approval_deadline, NEW.window_minutes, NEW.revoked_at, NEW.revoked_by)
           IS DISTINCT FROM
           (OLD.tenant_id, OLD.workspace_id, OLD.device_id, OLD.action, OLD.issued_by, OLD.reason,
            OLD.requested_at, OLD.approval_deadline, OLD.window_minutes, OLD.revoked_at, OLD.revoked_by) THEN
            RAISE EXCEPTION 'a request can be approved, not changed' USING ERRCODE = '42501';
        END IF;
        IF OLD.revoked_at IS NOT NULL THEN
            RAISE EXCEPTION 'a withdrawn request cannot be approved' USING ERRCODE = '42501';
        END IF;
        IF NEW.approved_by IS NULL OR NEW.approved_by = OLD.issued_by THEN
            RAISE EXCEPTION 'a factory reset needs a second, different deployment administrator'
                USING ERRCODE = '42501';
        END IF;
        IF clock_timestamp() > OLD.approval_deadline THEN
            RAISE EXCEPTION 'the approval window for this request has passed' USING ERRCODE = '42501';
        END IF;
        IF EXISTS (SELECT 1 FROM deployment_administrators a
                    WHERE a.user_id IN (OLD.issued_by, NEW.approved_by)
                      AND a.revoked_at >= OLD.requested_at) THEN
            RAISE EXCEPTION 'a deployment administrator on this request has been revoked'
                USING ERRCODE = '42501';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state THEN
        RAISE EXCEPTION 'a device instruction''s state cannot go back' USING ERRCODE = '42501';
    END IF;
    IF (NEW.tenant_id, NEW.workspace_id, NEW.device_id, NEW.action, NEW.nonce, NEW.issued_at,
        NEW.expires_at, NEW.issued_by, NEW.reason, NEW.payload, NEW.signature, NEW.key_id,
        NEW.requested_at, NEW.approval_deadline, NEW.window_minutes, NEW.approved_by, NEW.approved_at)
       IS DISTINCT FROM
       (OLD.tenant_id, OLD.workspace_id, OLD.device_id, OLD.action, OLD.nonce, OLD.issued_at,
        OLD.expires_at, OLD.issued_by, OLD.reason, OLD.payload, OLD.signature, OLD.key_id,
        OLD.requested_at, OLD.approval_deadline, OLD.window_minutes, OLD.approved_by, OLD.approved_at) THEN
        RAISE EXCEPTION 'a signed device instruction cannot be changed' USING ERRCODE = '42501';
    END IF;
    IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
        RAISE EXCEPTION 'a revoked device instruction stays revoked' USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END $$;

-- Waiting requests, for the approval check and for listing what is outstanding.
CREATE INDEX idx_care_device_admin_instructions_pending
    ON care_device_admin_instructions (tenant_id, workspace_id, approval_deadline)
    WHERE state = 'pending_approval';

-- ── 3. The audit trail's new events ─────────────────────────────────────────

ALTER TABLE care_device_admin_events DROP CONSTRAINT care_device_admin_events_event_check;
ALTER TABLE care_device_admin_events ADD CONSTRAINT care_device_admin_events_event_check
    CHECK (event IN ('issued', 'delivered', 'revoked', 'applied', 'refused', 'restored',
                     'requested', 'approved', 'approval_refused'));

COMMIT;
