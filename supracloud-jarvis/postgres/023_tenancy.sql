-- =============================================================================
-- 023_tenancy.sql — Phase 2: Cloud Identity and Tenant Isolation
--
-- Adds the isolation boundary that a multi-user service needs, and enforces it
-- in the DATABASE with Row-Level Security rather than only in application code.
-- Application-layer scoping is one forgotten WHERE clause away from a
-- cross-tenant read; RLS makes the database refuse regardless of the query.
--
-- Tenant context comes from ONE place: the `app.tenant_id` GUC, set per
-- connection from the authenticated Principal (see ira/core/identity/db_context.py).
-- Nothing accepts a tenant id as a query parameter, so a browser cannot supply one.
--
-- ── On reusing `tenants` ─────────────────────────────────────────────────────
-- 008_business_data.sql already created a `tenants` table as a CRM grouping for
-- the investor/client-agent pipeline. Phase 2 PROMOTES that table to the
-- isolation boundary rather than introducing a second, competing "tenant"
-- concept — two tables both called some form of tenant is how a query ends up
-- joined to the wrong one. Existing rows stay valid: every added column has a
-- default.
--
-- ── Migration safety ─────────────────────────────────────────────────────────
-- Backfill runs BEFORE the NOT NULL constraints, so an existing single-owner
-- install migrates without downtime or data loss. Rollback: 023_tenancy_down.sql.
-- Idempotent: safe to re-run.
-- =============================================================================

BEGIN;

-- ── The canonical private tenant ─────────────────────────────────────────────
-- This UUID is NOT random. It is uuid5(IRA_UUID_NAMESPACE, "tenant:private"),
-- the same value ira/core/identity/principal.py derives at runtime, so the
-- application and the database agree without either storing a lookup.
-- Changing it orphans every backfilled row.
-- The tenant UUID below was a `\set` variable. It is now a literal, because
-- `\set` is a psql METACOMMAND and the migration runner applies files through
-- asyncpg — a protocol driver, not psql — which rejects it outright:
--
--     asyncpg.exceptions.PostgresSyntaxError: syntax error at or near "\"
--
-- So this file could only ever be applied by `docker-entrypoint-initdb.d` on a
-- brand-new volume, and `python -m scripts.migrate apply` could not install a
-- fresh database at all. That was recorded as R-45 while the application still
-- applied its own migrations on boot through psql-seeded volumes; making the
-- migration command the only sanctioned path turns it into a blocker.
--
-- The value is unchanged: uuid5(IRA_UUID_NAMESPACE, 'tenant:private').

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Identity tables
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS slug       TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS edition    TEXT NOT NULL DEFAULT 'private'
    CHECK (edition IN ('private', 'cloud'));
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS status     TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'suspended', 'deleting'));
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS tenants_slug_key ON tenants (slug) WHERE slug IS NOT NULL;

