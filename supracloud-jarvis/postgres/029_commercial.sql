-- =============================================================================
-- 029_commercial.sql — Phase 8: model gateway, quotas, and break-glass access
--
-- Four tables:
--
--   model_providers      global catalogue: what each provider DOES with data
--   usage_quotas         per-tenant ceilings, checked before a call
--   model_usage          metering: tokens and cost, never prompt or response
--   admin_access_grants  a user authorising an administrator to look, briefly
--   admin_access_log     every read taken under such a grant
--
-- ── Unknown retention is a real value, and must stay one ─────────────────────
--
-- `model_providers.retains_prompts` is NULLABLE on purpose. The Phase 1 seam is
-- explicit:
--
--     Does the provider retain prompts? None = not known, which must be SHOWN
--     as "not known" rather than rendered as "no".
--
-- So NULL is not missing data to be cleaned up — it is the honest answer for a
-- provider whose terms do not say. `NOT NULL DEFAULT FALSE` here would be the
-- single most misleading line in this migration: it would silently convert "we
-- have not checked" into "it does not retain", and the routing policy would then
-- happily send health notes there.
--
-- `disclosure_source` and `disclosure_checked_at` record WHERE the answer came
-- from and WHEN, because a retention claim with no provenance is a rumour.
--
-- ── Usage rows cannot hold a prompt ──────────────────────────────────────────
--
-- `model_usage` is metering, and metering is the table most likely to be joined,
-- exported and shipped to a billing system. So it is built so a prompt cannot be
-- in it. Precisely: every column is a UUID, a timestamp, an integer, a numeric,
-- or a TEXT column that is either CHECK-constrained to a fixed vocabulary
-- (`tier`, `sensitivity`, `privacy_mode`, `outcome`, `locality`), a foreign key
-- (`provider_id`), or LENGTH-BOUNDED to 128 characters (`model`, which holds a
-- provider's model name — "llama3.1:8b" — not user content). No JSONB and no
-- BYTEA anywhere. The same device as `realtime_events` in 027, and the length
-- bound is stated rather than glossed as "constrained": a bare TEXT column here
-- would be a place a prompt could sit.
--
-- ── Break-glass access is bounded by the schema, not only by code ────────────
--
-- The threat model names the Phase 8 risk: *"administrators reading user content
-- without time-bound, logged, user-authorised access."* All three words are
-- constraints here:
--
--   time-bound      `expires_at` is required, must be after `granted_at`, and
--                   MAX_GRANT_HOURS caps the window.
--   user-authorised `user_authorised_at` is NOT NULL. There is no column for
--                   "authorised by an administrator" — a grant nobody
--                   authorised cannot be inserted.
--   logged          `admin_access_log` references a grant, and a trigger refuses
--                   a log row whose grant is missing, revoked or outside its
--                   window.
--
-- The honest limit: a schema cannot force code to log. What it CAN do is make
-- every logged access provably within an authorised window, so an audit shows
-- either a valid grant or nothing at all. The application chokepoint is
-- `core/models/admin_access.py`, and an AST test asserts nothing else reads
-- another user's content.
--
-- Additive. Idempotent. Rollback: rollback/029_commercial_down.sql.
-- =============================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Provider catalogue — global reference data, no RLS.
--
-- What each provider does with a request. Every column exists because a
-- disclosure surface or a routing decision needs it.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS model_providers (
    provider_id         TEXT PRIMARY KEY,
    display_name        TEXT NOT NULL CHECK (length(trim(display_name)) > 0),

    -- Where inference physically happens. Drives every privacy claim, so a
    -- provider that cannot state it cannot be routed to — which is why this is
    -- NOT NULL and enumerated.
    locality            TEXT NOT NULL
                            CHECK (locality IN ('local', 'self_hosted', 'external')),

    -- NULLABLE ON PURPOSE. NULL = "not stated", which the routing policy treats
    -- as "may retain" and the UI must show as "not known". See the header.
    retains_prompts     BOOLEAN,
    trains_on_data      BOOLEAN,

    -- Where the answer came from, and when. A retention claim with no
    -- provenance is a rumour.
    disclosure_source   TEXT NOT NULL DEFAULT '',
    disclosure_checked_at TIMESTAMPTZ,

    -- Coarse and enumerated, like 027's provider_region: a free-text region is
    -- a place to hide an identifier, and anything finer is not needed to be
    -- truthful about jurisdiction.
    region              TEXT NOT NULL DEFAULT 'unknown'
                            CHECK (region IN ('unknown', 'on-device', 'eu', 'uk',
                                              'us', 'apac', 'other')),

    tiers               TEXT[] NOT NULL DEFAULT '{}',

    -- Cost, for the ceiling. NUMERIC not float: money compared with floats
    -- produces off-by-a-penny ceilings that look like bugs in the quota.
    cost_per_1k_prompt_tokens     NUMERIC(12,6) NOT NULL DEFAULT 0
                                      CHECK (cost_per_1k_prompt_tokens >= 0),
    cost_per_1k_completion_tokens NUMERIC(12,6) NOT NULL DEFAULT 0
                                      CHECK (cost_per_1k_completion_tokens >= 0),

    enabled_in_private  BOOLEAN NOT NULL DEFAULT TRUE,
    enabled_in_cloud    BOOLEAN NOT NULL DEFAULT FALSE,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- A local provider that claims to retain prompts elsewhere, or to train on
    -- them, is a contradiction — local means on this machine.
    CONSTRAINT local_providers_keep_nothing_elsewhere CHECK (
        locality <> 'local'
        OR (retains_prompts IS NOT DISTINCT FROM FALSE
            AND trains_on_data IS NOT DISTINCT FROM FALSE)
    ),
    -- A stated posture needs a source. An unstated one (NULL) does not, and
    -- must not be forced to invent one.
    CONSTRAINT stated_postures_cite_a_source CHECK (
        (retains_prompts IS NULL AND trains_on_data IS NULL)
        OR length(trim(disclosure_source)) > 0
    )
);

COMMENT ON COLUMN model_providers.retains_prompts IS
    'NULL means the provider has not stated whether it retains prompts. That is '
    'a real answer, not missing data: the routing policy treats it as "may '
    'retain" and the UI must show "not known". Never default this to FALSE.';

-- The local path, stated honestly. Mirrors LOCAL_OLLAMA_DISCLOSURE in
-- core/interfaces/model_provider.py; a drift test asserts the two agree.
INSERT INTO model_providers
    (provider_id, display_name, locality, retains_prompts, trains_on_data,
     disclosure_source, region, tiers, enabled_in_private, enabled_in_cloud)
VALUES
    ('ollama-local', 'Local Ollama', 'local', FALSE, FALSE,
     'runs on this machine; nothing is transmitted', 'on-device',
     '{fast,deep,reasoning,embedding}', TRUE, FALSE)
ON CONFLICT (provider_id) DO UPDATE SET
    display_name      = EXCLUDED.display_name,
    locality          = EXCLUDED.locality,
    retains_prompts   = EXCLUDED.retains_prompts,
    trains_on_data    = EXCLUDED.trains_on_data,
    disclosure_source = EXCLUDED.disclosure_source,
    region            = EXCLUDED.region,
    tiers             = EXCLUDED.tiers,
    updated_at        = NOW();

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Quotas — per tenant, checked BEFORE a call.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS usage_quotas (
    tenant_id           UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,

    -- NULL means unmetered. Explicitly distinct from 0, which means "no calls
    -- permitted" — a build that conflated them would either bill nothing or
    -- refuse everything, and both look like the same bug from outside.
    monthly_token_limit     BIGINT CHECK (monthly_token_limit IS NULL
                                          OR monthly_token_limit >= 0),
    monthly_cost_limit      NUMERIC(12,4) CHECK (monthly_cost_limit IS NULL
                                                 OR monthly_cost_limit >= 0),
    daily_request_limit     INTEGER CHECK (daily_request_limit IS NULL
                                           OR daily_request_limit >= 0),

    -- What happens at the ceiling. 'refuse' is the default: silently switching
    -- to a cheaper provider would be a privacy decision made by a billing
    -- rule, which is exactly the wrong place for one.
    on_exceeded          TEXT NOT NULL DEFAULT 'refuse'
                             CHECK (on_exceeded IN ('refuse', 'local_only')),

    period_started_at    TIMESTAMPTZ NOT NULL DEFAULT date_trunc('month', NOW()),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Usage — metering that cannot hold a prompt. See the header.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS model_usage (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             UUID REFERENCES users(id) ON DELETE SET NULL,

    provider_id         TEXT NOT NULL REFERENCES model_providers(provider_id),
    -- Where the request PHYSICALLY went, captured at call time.
    --
    -- Deliberately denormalised rather than joined from model_providers: a
    -- provider's locality can be edited, and this row must keep saying what was
    -- true when the call happened. "Where did my words go?" must not be
    -- answerable differently next month because a catalogue row changed.
    locality            TEXT NOT NULL DEFAULT 'local'
                            CHECK (locality IN ('local', 'self_hosted', 'external')),
    -- The model name is provider vocabulary, not user content — "llama3.1:8b".
    -- Length-bounded so it cannot become somewhere a prompt sits: no model name
    -- is 128 characters, and a value that long is a bug worth failing on.
    model               TEXT NOT NULL DEFAULT '' CHECK (length(model) <= 128),
    tier                TEXT NOT NULL DEFAULT 'fast'
                            CHECK (tier IN ('fast', 'deep', 'reasoning',
                                            'vision', 'embedding')),

    -- The classification the routing decision was made on, kept so a later
    -- audit can check the decision rather than re-guessing it.
    sensitivity         TEXT NOT NULL DEFAULT 'normal'
                            CHECK (sensitivity IN ('normal', 'personal', 'health',
                                                   'financial', 'family',
                                                   'credential')),
    privacy_mode        TEXT NOT NULL DEFAULT 'local_only'
                            CHECK (privacy_mode IN ('local_only', 'local_first',
                                                    'balanced', 'external_ok')),

    outcome             TEXT NOT NULL DEFAULT 'ok'
                            CHECK (outcome IN ('ok', 'refused_privacy',
                                               'refused_quota', 'error')),

    prompt_tokens       INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens   INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    cost                NUMERIC(12,6) NOT NULL DEFAULT 0 CHECK (cost >= 0),
    latency_ms          INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),

    at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- A refusal consumed no tokens and cost nothing. A row claiming otherwise
    -- is either a billing error or a refusal that was not one.
    CONSTRAINT refusals_consume_nothing CHECK (
        outcome NOT IN ('refused_privacy', 'refused_quota')
        OR (prompt_tokens = 0 AND completion_tokens = 0 AND cost = 0)
    ),
    -- Credentials never leave the machine, so a usage row pairing them with a
    -- successful non-local call is a contradiction the schema refuses. Checked
    -- against the denormalised `locality` above rather than a subquery — a
    -- CHECK cannot contain one, and the captured value is the right thing to
    -- check anyway.
    CONSTRAINT credentials_never_left_the_machine CHECK (
        sensitivity <> 'credential' OR outcome <> 'ok' OR locality = 'local'
    ),
    -- local_only must never have produced a successful non-local call. This is
    -- the routing guarantee written into the billing record, so a violation is
    -- visible in the data and not only in the code that made it.
    CONSTRAINT local_only_stayed_local CHECK (
        privacy_mode <> 'local_only' OR outcome <> 'ok' OR locality = 'local'
    )
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Break-glass access. Time-bound, user-authorised, logged.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS admin_access_grants (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- Whose data may be looked at.
    subject_user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- Who may look. A string rather than a users FK because a support engineer
    -- is not necessarily a tenant member.
    admin_identity      TEXT NOT NULL CHECK (length(trim(admin_identity)) > 0),

    -- Why. Required, because "support investigation" is the minimum a user is
    -- owed when someone reads their conversations.
    reason              TEXT NOT NULL CHECK (length(trim(reason)) >= 10),
    scope               TEXT NOT NULL DEFAULT 'conversations'
                            CHECK (scope IN ('conversations', 'memories',
                                             'receipts', 'usage')),

    -- USER-AUTHORISED. NOT NULL, and there is deliberately no column for
    -- "authorised by an administrator": a grant nobody authorised cannot exist.
    user_authorised_at  TIMESTAMPTZ NOT NULL,

    granted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at          TIMESTAMPTZ NOT NULL,
    revoked_at          TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT grant_expires_after_it_starts CHECK (expires_at > granted_at),
    -- A window longer than a working day is not "break glass", it is access.
    CONSTRAINT grant_window_is_bounded CHECK (
        expires_at <= granted_at + INTERVAL '8 hours'
    ),
    -- Authorisation cannot post-date the grant it authorises.
    CONSTRAINT authorisation_precedes_the_grant CHECK (
        user_authorised_at <= granted_at + INTERVAL '1 minute'
    )
);

CREATE TABLE IF NOT EXISTS admin_access_log (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    grant_id            UUID NOT NULL REFERENCES admin_access_grants(id)
                            ON DELETE RESTRICT,

    admin_identity      TEXT NOT NULL,
    -- Shape only, like an Action Receipt: "12 message(s)", never their text.
    what_was_read       TEXT NOT NULL DEFAULT '',
    record_count        INTEGER NOT NULL DEFAULT 0 CHECK (record_count >= 0),

    at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Indexes.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS model_usage_tenant_period_idx
    ON model_usage (tenant_id, at DESC);
CREATE INDEX IF NOT EXISTS model_usage_billing_idx
    ON model_usage (tenant_id, at) WHERE outcome = 'ok';
CREATE INDEX IF NOT EXISTS admin_grants_live_idx
    ON admin_access_grants (tenant_id, subject_user_id, expires_at)
    WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS admin_access_log_recent_idx
    ON admin_access_log (tenant_id, at DESC);

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. Row-Level Security.
--
-- model_providers is intentionally absent: global reference data with no tenant
-- column. usage_quotas IS scoped — a tenant must not read another's ceilings.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t TEXT;
    scoped TEXT[] := ARRAY[
        'usage_quotas',
        'model_usage',
        'admin_access_grants',
        'admin_access_log'
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
-- 7. A logged access must fall inside a live, authorised grant.
--
-- This is what makes the audit trustworthy: it cannot contain an entry that was
-- outside a window the user authorised. It does NOT stop code from failing to
-- log — a schema cannot — but it means an auditor reading this table sees only
-- authorised access, and an unauthorised read leaves the log EMPTY rather than
-- leaving a plausible-looking row.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION admin_access_within_grant()
RETURNS TRIGGER AS $$
DECLARE
    g RECORD;
BEGIN
    SELECT * INTO g FROM admin_access_grants WHERE id = NEW.grant_id;

    IF g IS NULL THEN
        RAISE EXCEPTION
            'Cannot log administrator access: grant % does not exist. Every '
            'read of a user''s content must sit inside a grant that user '
            'authorised.', NEW.grant_id;
    END IF;
    IF g.revoked_at IS NOT NULL THEN
        RAISE EXCEPTION
            'Cannot log administrator access: grant % was revoked at %.',
            NEW.grant_id, g.revoked_at;
    END IF;
    IF NEW.at < g.granted_at OR NEW.at > g.expires_at THEN
        RAISE EXCEPTION
            'Cannot log administrator access at %: grant % is valid only from '
            '% to %. Access outside the authorised window is not permitted.',
            NEW.at, NEW.grant_id, g.granted_at, g.expires_at;
    END IF;
    IF NEW.tenant_id IS DISTINCT FROM g.tenant_id THEN
        RAISE EXCEPTION
            'Cannot log administrator access: the log entry''s tenant does not '
            'match the grant''s.';
    END IF;
    IF NEW.admin_identity IS DISTINCT FROM g.admin_identity THEN
        RAISE EXCEPTION
            'Cannot log administrator access: grant % authorises %, not %.',
            NEW.grant_id, g.admin_identity, NEW.admin_identity;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS admin_access_must_be_granted ON admin_access_log;
CREATE TRIGGER admin_access_must_be_granted
    BEFORE INSERT ON admin_access_log
    FOR EACH ROW EXECUTE FUNCTION admin_access_within_grant();

-- ─────────────────────────────────────────────────────────────────────────────
-- 8. The access log and the grants are evidence: append-only.
--
-- The one permitted UPDATE on a grant is a revocation. Nothing may edit the log.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION admin_access_log_append_only()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'admin_access_log is append-only: an administrator who can edit the '
        'record of what they read is not audited.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS admin_access_log_no_rewrite ON admin_access_log;
CREATE TRIGGER admin_access_log_no_rewrite
    BEFORE UPDATE OR DELETE ON admin_access_log
    FOR EACH ROW EXECUTE FUNCTION admin_access_log_append_only();

CREATE OR REPLACE FUNCTION admin_grants_revoke_only()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'admin_access_grants is append-only: DELETE is not permitted. A '
            'grant is the authorisation a user gave; removing it would erase '
            'the basis for accesses already logged against it.';
    END IF;
    IF NEW.revoked_at IS NULL THEN
        RAISE EXCEPTION
            'The only permitted change to a grant is revoking it (setting '
            'revoked_at). Extending a window, changing the admin, or widening '
            'the scope requires a new grant the user authorises.';
    END IF;
    IF OLD.revoked_at IS NOT NULL THEN
        RAISE EXCEPTION 'This grant was already revoked at %.', OLD.revoked_at;
    END IF;
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.subject_user_id IS DISTINCT FROM OLD.subject_user_id
       OR NEW.admin_identity IS DISTINCT FROM OLD.admin_identity
       OR NEW.scope IS DISTINCT FROM OLD.scope
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.user_authorised_at IS DISTINCT FROM OLD.user_authorised_at THEN
        RAISE EXCEPTION
            'A revocation may not alter what the grant authorised.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS admin_grants_revoke_only ON admin_access_grants;
CREATE TRIGGER admin_grants_revoke_only
    BEFORE UPDATE OR DELETE ON admin_access_grants
    FOR EACH ROW EXECUTE FUNCTION admin_grants_revoke_only();

COMMIT;
