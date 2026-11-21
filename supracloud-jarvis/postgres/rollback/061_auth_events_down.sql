-- =============================================================================
-- rollback/061_auth_events_down.sql — reverse 061_auth_events.sql
--
-- Refuses while the log holds anything at all.
--
-- ── Why ANY row is the line here, unlike every other rollback in this chain ──
--
-- `055` refuses on accepted invitations, `058` on live reset links, `060` on
-- live credentials — each picks the subset whose loss actually costs something,
-- and lets the harmless rows through. This one refuses on the FIRST row, and
-- the difference is what the table is.
--
-- Every other table holds state: something is true now, and the rollback asks
-- whether reversing breaks it. This holds HISTORY, and history has no harmless
-- subset — the rows nobody minds losing are indistinguishable from the ones an
-- investigation will need, because which is which is decided later, by an event
-- that has not happened yet. "Only the old ones" is precisely the criterion an
-- intruder would choose.
--
-- So the guard is blunt on purpose. Dropping this table is exporting the log
-- first and saying so, which is a decision an operator makes deliberately.
--
-- Counted with FORCE lifted, because this script binds no GUCs and the SELECT
-- policy keys on `app.user_id` — an unbound connection matches ZERO rows, so
-- the guard would report an empty log on every deployment in existence. That is
-- the P2-05 shape, and on an audit table it is the one that destroys the
-- evidence while reporting there was none.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_events bigint;
BEGIN
    IF to_regclass('public.auth_events') IS NULL THEN
        RETURN;   -- already reversed
    END IF;

    EXECUTE 'ALTER TABLE auth_events NO FORCE ROW LEVEL SECURITY';
    SELECT count(*) INTO n_events FROM auth_events;
    EXECUTE 'ALTER TABLE auth_events FORCE ROW LEVEL SECURITY';

    IF n_events > 0 THEN
        RAISE EXCEPTION
            '061-down REFUSING: the authentication log holds % event(s).', n_events
            USING HINT =
                'This table is history, not state, and history has no harmless '
                'subset: which rows an investigation needs is decided later, by '
                'an event that has not happened yet. Export the log and record '
                'where it went, then truncate it deliberately and re-run. '
                'Reversing this silently is how the record of an intrusion '
                'disappears along with the schema change that was blamed for '
                'it.';
    END IF;
END $$;

-- The trigger refuses DELETE, so it goes before the table does. Dropping the
-- table would take it anyway; doing it explicitly keeps the reversal readable
-- and makes the order match the forward script's.
DROP TRIGGER IF EXISTS auth_events_append_only_trg ON auth_events;
DROP FUNCTION IF EXISTS auth_events_append_only();

DROP POLICY IF EXISTS auth_event_redaction_select ON auth_events;
DROP POLICY IF EXISTS auth_event_update ON auth_events;
DROP POLICY IF EXISTS auth_event_insert ON auth_events;
DROP POLICY IF EXISTS auth_event_select ON auth_events;

DROP INDEX IF EXISTS auth_events_tenant_idx;
DROP INDEX IF EXISTS auth_events_subject_idx;
DROP INDEX IF EXISTS auth_events_user_idx;

DROP TABLE IF EXISTS auth_events;

DO $$
BEGIN
    IF to_regclass('public.auth_events') IS NOT NULL THEN
        RAISE EXCEPTION '061-down: auth_events still exists';
    END IF;
    -- 061 creates one table and one function. Nothing else may have gone —
    -- least of all the other audit tables, which is what a rollback of an audit
    -- migration is most likely to take with it.
    IF to_regclass('public.access_audit_log') IS NULL THEN
        RAISE EXCEPTION '061-down destroyed access_audit_log';
    END IF;
    IF to_regclass('public.biometric_audit') IS NULL THEN
        RAISE EXCEPTION '061-down destroyed biometric_audit';
    END IF;
    IF to_regclass('public.external_identities') IS NULL THEN
        RAISE EXCEPTION '061-down destroyed external_identities';
    END IF;
END $$;

COMMIT;
