-- Rollback of 107_care_push_tokens.sql. Refuses while any push ticket records a delivery:
-- that is evidence of who was told about an alert.
BEGIN;
DO $$
DECLARE n BIGINT;
BEGIN
    SELECT count(*) INTO n FROM care_push_tickets WHERE status = 'delivered';
    IF n > 0 THEN RAISE EXCEPTION 'Refusing rollback: % delivered push ticket row(s) exist.', n; END IF;
END $$;
DROP TABLE IF EXISTS care_push_tickets;
DROP TABLE IF EXISTS care_push_tokens;
COMMIT;
