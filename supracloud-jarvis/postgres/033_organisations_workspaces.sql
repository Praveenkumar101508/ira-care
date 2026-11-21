-- =============================================================================
-- 033_organisations_workspaces.sql — Phase 3B Batch 0A
--
-- The tenancy substrate: `organisations` and `workspaces`, plus row-level
-- security on `tenants` itself.
--
-- ── The three boundaries, and why they are three ─────────────────────────────
--
--   tenant        security, billing and isolation boundary
--   organisation  customer-facing business identity
--   workspace     collaboration and content boundary
--
-- Collapsing any pair looks like simplification and is not. A tenant that is
-- also the business profile means renaming a company touches the isolation
-- boundary. A tenant that is also the content boundary means a customer cannot
-- separate "Engineering" from "Legal" without buying a second subscription.
--
-- **Tenant to organisation is 1:1 initially** (CLOUD-ADR-0010, owner decision
-- D-15), enforced by a UNIQUE constraint on `organisations.tenant_id`. That
-- constraint is the whole of the restriction: dropping it later is a one-line
-- migration, and no content table references `organisation_id`, so a future
-- one-to-many split re-scopes nothing. The cheap decision now is the one that
-- stays cheap to reverse.
--
-- ── Why `tenants` gets RLS here ──────────────────────────────────────────────
--
-- `023_tenancy.sql` created the tenant registry and forced RLS on 45 tables
-- that reference it, but left `tenants` itself unprotected — defensible while
-- one tenant existed, and not once a second does. A tenant able to enumerate
-- the registry learns every other customer's name and slug.
--
-- The policy is `id = <the caller's tenant>`, not `tenant_id = …`: this table's
-- own primary key *is* the tenant identifier.
--
-- ── Idempotence ──────────────────────────────────────────────────────────────
--
-- `utils/migrations.py` re-runs the chain on every boot, so every statement
-- here must survive re-execution. Inserts use fixed UUIDs and
-- `ON CONFLICT DO NOTHING`; DDL uses `IF NOT EXISTS`. Re-running must not
-- create a second organisation or a second default workspace — there are tests
-- for exactly that, because "ran twice and made two defaults" is the failure
-- that only shows up in production.
--
-- ── Migration constants ──────────────────────────────────────────────────────
--
-- Deterministic UUIDv5 values derived from IRA_UUID_NAMESPACE in
-- `core/identity/principal.py`. They are the same on every machine and across
-- restarts without being stored anywhere, which is what makes a re-run safe.
-- `023_tenancy.sql` already seeds the tenant with the first of them.
--
--   tenant:private        e91aceeb-89fb-5ad2-b43c-4eee2bad079c   (seeded by 023)
--   organisation:private  53925952-13ec-585d-9ab0-7016b647d6e8
--   workspace:private     38cdd893-d172-5afd-bd08-005902c30089
--
-- Rollback: rollback/033_organisations_workspaces_down.sql
-- ── No psql metacommands in this file ────────────────────────────────────────
--
-- The UUIDs below are written as literals rather than `\set` variables, and
-- that is deliberate. `utils/migrations.py` — the durable upgrade path — applies
-- migrations through **asyncpg**, which is a PostgreSQL protocol driver and not
-- psql: it rejects `\set` with `syntax error at or near "\"`.
--
-- Migrations 023 and 031 use `\set` and are therefore rejected by that runner
-- (recorded as R-45). They apply only because docker-entrypoint-initdb.d runs
-- them through psql on a brand-new volume. This file must not inherit that.
--
-- =============================================================================


