-- Rollback for 044_strategy_calibration_tenant_key.sql (Phase 3B Batch 4B).
--
-- **This rollback can LOSE ROWS, and it is the only one in this repository that
-- can.** It restores `PRIMARY KEY (domain)`, which is global — so if two tenants
-- have calibrated the same domain since the migration, the restore is
-- impossible while both rows exist.
--
-- It refuses rather than choosing a winner. Deleting one tenant's calibration to
-- make a rollback succeed is a silent cross-tenant data loss, which is the exact
-- defect 044 exists to fix; the operator has to decide, and the refusal names
-- the domains involved.
--
-- The view goes first because it reads strategy_calibration.tenant_id.

DROP VIEW IF EXISTS batch4_tenant_scope_status;

DO $$
DECLARE
    clashes text;
BEGIN
    SELECT string_agg(domain, ', ') INTO clashes
      FROM (SELECT domain FROM strategy_calibration GROUP BY domain HAVING count(*) > 1) d;

    IF clashes IS NOT NULL THEN
        RAISE EXCEPTION
            'cannot restore PRIMARY KEY (domain): more than one tenant has calibrated %',
            clashes
            USING HINT = 'The old key is global. Restoring it requires deciding which '
                         'tenant keeps each domain, and this script will not decide that '
                         'for you — picking one silently is the data loss 044 prevents.';
    END IF;
END
$$;

DROP POLICY IF EXISTS tenant_select ON strategy_calibration;
DROP POLICY IF EXISTS tenant_insert ON strategy_calibration;
DROP POLICY IF EXISTS tenant_update ON strategy_calibration;
DROP POLICY IF EXISTS tenant_delete ON strategy_calibration;
ALTER TABLE strategy_calibration NO FORCE ROW LEVEL SECURITY;
ALTER TABLE strategy_calibration DISABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
         WHERE t.relname = 'strategy_calibration' AND c.contype = 'p'
           AND pg_get_constraintdef(c.oid) = 'PRIMARY KEY (tenant_id, domain)'
    ) THEN
        ALTER TABLE strategy_calibration DROP CONSTRAINT strategy_calibration_pkey;
        ALTER TABLE strategy_calibration
            ADD CONSTRAINT strategy_calibration_pkey PRIMARY KEY (domain);
    END IF;
END
$$;

ALTER TABLE strategy_calibration DROP COLUMN IF EXISTS created_at;
ALTER TABLE strategy_calibration DROP COLUMN IF EXISTS created_by;
ALTER TABLE strategy_calibration DROP COLUMN IF EXISTS tenant_id;
