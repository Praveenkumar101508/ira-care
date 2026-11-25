-- Rollback of 113_care_delivery_states.sql.
--
-- What is lost: the outbox (messages still waiting to be sent are NOT sent by
-- the pre-113 code; send or note them first) and the history of what each
-- provider reported. The attempts themselves stay. The narrower outcome
-- constraint cannot hold 'queued' and the new words, so those rows are first
-- mapped to the nearest pre-113 word: queued -> failed (never handed over),
-- accepted -> sent, the rest -> failed.
BEGIN;
DROP TABLE IF EXISTS care_alert_attempt_events;
DROP TABLE IF EXISTS care_alert_dispatch;
UPDATE care_alert_attempts SET outcome = CASE outcome WHEN 'accepted' THEN 'sent' ELSE 'failed' END
 WHERE outcome IN ('accepted', 'queued', 'undelivered', 'no_answer', 'busy', 'unknown');
ALTER TABLE care_alert_attempts DROP CONSTRAINT IF EXISTS care_alert_attempts_outcome_check;
ALTER TABLE care_alert_attempts ADD CONSTRAINT care_alert_attempts_outcome_check
    CHECK (outcome IN ('logged', 'sent', 'delivered', 'failed', 'skipped'));
ALTER TABLE care_alert_attempts DROP COLUMN IF EXISTS wording_tier;
COMMIT;
