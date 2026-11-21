-- F1 durable access-token revocation (revision 3, owner-approved 2026-10-19).
-- Global authentication metadata, no evidence content, no credential: only
-- SHA-256 of a JWT identifier (jti), which authenticates nothing on its own.
-- Immutable deny records: never expire or delete them, during rollout or
-- rollback. Deleting one could resurrect the token it revokes.
--
-- No SECURITY DEFINER. Revisions 1 and 2 reached the table through two definer
-- functions owned by ira_schema_owner; the repository's rule is that no definer
-- function exists (tests/tenancy/test_role_separation.py and docs/f1/DESIGN.md),
-- so access is granted directly instead:
--
--   * ira_app: SELECT and INSERT only. No UPDATE, no DELETE, for any role,
--     and no UPDATE/DELETE policy either, so under FORCE RLS even the table
--     owner cannot change or remove a row. Append-only for everyone below
--     superuser.
--   * SELECT lets the runtime list the stored hashes. Accepted: they are
--     one-way hashes of token identifiers, not secrets, and listing them
--     yields no token.
--   * The per-token advisory lock that orders revocation against an in-flight
--     evidence commit is taken by the caller, core/identity/f1_revocation.py,
--     in the same transaction as the INSERT.
--   * ira_worker gets nothing.
CREATE TABLE public.f1_token_revocations (
    token_hash text PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    revoked_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE public.f1_token_revocations OWNER TO ira_schema_owner;
REVOKE ALL ON public.f1_token_revocations FROM PUBLIC, ira_app, ira_worker;
GRANT SELECT, INSERT ON public.f1_token_revocations TO ira_app;

ALTER TABLE public.f1_token_revocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.f1_token_revocations FORCE ROW LEVEL SECURITY;
CREATE POLICY f1_token_revocations_read ON public.f1_token_revocations
    FOR SELECT USING (true);
CREATE POLICY f1_token_revocations_append ON public.f1_token_revocations
    FOR INSERT WITH CHECK (true);
