-- =============================================================================
-- 114_care_watch_unpaired.sql — IRA CARE: the care circle is told when the
-- watch is unpaired from the wrist (remediation C-21)
--
-- A watch can now be unpaired by the person wearing it, after a warning
-- (POST /devices/me/unpair). That stops the monitoring, so it must never pass
-- in silence: the same transaction that revokes the watch opens a `notice`
-- alert of the new kind 'watch_unpaired', which tells the carers once, the way
-- a switched-off sensor permission does.
--
-- Additive: one constraint widened.
-- Rollback: rollback/114_care_watch_unpaired_down.sql
-- =============================================================================
BEGIN;

ALTER TABLE care_alerts DROP CONSTRAINT care_alerts_kind_check;
ALTER TABLE care_alerts ADD CONSTRAINT care_alerts_kind_check
    CHECK (kind IN ('help_requested', 'no_answer_no_movement', 'heart_rate_outside_range',
        'off_wrist_during_check', 'missed_checkins', 'off_wrist', 'low_battery',
        'sensor_permission_off', 'no_sync', 'manual', 'off_wrist_night', 'suspected_fall',
        'concerning_phrase', 'visit_late', 'visit_missed', 'watch_unpaired'));

COMMIT;
