-- =============================================================================
-- 025_governance.sql — Phase 4: User Constitution and Permission Broker
--
-- IRA's primary differentiator, in schema form: the user's permanent rules, the
-- grants those rules permit, and the record of every decision made under them.
--
-- ── What decides what ────────────────────────────────────────────────────────
--
--   tool_policies       The DEPLOYMENT's baseline. "email.send is high risk and
--                       always needs confirmation." Global, operator-curated,
--                       not tenant-scoped — it is a property of the build, not
--                       of a customer. A tenant cannot edit it, and cannot make
--                       an action LESS restricted than this baseline.
--
--   constitution_rules  The USER's permanent rules, layered on top. "Never send
--                       an email without confirmation." "Never remember health
--                       conversations." Tenant-scoped customer data.
--
--   permission_grants   What the user has actually granted, to which connector,
--                       for which purpose, until when.
--
--   permission_decisions  The append-only record of what was decided and why.
--
--   approval_events     What happened to an approval request (023 created
--                       `approval_requests`; this is its event log).
--
-- ── The direction of authority ───────────────────────────────────────────────
--
-- A constitution rule may only make things STRICTER than the tool policy, never
-- looser. That is enforced in application code (core/governance/broker.py) and
-- is the reason `tool_policies.max_effect` exists: the policy states the most
-- permissive answer possible for an action, and nothing downstream can exceed
-- it. A user who writes "always allow email sending" gets confirmation anyway,
-- because the deployment says that action always confirms.
--
-- This is deliberate and it is the opposite of how most permission systems
-- work. The reasoning: a user's rules exist to CONSTRAIN their assistant, and a
-- rule engine that can also be used to remove safety rails is one prompt
-- injection away from writing itself a permissive rule.
--
-- ── Fail closed ──────────────────────────────────────────────────────────────
--
-- Every default in this file denies. `tool_policies.default_effect` defaults to
-- 'deny'; `permission_grants` are inert until `granted_at` is set and are dead
-- once `revoked_at` is; a decision row with no matching rule records
-- 'deny'/'fail-closed-default'.
--
-- Additive only. Idempotent. Rollback: rollback/025_governance_down.sql.
-- =============================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Tool policies — the deployment's baseline. GLOBAL, not tenant-scoped.
--
-- Same call as Phase 3's catalogues: this is operator-curated reference data
-- describing what this BUILD permits, identical for every tenant. Copying it
-- per tenant is how a safety rail ends up tightened for some customers and not
-- others.
--
-- `max_effect` is the ceiling, `default_effect` is the answer when no rule and
-- no grant applies. Both default to the strict end.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tool_policies (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    domain          TEXT NOT NULL,
    action          TEXT NOT NULL,
    -- The most permissive effect this action may EVER resolve to, whatever a
    -- user's constitution says. 'require_confirmation' here means the action
    -- can never become automatic.
    max_effect      TEXT NOT NULL DEFAULT 'require_confirmation'
                        CHECK (max_effect IN ('allow', 'deny', 'require_confirmation',
                                              'allow_for_session', 'allow_for_routine')),
    -- The answer when nothing else matches.
    default_effect  TEXT NOT NULL DEFAULT 'deny'
                        CHECK (default_effect IN ('allow', 'deny', 'require_confirmation',
                                                  'allow_for_session', 'allow_for_routine')),
    -- Baseline risk. The broker may raise it from context; it may never lower
    -- it (mirrors trust/action_guard.py, where risk is derived and a caller can
    -- only raise it).
    risk            TEXT NOT NULL DEFAULT 'medium'
                        CHECK (risk IN ('low', 'medium', 'high', 'critical')),
    -- True when the action changes something outside IRA. Side-effecting
    -- actions must reach Action Guard before they run.
    side_effecting  BOOLEAN NOT NULL DEFAULT TRUE,
    -- The Shadow World action kind to simulate under, when there is one.
    -- NULL means "no simulator" — which, for a side-effecting action, the
    -- broker treats as a reason to require confirmation rather than to skip it.
    shadow_kind     TEXT,
    description     TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (domain, action)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Constitution rules — the user's permanent rules. Tenant-scoped.
--
-- Structured, not free text. A rule is (domain, action, effect, conditions,
-- priority), and the engine that reads it is ordinary application code.
--
-- There is deliberately NO column holding prompt text or natural-language
-- instructions for the model. A "rule" the model is asked to obey is not a
-- rule, it is a request — and the whole point of this table is that IRA's
-- constraints do not depend on a model's willingness to follow them.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS constitution_rules (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id      UUID REFERENCES users(id) ON DELETE CASCADE,

    -- '*' matches any domain/action. Kept as a literal rather than NULL so the
    -- matching rule is visible in the row and in the audit record.
    domain       TEXT NOT NULL DEFAULT '*',
    action       TEXT NOT NULL DEFAULT '*',
    effect       TEXT NOT NULL
                     CHECK (effect IN ('allow', 'deny', 'require_confirmation',
                                       'allow_for_session', 'allow_for_routine')),
    conditions   JSONB NOT NULL DEFAULT '{}',
    -- Higher wins. Ties break toward the stricter effect (application code).
    priority     INTEGER NOT NULL DEFAULT 100,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,

    -- What the user typed, for display only. NEVER read by the engine.
    label        TEXT NOT NULL DEFAULT '',
    -- Where the rule came from: the user, onboarding defaults, or a template.
    origin       TEXT NOT NULL DEFAULT 'user'
                     CHECK (origin IN ('user', 'onboarding', 'template', 'system')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Permission grants — what the user has actually allowed, and until when.
--
-- A grant is inert unless it is granted, unexpired and unrevoked. Those are
-- three separate columns rather than one status, because "never granted",
-- "expired" and "revoked" are different facts and the Privacy Centre (Phase 5)
-- has to tell a user which one applies.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS permission_grants (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             UUID REFERENCES users(id) ON DELETE CASCADE,

    connector           TEXT NOT NULL,
    resource            TEXT NOT NULL DEFAULT '*',
    scope               TEXT NOT NULL,
    -- Why the user granted it. Shown back to them, and compared against the
    -- purpose of the request: a grant given for one purpose does not authorise
    -- a different one.
    purpose             TEXT NOT NULL,

    granted_at          TIMESTAMPTZ,
    expires_at          TIMESTAMPTZ,
    revoked_at          TIMESTAMPTZ,

    confirmation_policy TEXT NOT NULL DEFAULT 'always'
                            CHECK (confirmation_policy IN ('always', 'once_per_session',
                                                           'once_per_routine', 'never')),
    retention_policy    TEXT NOT NULL DEFAULT 'none',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Permission decisions — the append-only record of what was decided.
--
-- Written for EVERY decision, including denials. A log that only records what
-- happened cannot answer "why did IRA refuse?", which is the question a user
-- actually asks.
--
-- `ticket_id` links a decision to the one-shot execution ticket it minted (see
-- core/governance/ticket.py), so an executed action can always be traced back
-- to the decision that permitted it — and an execution with no decision has no
-- row here, which is itself the anomaly worth alerting on.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS permission_decisions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             UUID REFERENCES users(id) ON DELETE SET NULL,

    domain              TEXT NOT NULL,
    action              TEXT NOT NULL,
    resource            TEXT NOT NULL DEFAULT '',
    purpose             TEXT NOT NULL,

    effect              TEXT NOT NULL
                            CHECK (effect IN ('allow', 'deny', 'require_confirmation',
                                              'allow_for_session', 'allow_for_routine')),
    risk                TEXT NOT NULL DEFAULT 'medium'
                            CHECK (risk IN ('low', 'medium', 'high', 'critical')),
    reason              TEXT NOT NULL DEFAULT '',
    -- Which rule/grant/policy decided it.
    source              TEXT NOT NULL DEFAULT '',
    rule_id             UUID REFERENCES constitution_rules(id) ON DELETE SET NULL,
    grant_id            UUID REFERENCES permission_grants(id) ON DELETE SET NULL,
    approval_request_id UUID REFERENCES approval_requests(id) ON DELETE SET NULL,

    ticket_id           UUID,
    -- True when the model proposed this rather than the user asking directly.
    model_proposed      BOOLEAN NOT NULL DEFAULT TRUE,
    decided_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Approval events — the life of an approval request.
--
-- 023 created `approval_requests` (the request and its current status). This is
-- its event log: requested → shown → approved/denied → expired. Separate,
-- because the current status is a mutable field and the history must not be.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS approval_events (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    approval_request_id UUID NOT NULL REFERENCES approval_requests(id) ON DELETE CASCADE,
    event               TEXT NOT NULL
                            CHECK (event IN ('requested', 'shown', 'approved',
                                             'denied', 'expired', 'withdrawn')),
    actor               TEXT NOT NULL DEFAULT 'user',
    detail              JSONB NOT NULL DEFAULT '{}',
    at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. Indexes
-- ─────────────────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS constitution_rules_tenant_idx   ON constitution_rules (tenant_id);
CREATE INDEX IF NOT EXISTS constitution_rules_lookup_idx   ON constitution_rules (tenant_id, domain, action) WHERE enabled;
CREATE INDEX IF NOT EXISTS permission_grants_tenant_idx    ON permission_grants (tenant_id);
CREATE INDEX IF NOT EXISTS permission_grants_lookup_idx    ON permission_grants (tenant_id, connector, scope);
CREATE INDEX IF NOT EXISTS permission_decisions_tenant_idx ON permission_decisions (tenant_id);
CREATE INDEX IF NOT EXISTS permission_decisions_recent_idx ON permission_decisions (tenant_id, decided_at DESC);
CREATE INDEX IF NOT EXISTS approval_events_tenant_idx      ON approval_events (tenant_id);
CREATE INDEX IF NOT EXISTS approval_events_request_idx     ON approval_events (approval_request_id, at);
CREATE INDEX IF NOT EXISTS tool_policies_lookup_idx        ON tool_policies (domain, action);

-- ─────────────────────────────────────────────────────────────────────────────
-- 7. Row-Level Security on the customer-data tables
--
-- Same uniform policy as 023 and 024. `tool_policies` is NOT in this list, on
-- purpose — it holds no customer data (see the header). Both halves are
-- asserted by tests/tenancy/test_governance_isolation_rls.py.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t TEXT;
    scoped TEXT[] := ARRAY[
        'constitution_rules', 'permission_grants', 'permission_decisions',
        'approval_events'
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
-- 8. Append-only guard on permission_decisions
--
-- The decision log is evidence. A decision that can be edited after the fact
-- cannot answer "what was IRA allowed to do at the time", which is the only
-- question the log exists for. UPDATE and DELETE are refused outright — the
-- same posture 020_trust_ledger.sql takes, and for the same reason.
--
-- Note the consequence, deliberately accepted: a tenant deletion cannot cascade
-- through this table either. `tenant_id` is ON DELETE CASCADE above, which would
-- fire this trigger and abort. Phase 5 owns the audit-vs-erasure decision for
-- the whole system (023 flagged the same tension on trust_ledger); until then a
-- tenant delete failing loudly is better than audit history vanishing quietly.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION permission_decisions_append_only()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'permission_decisions is append-only: % is not permitted. The decision '
        'log is evidence of what IRA was allowed to do at a point in time.',
        TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS permission_decisions_no_update ON permission_decisions;
CREATE TRIGGER permission_decisions_no_update
    BEFORE UPDATE OR DELETE ON permission_decisions
    FOR EACH ROW EXECUTE FUNCTION permission_decisions_append_only();

-- ─────────────────────────────────────────────────────────────────────────────
-- 9. Seed: the deployment's tool policy baseline
--
-- These are the actions IRA can currently attempt. Every one defaults to the
-- strict end, and the ones the transformation brief names as never-automatic
-- carry a `max_effect` of 'require_confirmation' or 'deny' so no user rule and
-- no grant can make them silent.
--
-- 'deny' as max_effect means the action cannot be performed at all in this
-- build — it is here so the vocabulary is complete and the refusal is a
-- deliberate policy row rather than a missing one.
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO tool_policies (domain, action, max_effect, default_effect, risk, side_effecting, shadow_kind, description)
VALUES
    -- Email. Reading is a grant question; sending can never be automatic, and
    -- deletion is not offered at all (the brief: "Do not support email deletion
    -- initially").
    ('email',    'search',   'allow_for_session',      'deny',               'low',      FALSE, NULL,                 'Search the mailbox'),
    ('email',    'read',     'require_confirmation',   'deny',               'medium',   FALSE, NULL,                 'Read a selected message'),
    ('email',    'draft',    'allow_for_session',      'require_confirmation','low',     FALSE, NULL,                 'Prepare a draft — never sends'),
    ('email',    'send',     'require_confirmation',   'require_confirmation','high',    TRUE,  'email_send',         'Send an email'),
    ('email',    'delete',   'deny',                   'deny',               'critical', TRUE,  NULL,                 'Not supported in this build'),

    -- Calendar.
    ('calendar', 'read',     'allow_for_session',      'deny',               'low',      FALSE, NULL,                 'Read the calendar'),
    ('calendar', 'create',   'require_confirmation',   'require_confirmation','medium',  TRUE,  'calendar_change',    'Create an event'),
    ('calendar', 'delete',   'require_confirmation',   'deny',               'high',     TRUE,  'calendar_change',    'Delete an event'),

    -- Files. Deletion is high risk and reaches Action Guard, which holds the
    -- protected-path denylist and takes a rollback snapshot first.
    ('files',    'read',     'allow_for_session',      'deny',               'low',      FALSE, NULL,                 'Read a file'),
    ('files',    'write',    'require_confirmation',   'require_confirmation','medium',  TRUE,  'file_change',        'Write a file'),
    ('files',    'delete',   'require_confirmation',   'deny',               'high',     TRUE,  'file_change',        'Delete a file'),

    -- Memory. Deleting a memory is the user's right and is confirmed, never
    -- automatic — an assistant that can silently forget is one that can be made
    -- to forget.
    ('memory',   'read',     'allow',                  'allow',              'low',      FALSE, NULL,                 'Read stored memory'),
    ('memory',   'write',    'allow_for_session',      'require_confirmation','low',     FALSE, NULL,                 'Store a memory'),
    ('memory',   'delete',   'require_confirmation',   'require_confirmation','medium',  TRUE,  'memory_deletion',    'Delete a memory'),

    -- Shell and system. Graded by the Action Guard's existing two gates.
    ('shell',    'run',      'require_confirmation',   'deny',               'high',     TRUE,  'shell_command',      'Run a shell command'),

    -- Outbound network. Off by default since Phase 0; a policy row makes the
    -- refusal explicit rather than incidental.
    ('web',      'search',   'require_confirmation',   'deny',               'medium',   FALSE, 'external_api_call',  'Search the web'),
    ('web',      'fetch',    'require_confirmation',   'deny',               'medium',   FALSE, 'external_api_call',  'Fetch a URL'),

    -- Excluded from the consumer cloud entirely (brief §7). Present as an
    -- explicit deny so an attempt is refused BY POLICY and leaves a decision
    -- row, rather than falling through an unknown-action path.
    ('payment',  'execute',  'deny',                   'deny',               'critical', TRUE,  NULL,                 'Never supported'),
    ('social',   'publish',  'deny',                   'deny',               'critical', TRUE,  NULL,                 'Never supported'),
    ('security', 'change',   'deny',                   'deny',               'critical', TRUE,  'permission_change',  'Not changeable by an agent')
ON CONFLICT (domain, action) DO NOTHING;

COMMIT;