INSERT INTO tenants (id, name, slug, edition, status)
VALUES ('e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid, 'IRA Private', 'private', 'private', 'active')
ON CONFLICT (id) DO UPDATE
    SET slug = EXCLUDED.slug, edition = EXCLUDED.edition;

-- Users. Distinct from `delegated_users` (015): that table is IRA Private's
-- owner-granted household accounts and keeps working untouched. This one is the
-- cloud identity record. Phase 2 links them via external_subject for the
-- private backfill; a later phase may unify them.
CREATE TABLE IF NOT EXISTS users (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email             TEXT,
    email_verified_at TIMESTAMPTZ,
    display_name      TEXT NOT NULL DEFAULT '',
    -- Identity provider that authenticated this user ("password", "google",
    -- "apple", "passkey"). No credential material is ever stored here.
    auth_provider     TEXT NOT NULL DEFAULT 'password',
    -- The provider's own subject identifier. For the private edition this is
    -- the .env admin username.
    external_subject  TEXT,
    status            TEXT NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'suspended', 'deleted')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at        TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS users_provider_subject_key
    ON users (auth_provider, external_subject) WHERE external_subject IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS users_email_key
    ON users (lower(email)) WHERE email IS NOT NULL AND deleted_at IS NULL;

-- The private owner, at the UUID principal.py derives for username "admin".
-- Real deployments with a different IRA_ADMIN_USERNAME get their row from the
-- application on first login; this seeds the common case.
INSERT INTO users (id, display_name, auth_provider, external_subject)
VALUES ('fbfaa4e1-ddfa-52ea-be1d-a2560d20b9fc'::uuid, 'Owner', 'password', 'admin')
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS tenant_memberships (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id    UUID NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'viewer',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ,
    UNIQUE (tenant_id, user_id)
);

INSERT INTO tenant_memberships (tenant_id, user_id, role)
VALUES ('e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid,
        'fbfaa4e1-ddfa-52ea-be1d-a2560d20b9fc'::uuid,
        'primary_owner')
ON CONFLICT (tenant_id, user_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS user_devices (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id       UUID NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    label         TEXT NOT NULL DEFAULT '',
    platform      TEXT NOT NULL DEFAULT '',
    last_seen_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id      UUID NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    device_id    UUID REFERENCES user_devices(id) ON DELETE SET NULL,
    -- Hash of the refresh token's jti, never the token itself: a session table
    -- that stores usable tokens turns a read of this table into account takeover.
    token_hash   TEXT,
    issued_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at   TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    ip_hash      TEXT,
    user_agent   TEXT
);

-- The user's chosen companion identity (Phase 3 fills the catalogues).
CREATE TABLE IF NOT EXISTS companions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    display_name    TEXT NOT NULL DEFAULT 'IRA',
    avatar_slug     TEXT,
    voice_id        TEXT,
    language        TEXT NOT NULL DEFAULT 'en-GB',
    personality     TEXT NOT NULL DEFAULT 'supportive',
    form_of_address TEXT NOT NULL DEFAULT '',
    memory_mode     TEXT NOT NULL DEFAULT 'ask'
                        CHECK (memory_mode IN ('off', 'ask', 'automatic')),
    autonomy_level  TEXT NOT NULL DEFAULT 'confirm_before_action',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Tenant ownership on existing customer data
--    Nullable first, backfilled, then constrained — so an existing install
--    migrates without losing a row.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE conversations     ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE messages          ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE people            ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE tasks             ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
-- trust_ledger is deliberately NOT "ON DELETE CASCADE".
--
-- 020_trust_ledger.sql installs an append-only trigger that refuses DELETE, so a
-- cascade from tenants would abort the whole tenant deletion — the constraint
-- would look correct and fail only when someone first tried to delete a tenant.
-- RESTRICT makes that conflict explicit at the point of deletion instead.
--
-- The underlying tension is real and is Phase 5's problem, not this migration's:
-- a tamper-evident audit ledger and a "delete everything about me" request pull
-- in opposite directions. Phase 5 must decide deliberately (redact the record
-- while preserving the hash chain, or expire the chain segment) rather than have
-- a cascade silently destroy audit history as a side effect of a DELETE.
ALTER TABLE trust_ledger      ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE RESTRICT;

UPDATE conversations     SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;
UPDATE messages          SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;
UPDATE memory_embeddings SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;
UPDATE people            SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;
UPDATE tasks             SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;
UPDATE trust_ledger      SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;

ALTER TABLE conversations     ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE messages          ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memory_embeddings ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE people            ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE tasks             ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE trust_ledger      ALTER COLUMN tenant_id SET NOT NULL;

ALTER TABLE conversations     ALTER COLUMN tenant_id SET DEFAULT NULL;
ALTER TABLE memory_embeddings ALTER COLUMN tenant_id SET DEFAULT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Governance tables, tenant-scoped from birth
--    Created with RLS from the start rather than retrofitted. Retrofitting
--    isolation onto a table that already holds real data is how gaps appear.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS approval_requests (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id       UUID REFERENCES users(id) ON DELETE SET NULL,
    domain        TEXT NOT NULL,
    action        TEXT NOT NULL,
    purpose       TEXT NOT NULL,
    risk          TEXT NOT NULL DEFAULT 'medium',
    status        TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'approved', 'denied', 'expired')),
    payload       JSONB NOT NULL DEFAULT '{}',
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at    TIMESTAMPTZ,
    expires_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id     UUID REFERENCES users(id) ON DELETE SET NULL,
    agent       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at    TIMESTAMPTZ,
    metadata    JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_run_id  UUID REFERENCES agent_runs(id) ON DELETE CASCADE,
    connector     TEXT NOT NULL,
    operation     TEXT NOT NULL,
    purpose       TEXT NOT NULL,
    decision      TEXT NOT NULL DEFAULT 'deny',
    called_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Result summary only. Never credentials, never raw connector payloads.
    result_summary TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS privacy_events (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id     UUID REFERENCES users(id) ON DELETE SET NULL,
    kind        TEXT NOT NULL,
    detail      JSONB NOT NULL DEFAULT '{}',
    at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Indexes
--    Every tenant-scoped read filters on tenant_id, so an index on it is not
--    optional: without one, RLS turns every query into a sequential scan.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS conversations_tenant_idx     ON conversations (tenant_id);
CREATE INDEX IF NOT EXISTS messages_tenant_idx          ON messages (tenant_id);
CREATE INDEX IF NOT EXISTS memory_embeddings_tenant_idx ON memory_embeddings (tenant_id);
CREATE INDEX IF NOT EXISTS people_tenant_idx            ON people (tenant_id);
CREATE INDEX IF NOT EXISTS tasks_tenant_idx             ON tasks (tenant_id);
CREATE INDEX IF NOT EXISTS trust_ledger_tenant_idx      ON trust_ledger (tenant_id);
CREATE INDEX IF NOT EXISTS approval_requests_tenant_idx ON approval_requests (tenant_id);
CREATE INDEX IF NOT EXISTS agent_runs_tenant_idx        ON agent_runs (tenant_id);
CREATE INDEX IF NOT EXISTS tool_calls_tenant_idx        ON tool_calls (tenant_id);
CREATE INDEX IF NOT EXISTS privacy_events_tenant_idx    ON privacy_events (tenant_id);
CREATE INDEX IF NOT EXISTS memberships_tenant_idx       ON tenant_memberships (tenant_id);
CREATE INDEX IF NOT EXISTS user_sessions_tenant_idx     ON user_sessions (tenant_id);
CREATE INDEX IF NOT EXISTS user_devices_tenant_idx      ON user_devices (tenant_id);
CREATE INDEX IF NOT EXISTS companions_tenant_idx        ON companions (tenant_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Row-Level Security
--
-- The policy is deliberately uniform and boring: a row is visible only when its
-- tenant_id equals the `app.tenant_id` GUC.
--
-- `current_setting('app.tenant_id', true)` returns NULL when the GUC was never
-- set. NULL = NULL is NULL, not true, so **an unset tenant context matches no
-- rows** — missing context denies by construction rather than by a check
-- somebody has to remember to write. That is the single most important
-- property in this file.
--
-- NULLIF(..., '') is load-bearing, not cosmetic. Resetting a GUC leaves it as
-- the EMPTY STRING rather than NULL, and ''::uuid raises
-- "invalid input syntax for type uuid". That would make a cleared context throw
-- a database error instead of returning no rows — still fail-closed, but an
-- error application code might catch and treat as a transient fault, and a
-- confusing one to debug. NULLIF maps empty to NULL so a cleared context denies
-- exactly the way an unset one does. (Caught by
-- tests/tenancy/test_tenant_isolation_rls.py::test_missing_tenant_context_returns_nothing.)
--
-- WITH CHECK uses the same expression, so a tenant cannot INSERT or UPDATE a
-- row INTO another tenant either. A read-only policy would leave writes able to
-- cross the boundary.
--
-- FORCE ROW LEVEL SECURITY makes the policy apply to the table owner too.
-- Without it the application role — which typically owns its tables — bypasses
-- RLS silently, and every test below would pass while protecting nothing.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t TEXT;
    scoped TEXT[] := ARRAY[
        'conversations', 'messages', 'memory_embeddings', 'people', 'tasks',
        'trust_ledger', 'approval_requests', 'agent_runs', 'tool_calls',
        'privacy_events', 'tenant_memberships', 'user_devices', 'user_sessions',
        'companions'
    ];
BEGIN
    FOREACH t IN ARRAY scoped LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format($f$
            CREATE POLICY tenant_isolation ON %I
                USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        $f$, t);
    END LOOP;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. Harden the two policies that already existed
--
-- 008_business_data.sql put RLS on `investors` and `client_agents` using the
-- SAME app.tenant_id GUC, but without the NULLIF guard — so once this migration
-- starts setting that GUC, a CLEARED context makes those two tables raise
-- "invalid input syntax for type uuid" instead of returning nothing.
--
-- Redefined here rather than by editing 008, because 008 has already run on
-- existing installs and editing an applied migration changes nothing on them.
-- Behaviour is otherwise identical: same GUC, same comparison.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t TEXT;
    business TEXT[] := ARRAY['investors', 'client_agents'];
    policy_name TEXT;
BEGIN
    FOREACH t IN ARRAY business LOOP
        IF EXISTS (SELECT 1 FROM pg_class WHERE relname = t) THEN
            policy_name := 'tenant_isolation_' || t;
            EXECUTE format('DROP POLICY IF EXISTS %I ON %I', policy_name, t);
            EXECUTE format($f$
                CREATE POLICY %I ON %I
                    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
                    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            $f$, policy_name, t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;
    END LOOP;
END
$$;

COMMIT;