-- ─────────────────────────────────────────────────────────────────────────────
-- 1. organisations — the customer-facing business identity
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS organisations (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    slug         TEXT,
    legal_name   TEXT,
    country_code TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 1:1 with tenant. This single constraint is the entire cardinality decision.
CREATE UNIQUE INDEX IF NOT EXISTS organisations_tenant_key
    ON organisations (tenant_id);

-- Slug uniqueness is deliberately GLOBAL and deliberately partial.
--
-- Unlike a workspace name, an organisation slug is a customer-facing
-- identifier that may end up in a URL or an invite link, so two customers
-- cannot hold the same one. It is scoped globally *because* it is the public
-- identity — which is the opposite of the rule for tenant-created content, and
-- the exception is called out here so it is not read as an oversight.
CREATE UNIQUE INDEX IF NOT EXISTS organisations_slug_key
    ON organisations (slug) WHERE slug IS NOT NULL;

-- Organisation NAME is not globally unique. Two unrelated customers may both
-- be called "Acme"; refusing the second is a support ticket, not a security
-- control.

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. workspaces — the collaboration and content boundary
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS workspaces (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    organisation_id UUID NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL,
    is_default      BOOLEAN NOT NULL DEFAULT FALSE,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'archived')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at     TIMESTAMPTZ
);

-- Slug is unique PER TENANT, not globally — this is the ordinary rule, and the
-- reason is the one the classification work kept running into: a global unique
-- constraint on a tenant-created name means the first tenant to use `main`
-- takes it from everyone, and the second tenant sees an error that looks like
-- their own input was wrong.
CREATE UNIQUE INDEX IF NOT EXISTS workspaces_tenant_slug_key
    ON workspaces (tenant_id, slug);

-- At most ONE active default workspace per tenant. A partial unique index is
-- the right tool: it constrains only the rows where `is_default` is true, so
-- any number of non-default workspaces coexist, and an archived former default
-- does not block a new one.
CREATE UNIQUE INDEX IF NOT EXISTS workspaces_one_default_per_tenant
    ON workspaces (tenant_id) WHERE is_default AND status = 'active';

CREATE INDEX IF NOT EXISTS idx_workspaces_tenant
    ON workspaces (tenant_id, created_at DESC);

-- A workspace must belong to the same tenant as its organisation.
--
-- Enforced by a composite foreign key rather than a trigger or a convention: a
-- cross-tenant workspace becomes IMPOSSIBLE TO WRITE rather than something a
-- later query has to filter out. The UNIQUE it depends on is the 1:1 index
-- above, reused rather than duplicated.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'workspaces_org_same_tenant'
    ) THEN
        ALTER TABLE organisations
            ADD CONSTRAINT organisations_tenant_id_id_key UNIQUE (tenant_id, id);
        ALTER TABLE workspaces
            ADD CONSTRAINT workspaces_org_same_tenant
            FOREIGN KEY (tenant_id, organisation_id)
            REFERENCES organisations (tenant_id, id) ON DELETE CASCADE;
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Default entities for the existing single-owner install
--
--    Fixed UUIDs + ON CONFLICT DO NOTHING = safe on every re-run. The tenant
--    itself already exists (seeded by 023); these attach an organisation and a
--    default workspace to it.
-- ─────────────────────────────────────────────────────────────────────────────

-- `ON CONFLICT DO NOTHING` does NOT make these inserts safe under row-level
-- security. PostgreSQL evaluates the INSERT policy's WITH CHECK against the
-- PROPOSED row, before conflict arbitration — so on a re-run, where section 4
-- has already forced RLS, the insert is refused outright rather than quietly
-- skipped:
--
--     ERROR: new row violates row-level security policy for table "organisations"
--
-- The migration binds no `app.tenant_id`, because it is not acting for a
-- tenant. FORCE is therefore lifted for the seed and restored in section 4.
-- See the long note in `035_memberships.sql` section 0 for the full reasoning,
-- and for why this is `NO FORCE` rather than `DISABLE`.
ALTER TABLE organisations NO FORCE ROW LEVEL SECURITY;
ALTER TABLE workspaces    NO FORCE ROW LEVEL SECURITY;

