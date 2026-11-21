-- =============================================================================
-- 103_care_fall_detection.sql — IRA CARE: suspected falls
--
-- The watch detects a hard impact followed by stillness and asks first, with a
-- countdown, before an SOS with location. These are the per-person settings
-- that drive it, all set by the carer; none of them is a medical claim.
--
--   care_safety_settings
--     fall_detection_enabled    on by default; a carer can turn it off per person
--     fall_countdown_s          how long the watch asks "are you OK?" before the SOS (30)
--     fall_impact_g             a sample at or above this is an impact (2.5 g)
--     fall_still_seconds        no movement for this long after the impact (20)
--     fall_still_threshold_g    how far from 1 g counts as movement (0.3 g)
--   care_alerts.kind + 'suspected_fall'
--
-- No new table, so no new RLS. Rollback: rollback/103_care_fall_detection_down.sql
-- =============================================================================

BEGIN;

ALTER TABLE care_safety_settings
    ADD COLUMN IF NOT EXISTS fall_detection_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS fall_countdown_s INTEGER NOT NULL DEFAULT 30
        CHECK (fall_countdown_s BETWEEN 10 AND 120),
    ADD COLUMN IF NOT EXISTS fall_impact_g DOUBLE PRECISION NOT NULL DEFAULT 2.5
        CHECK (fall_impact_g BETWEEN 1.5 AND 8.0),
    ADD COLUMN IF NOT EXISTS fall_still_seconds INTEGER NOT NULL DEFAULT 20
        CHECK (fall_still_seconds BETWEEN 5 AND 120),
    ADD COLUMN IF NOT EXISTS fall_still_threshold_g DOUBLE PRECISION NOT NULL DEFAULT 0.3
        CHECK (fall_still_threshold_g BETWEEN 0.05 AND 1.0);

ALTER TABLE care_alerts DROP CONSTRAINT care_alerts_kind_check;
ALTER TABLE care_alerts ADD CONSTRAINT care_alerts_kind_check
    CHECK (kind IN ('help_requested', 'no_answer_no_movement', 'heart_rate_outside_range',
        'off_wrist_during_check', 'missed_checkins', 'off_wrist', 'low_battery',
        'sensor_permission_off', 'no_sync', 'manual', 'off_wrist_night', 'suspected_fall'));

COMMIT;
