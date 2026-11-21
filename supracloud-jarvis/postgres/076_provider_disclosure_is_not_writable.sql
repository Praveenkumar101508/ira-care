-- =============================================================================
-- 076_provider_disclosure_is_not_writable.sql — Phase 8, batch P8-07.
--
-- The runtime role can rewrite what IRA discloses about where requests go.
--
-- ── The finding ────────────────────────────────────────────────────────────
--
-- `api/routes/models.py` states the rule in its own module docstring:
--
--     There is no endpoint that changes a provider's disclosure. A retention
--     claim is a fact about a third party's terms, not a user setting, and an
--     API that let it be edited would let a deployment mark a retaining
--     provider as clean and route health notes to it.
--
-- That is true of the API, and it was the whole of the control. The database
-- says otherwise: `ira_app` and `ira_worker` hold INSERT, UPDATE and DELETE on
-- `model_providers`, so the process that answers requests can also edit the
-- record of where it sent them.
--
--     model_licence_register   ira_app: no INSERT, no UPDATE, no DELETE   (068)
--     model_registry           ira_app: no INSERT, no UPDATE, no DELETE   (067)
--     model_pull_allowlist     ira_app: no INSERT, no UPDATE, no DELETE   (067)
--     model_providers          ira_app: INSERT, UPDATE, DELETE            ← here
--
-- `067` and `068` each revoked deliberately, for the reason `067` recorded: a
-- process that answers requests must not be able to approve its own licence.
-- `029` predates both and nothing revoked its table, so it kept the DML that
-- `042`'s `ALTER DEFAULT PRIVILEGES FOR ROLE ira_schema_owner` hands to every
-- table that role owns.
--
-- Nobody exploited it and no endpoint exposes it. That is exactly the shape
-- `067` describes as *"a gate, not an answer"* — a control that holds because
-- of what has not been written yet.
--
-- Found by the P8-07 staging acceptance script asserting the property against a
-- real chain rather than against the routes.
--
-- ── What is deliberately NOT revoked ───────────────────────────────────────
--
-- `model_usage` and `usage_quotas` stay writable by `ira_app`. The application
-- IS what records a usage row and spends a quota slot; taking that away would
-- move metering to a role that does not serve requests, which is a bigger
-- change than a privilege fix and a worse design. Both carry RLS, so a tenant's
-- rows stay a tenant's rows.
--
-- SELECT is untouched. The Privacy Centre reads this table on every request.
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'model_providers') THEN
        RAISE EXCEPTION
            '076 REFUSING: model_providers does not exist. 029_commercial.sql '
            'has not been applied, so there is no disclosure catalogue to '
            'protect and applying this would hide that.';
    END IF;
END $$;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON model_providers FROM ira_app;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON model_providers FROM ira_worker;

-- 042's default privileges are per-role and forward-looking: they grant on
-- tables created AFTER the statement. A future migration that re-creates this
-- table would hand the DML straight back, so the default is narrowed for this
-- role too rather than only the current grant being revoked.
--
-- Scoped to `model_providers` is not possible — ALTER DEFAULT PRIVILEGES has no
-- per-table form — so this does NOT attempt it. The revoke above is the
-- control; this comment exists so the next reader knows the gap rather than
-- assuming it was handled.

GRANT SELECT ON model_providers TO ira_app;
GRANT SELECT ON model_providers TO ira_worker;

COMMENT ON TABLE model_providers IS
    'Phase 8 commercial catalogue. Platform-authored operator configuration and '
    'a disclosure record: what a provider does with data is a fact about that '
    'provider''s terms, not a user setting. ira_app and ira_worker read it and '
    'cannot write it (076) — the process that answers requests must not be able '
    'to edit the record of where it sent them.';

-- ── Prove the revoke actually took ─────────────────────────────────────────
--
-- A REVOKE that ran and a REVOKE that mattered look identical in a diff, and
-- this one is undone by any later `GRANT ALL` — which is not hypothetical: the
-- rollback cycle in CI used to end with exactly that statement.
DO $$
DECLARE
    offender TEXT;
BEGIN
    FOR offender IN
        SELECT r.rolname
          FROM (VALUES ('ira_app'), ('ira_worker')) AS v(rolname)
          JOIN pg_roles r ON r.rolname = v.rolname
         WHERE has_table_privilege(r.rolname, 'model_providers', 'INSERT')
            OR has_table_privilege(r.rolname, 'model_providers', 'UPDATE')
            OR has_table_privilege(r.rolname, 'model_providers', 'DELETE')
    LOOP
        RAISE EXCEPTION
            '076 FAILED: % can still write model_providers. The disclosure '
            'catalogue is editable by a role that serves requests.', offender;
    END LOOP;

    IF NOT has_table_privilege('ira_app', 'model_providers', 'SELECT') THEN
        RAISE EXCEPTION
            '076 FAILED: ira_app can no longer READ model_providers. The '
            'Privacy Centre reads it on every request; revoking the read '
            'would take the disclosure surface offline to protect it.';
    END IF;
END $$;

COMMIT;