INSERT INTO organisations (id, tenant_id, name, slug)
VALUES ('53925952-13ec-585d-9ab0-7016b647d6e8'::uuid, 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid, 'Private', 'private')
ON CONFLICT (id) DO NOTHING;

INSERT INTO workspaces (id, tenant_id, organisation_id, name, slug, is_default)
VALUES ('38cdd893-d172-5afd-bd08-005902c30089'::uuid, 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid,
        '53925952-13ec-585d-9ab0-7016b647d6e8'::uuid, 'Default', 'default', TRUE)
ON CONFLICT (id) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Row-level security
--
--    Defaults come from the transaction-local GUC, so an INSERT that omits the
--    tenant gets the caller's own — the only value WITH CHECK would accept
--    anyway — and an INSERT with no tenant bound resolves to NULL, hits NOT
--    NULL and fails loudly. That is the fail-closed direction: an unscoped
--    write is refused rather than landing somewhere arbitrary.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE organisations
    ALTER COLUMN tenant_id SET DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid;
ALTER TABLE workspaces
    ALTER COLUMN tenant_id SET DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid;

ALTER TABLE organisations ENABLE ROW LEVEL SECURITY;
ALTER TABLE organisations FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON organisations;
CREATE POLICY tenant_isolation ON organisations
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspaces FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON workspaces;
CREATE POLICY tenant_isolation ON workspaces
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- ── `tenants` deliberately does NOT get RLS ─────────────────────────────────
--
-- The Phase 3A classification records `tenants` as `PLATFORM_GLOBAL` with
-- `rls.required = false` — "global by classification; protected by role grants,
-- not RLS". This migration originally added a self-only policy anyway, on the
-- reasoning that a tenant able to read the registry learns every other
-- customer's name. That reasoning is sound and the implementation was wrong,
-- for two reasons found by running it:
--
--   1. **It contradicts the approved classification.** The manifest is the
--      specification and it is test-enforced; a migration that silently
--      disagrees with it is exactly the drift Phase 3A exists to prevent.
--
--   2. **It breaks `INSERT ... ON CONFLICT`.** PostgreSQL consults the SELECT
--      policy to evaluate the arbiter index, so with a restricted SELECT the
--      whole statement fails — reported, confusingly, as a WITH CHECK
--      violation. `023_tenancy.sql` seeds the private tenant with
--      `ON CONFLICT (id) DO NOTHING`, and the existing tenancy suite uses the
--      same idiom, so the policy broke both.
--
-- Registry enumeration is a real concern and it is NOT solved here. It belongs
-- to a later batch as a GRANT and view problem: the application role should
-- read tenants through a membership-filtered view rather than the base table.
-- Recorded as R-46.
--
-- ─────────────────────────────────────────────────────────────────────────────

-- Nothing is executed against `tenants` here. An earlier draft of this file
-- added a policy and then removed it; the removal statements are gone too,
-- because `tests/tenancy/test_migration_hygiene.py` correctly flags
-- `DISABLE ROW LEVEL SECURITY` as destructive in a migration that auto-runs.
-- It was right: a cleanup for a state this file never creates is a statement
-- that can only ever remove protection somebody else added.

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. updated_at triggers, matching the convention in earlier migrations
-- ─────────────────────────────────────────────────────────────────────────────

DROP TRIGGER IF EXISTS trg_organisations_updated_at ON organisations;
CREATE TRIGGER trg_organisations_updated_at
    BEFORE UPDATE ON organisations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

DROP TRIGGER IF EXISTS trg_workspaces_updated_at ON workspaces;
CREATE TRIGGER trg_workspaces_updated_at
    BEFORE UPDATE ON workspaces
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

COMMENT ON TABLE organisations IS
    'Customer-facing business identity. 1:1 with tenant initially (CLOUD-ADR-0010); '
    'the UNIQUE on tenant_id is the entire cardinality restriction.';
COMMENT ON TABLE workspaces IS
    'Collaboration and content boundary. Exactly one active default per tenant.';
