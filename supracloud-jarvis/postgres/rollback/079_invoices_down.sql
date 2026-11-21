-- =============================================================================
-- rollback/079_invoices_down.sql
--
-- Drops the invoice tables.
--
-- Harmless while nothing has been billed, and REFUSING once any invoice has
-- been ISSUED. A draft is a working document and may be discarded with the
-- table; an issued invoice is the record that a customer was told what they
-- owe, and dropping it destroys that record while the obligation it describes
-- continues to exist.
--
-- Same shape as 051, 055, 056 and 078: reversible until it matters.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_issued BIGINT := 0;
BEGIN
    IF to_regclass('public.invoices') IS NOT NULL THEN
        -- Counted as the schema owner. Under FORCE RLS this would read zero on
        -- a table full of rows, which is how a guard reports "nothing here"
        -- about data it simply cannot see.
        EXECUTE 'ALTER TABLE invoices NO FORCE ROW LEVEL SECURITY';
        SELECT count(*) INTO n_issued FROM invoices WHERE status <> 'draft';
        EXECUTE 'ALTER TABLE invoices FORCE ROW LEVEL SECURITY';
    END IF;

    IF n_issued > 0 THEN
        RAISE EXCEPTION
            '079-down REFUSING: % invoice(s) have been issued. Dropping this '
            'table deletes the record of what customers were billed while the '
            'obligation continues to exist.', n_issued
        USING HINT =
            'Export them first: SELECT * FROM invoices; SELECT * FROM '
            'invoice_lines. To stop billing a tenant, cancel the subscription '
            'and VOID the invoice — voiding keeps the record that it was sent.';
    END IF;
END $$;

DROP TRIGGER IF EXISTS invoice_lines_no_edit_once_issued ON invoice_lines;
DROP TRIGGER IF EXISTS invoices_no_edit_once_issued ON invoices;
DROP FUNCTION IF EXISTS invoice_lines_follow_their_invoice();
DROP FUNCTION IF EXISTS invoices_immutable_once_issued();
DROP TABLE IF EXISTS invoice_lines;
DROP TABLE IF EXISTS invoices;

COMMIT;
