-- =============================================================================
-- 028_connectors.sql — Phase 7: connectors, OAuth, and Action Receipts
--
-- Four tables, each with one job:
--
--   connector_scopes       global reference — which scopes exist, what each one
--                          actually permits, and whether it is offered in cloud
--   connector_accounts     one per (tenant, user, connector): which scopes were
--                          granted, and the ENCRYPTED credential
--   connector_oauth_states one per in-flight authorisation: single-use,
--                          expiring, tenant-bound, PKCE verifier encrypted
--   action_receipts        what IRA actually did, produced by the gateway
--
-- ── The rule this migration exists to enforce ────────────────────────────────
--
-- From the architecture brief, quoted in core/interfaces/connector_gateway.py:
--
--     The model must never directly access OAuth credentials, database
--     credentials, email tokens, calendar tokens, user secrets, payment
--     credentials or cloud provider secrets.
--
-- A schema cannot stop application code from reading a column. What it CAN do
-- is make an unencrypted credential impossible to store, so that the worst case
-- is a leak of ciphertext rather than a leak of a refresh token:
--
--   1. THERE IS NO PLAINTEXT CREDENTIAL COLUMN. The only credential column is
--      `credential_ciphertext BYTEA`, and `credentials_are_encrypted` requires
--      that a non-null ciphertext is accompanied by a key id and a nonce. A row
--      that has a credential but no key it was encrypted under cannot exist.
--
--   2. VALUES THAT LOOK LIKE REAL TOKENS ARE REFUSED. Google refresh tokens
--      begin `1//`, its access tokens `ya29.`, GitHub's `gho_`/`ghp_`, and a
--      JWT `eyJ`. `credential_is_not_obvious_plaintext` rejects a ciphertext
--      whose first bytes match any of those. It is a smoke alarm, not a lock —
--      real ciphertext will not match by accident, and a developer who
--      accidentally assigns the raw token gets an immediate, specific error
--      instead of a working INSERT.
--
--   3. THE OAUTH VERIFIER IS TREATED THE SAME WAY. A PKCE code_verifier is a
--      credential for the duration of the flow, so it is stored encrypted under
--      the same constraint rather than as text.
--
-- ── Single-use OAuth state ───────────────────────────────────────────────────
--
-- `connector_oauth_states` is the CSRF defence, and the brief lists "invalid
-- OAuth state" among the conditions that must fail closed. The schema enforces
-- what it can: state is unique, carries an `expires_at`, and has a
-- `consumed_at` that a trigger refuses to overwrite. Replaying a callback
-- therefore fails at the database, not only in the handler that checked it.
--
-- ── Receipts are evidence, and evidence is redactable but not editable ───────
--
-- `action_receipts` follows the line ADR-0004 drew for `permission_decisions`:
-- ordinary UPDATE and every DELETE are refused, and exactly one transition is
-- permitted — a redaction that blanks the descriptive columns and stamps
-- `redacted_at`. Receipts describe what was read ("3 messages matching
-- 'invoice'"), which is personal data, so erasure must be able to reach it;
-- but a receipt that can be rewritten is not evidence.
--
-- Additive. Idempotent. Rollback: rollback/028_connectors_down.sql.
-- =============================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Scope catalogue — global reference data, no RLS.
--
-- The point of naming scopes here rather than in application code is that a
-- consent screen must be able to tell the user what a scope actually lets IRA
-- do, in a sentence they can read. `gmail.readonly` means nothing to anybody.
--
-- `offered_in_cloud` is the edition gate (ADR-0001): a scope that grants write
-- access to a user's mailbox is not offered to consumer cloud in this phase.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS connector_scopes (
    connector       TEXT NOT NULL
                        CHECK (connector IN ('gmail', 'google_calendar', 'google_drive')),
    scope           TEXT NOT NULL,

    -- What it permits, for the consent screen. Not optional: a scope nobody can
    -- explain is a scope nobody should be asked to grant.
    human_summary   TEXT NOT NULL CHECK (length(trim(human_summary)) > 0),
    -- 'read' or 'write'. Separate from the summary so the UI can group and warn
    -- without parsing English.
    access_kind     TEXT NOT NULL CHECK (access_kind IN ('read', 'write')),

    -- Whether this build offers it at all, and to which edition.
    offered_in_private BOOLEAN NOT NULL DEFAULT TRUE,
    offered_in_cloud   BOOLEAN NOT NULL DEFAULT FALSE,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (connector, scope)
);

-- Read-only scopes only, in this phase, for both editions. Phase 7 implements
-- reading and DRAFTING; it does not implement sending, and a scope that permits
-- something the build cannot do is a scope asked for under false pretences.
INSERT INTO connector_scopes
    (connector, scope, human_summary, access_kind, offered_in_private, offered_in_cloud)
VALUES
    ('gmail', 'https://www.googleapis.com/auth/gmail.readonly',
     'Read your email messages and search your mailbox. IRA cannot send, reply to, or delete anything with this.',
     'read', TRUE, TRUE),
    ('gmail', 'https://www.googleapis.com/auth/gmail.compose',
     'Prepare draft replies that are saved to your Drafts folder. Drafts are never sent — you send them yourself from Gmail.',
     'write', TRUE, FALSE),
    ('google_calendar', 'https://www.googleapis.com/auth/calendar.readonly',
     'See the events on your calendar, including titles, times and attendees.',
     'read', TRUE, TRUE),
    ('google_drive', 'https://www.googleapis.com/auth/drive.metadata.readonly',
     'See the names, types and dates of your files — not their contents.',
     'read', TRUE, TRUE),
    ('google_drive', 'https://www.googleapis.com/auth/drive.readonly',
     'Read the contents of files you select. IRA cannot change or delete them.',
     'read', TRUE, FALSE)
ON CONFLICT (connector, scope) DO UPDATE SET
    human_summary      = EXCLUDED.human_summary,
    access_kind        = EXCLUDED.access_kind,
    offered_in_private = EXCLUDED.offered_in_private,
    offered_in_cloud   = EXCLUDED.offered_in_cloud;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. The encryption constraint, defined once and applied to both tables that
--    hold a secret.
--
-- IMMUTABLE so it can sit in a CHECK. Takes the ciphertext and the two fields
-- that must accompany it.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION credential_looks_like_plaintext(blob BYTEA)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    -- A smoke alarm, not a lock. Real ciphertext will not begin with any of
    -- these by accident; a developer who assigned the raw token will.
    SELECT blob IS NOT NULL AND (
           position('1//'   in encode(substring(blob from 1 for 8), 'escape')) = 1
        OR position('ya29.' in encode(substring(blob from 1 for 8), 'escape')) = 1
        OR position('gho_'  in encode(substring(blob from 1 for 8), 'escape')) = 1
        OR position('ghp_'  in encode(substring(blob from 1 for 8), 'escape')) = 1
        OR position('eyJ'   in encode(substring(blob from 1 for 8), 'escape')) = 1
    );
$$;

COMMENT ON FUNCTION credential_looks_like_plaintext(BYTEA) IS
    'True when a credential blob begins with a well-known token prefix, which '
    'means it was almost certainly stored unencrypted. Used in CHECK '
    'constraints so an accidental plaintext write fails loudly at the INSERT '
    'rather than sitting in the table looking fine.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Connector accounts — the granted scopes and the encrypted credential.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS connector_accounts (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    connector           TEXT NOT NULL
                            CHECK (connector IN ('gmail', 'google_calendar',
                                                 'google_drive')),
    -- Which account at the provider, for display. An email address, so the user
    -- can tell two connected mailboxes apart.
    provider_account    TEXT NOT NULL DEFAULT '',

    -- Exactly what was granted. Compared against `connector_scopes` by the
    -- application so a scope the build does not offer cannot be recorded as
    -- granted.
    granted_scopes      TEXT[] NOT NULL DEFAULT '{}',

    -- The autonomy ladder from core/interfaces/connector_gateway.py. Default is
    -- the second rung, not the top: connecting an account grants IRA the
    -- ability to look, not the ability to act.
    access_level        TEXT NOT NULL DEFAULT 'observe'
                            CHECK (access_level IN ('never', 'observe', 'suggest',
                                                    'prepare',
                                                    'confirm_before_action',
                                                    'automatic_for_approved_routine')),

    status              TEXT NOT NULL DEFAULT 'connected'
                            CHECK (status IN ('connected', 'expired', 'revoked',
                                              'error')),

    -- ── The credential. Encrypted, or absent. ────────────────────────────────
    credential_ciphertext BYTEA,
    -- Which key it was encrypted under, so a key rotation can find the rows it
    -- has to re-wrap instead of guessing.
    encryption_key_id     TEXT,
    credential_nonce      BYTEA,

    connected_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at        TIMESTAMPTZ,
    expires_at          TIMESTAMPTZ,
    revoked_at          TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (tenant_id, user_id, connector, provider_account),

    -- A credential with no key it was encrypted under cannot exist.
    CONSTRAINT credentials_are_encrypted CHECK (
        credential_ciphertext IS NULL
        OR (encryption_key_id IS NOT NULL AND credential_nonce IS NOT NULL)
    ),
    CONSTRAINT credential_is_not_obvious_plaintext CHECK (
        NOT credential_looks_like_plaintext(credential_ciphertext)
    ),
    -- A revoked account must not still hold a credential. "Disconnect" that
    -- leaves the token behind is not a disconnect.
    CONSTRAINT revoked_accounts_hold_no_credential CHECK (
        revoked_at IS NULL OR credential_ciphertext IS NULL
    )
);

COMMENT ON COLUMN connector_accounts.credential_ciphertext IS
    'Encrypted refresh token. There is deliberately no plaintext column: the '
    'worst case for a leak of this table is ciphertext, not a usable token. '
    'See core/connectors/secrets.py, which is the only module that decrypts.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. In-flight OAuth authorisations. Single-use, expiring, tenant-bound.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS connector_oauth_states (
    -- The `state` parameter itself is the key. UNIQUE by being the primary key,
    -- so two flows cannot share one and a forged callback cannot collide.
    state               TEXT PRIMARY KEY CHECK (length(state) >= 32),

    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    connector           TEXT NOT NULL
                            CHECK (connector IN ('gmail', 'google_calendar',
                                                 'google_drive')),
    requested_scopes    TEXT[] NOT NULL DEFAULT '{}',

    -- The PKCE verifier is a credential for the life of the flow, so it gets
    -- the same treatment as a refresh token.
    verifier_ciphertext BYTEA NOT NULL,
    encryption_key_id   TEXT NOT NULL,
    verifier_nonce      BYTEA NOT NULL,
    -- Recorded so the callback can confirm the challenge it sent matches.
    code_challenge      TEXT NOT NULL,
    code_challenge_method TEXT NOT NULL DEFAULT 'S256'
                            CHECK (code_challenge_method = 'S256'),

    -- Where to return the user afterwards. Enumerated, NOT a free URL: an
    -- attacker-supplied redirect target is an open redirect, and this table is
    -- reachable from a callback.
    return_to           TEXT NOT NULL DEFAULT 'connectors'
                            CHECK (return_to IN ('connectors', 'onboarding',
                                                 'settings')),

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at          TIMESTAMPTZ NOT NULL,
    consumed_at         TIMESTAMPTZ,

    -- A flow that never expires is a CSRF token that never expires.
    CONSTRAINT oauth_state_expires_after_creation CHECK (expires_at > created_at),
    CONSTRAINT verifier_is_not_obvious_plaintext CHECK (
        NOT credential_looks_like_plaintext(verifier_ciphertext)
    )
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Action Receipts — what IRA actually did.
--
-- Produced by the gateway, never by the agent that asked: an actor describing
-- its own behaviour is not evidence (core/interfaces/connector_gateway.py).
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS action_receipts (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             UUID REFERENCES users(id) ON DELETE SET NULL,

    connector           TEXT NOT NULL,
    operation           TEXT NOT NULL,
    purpose             TEXT NOT NULL,
    performed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    outcome             TEXT NOT NULL DEFAULT 'ok'
                            CHECK (outcome IN ('ok', 'denied', 'error')),

    -- What was read or touched, in user-readable terms. SHAPE ONLY — "3
    -- message(s) matching a search" — never the payload. A connector response
    -- can carry message bodies and access tokens, and truncating a repr of one
    -- is not redaction (PERMISSION_MODEL.md §7).
    data_accessed       TEXT[] NOT NULL DEFAULT '{}',
    -- What left the machine, and to whom. Empty means nothing did.
    sent_externally     TEXT[] NOT NULL DEFAULT '{}',

    model_provider      TEXT NOT NULL DEFAULT '',
    approved_by_user    BOOLEAN NOT NULL DEFAULT FALSE,
    undo_available      BOOLEAN NOT NULL DEFAULT FALSE,

    -- Honest about its own audit trail: when the Trust Ledger is unreachable the
    -- action still ran, and the receipt says the entry is missing rather than
    -- claiming one that does not exist.
    ledger_recorded     BOOLEAN NOT NULL DEFAULT FALSE,
    audit_event_id      TEXT NOT NULL DEFAULT '',

    -- ── Taint, recorded for the user's benefit ───────────────────────────────
    -- Whether external content influenced this action, and which injection
    -- patterns were seen in it. Names of sources only, never their text.
    influenced_by_external BOOLEAN NOT NULL DEFAULT FALSE,
    influence_sources      TEXT[] NOT NULL DEFAULT '{}',
    injection_findings     TEXT[] NOT NULL DEFAULT '{}',

    redacted_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. Indexes. Every read filters on tenant_id first.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS connector_accounts_tenant_idx
    ON connector_accounts (tenant_id, user_id, connector);
CREATE INDEX IF NOT EXISTS connector_accounts_live_idx
    ON connector_accounts (tenant_id, status) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS oauth_states_expiry_idx
    ON connector_oauth_states (expires_at) WHERE consumed_at IS NULL;
CREATE INDEX IF NOT EXISTS action_receipts_recent_idx
    ON action_receipts (tenant_id, performed_at DESC);

-- ─────────────────────────────────────────────────────────────────────────────
-- 7. Row-Level Security.
--
-- connector_scopes is intentionally absent: global reference data with no
-- tenant column.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t TEXT;
    scoped TEXT[] := ARRAY[
        'connector_accounts',
        'connector_oauth_states',
        'action_receipts'
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
-- 8. An OAuth state may be consumed exactly once.
--
-- The handler checks this too. The trigger is what makes a replayed callback
-- fail even if the handler's check were wrong or raced — two callbacks arriving
-- together is the ordinary case for a double-clicked consent screen.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION oauth_state_single_use()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.consumed_at IS NOT NULL THEN
        RAISE EXCEPTION
            'OAuth state has already been consumed at %. A state parameter is '
            'single-use; re-presenting one is either a double-submitted consent '
            'screen or a replay, and both must fail closed.',
            OLD.consumed_at;
    END IF;
    -- The only field a consumption may change is consumed_at. Anything else
    -- would let a callback rewrite the tenant, user or scopes it was bound to.
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.user_id IS DISTINCT FROM OLD.user_id
       OR NEW.connector IS DISTINCT FROM OLD.connector
       OR NEW.requested_scopes IS DISTINCT FROM OLD.requested_scopes
       OR NEW.code_challenge IS DISTINCT FROM OLD.code_challenge THEN
        RAISE EXCEPTION
            'An OAuth state''s binding is immutable: tenant, user, connector, '
            'scopes and challenge cannot change between authorisation and '
            'callback. Only consumed_at may be set.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS oauth_states_single_use ON connector_oauth_states;
CREATE TRIGGER oauth_states_single_use
    BEFORE UPDATE ON connector_oauth_states
    FOR EACH ROW EXECUTE FUNCTION oauth_state_single_use();

-- ─────────────────────────────────────────────────────────────────────────────
-- 9. Receipts are evidence: redactable, never editable.
--
-- The same narrow relaxation ADR-0004 defined for permission_decisions. One
-- permitted transition — blank the descriptive columns and stamp redacted_at.
-- Everything else, and every DELETE, is refused.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION action_receipts_append_only()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'action_receipts is append-only: DELETE is not permitted. A receipt '
            'is the evidence of what IRA did with a user''s data. Use the '
            'redaction path, which blanks the content and keeps the fact.';
    END IF;

    IF OLD.redacted_at IS NOT NULL THEN
        RAISE EXCEPTION 'This receipt has already been redacted at %.',
            OLD.redacted_at;
    END IF;

    IF NEW.redacted_at IS NULL THEN
        RAISE EXCEPTION
            'action_receipts is append-only: the only permitted UPDATE is a '
            'redaction, which must stamp redacted_at.';
    END IF;

    -- A redaction must actually remove the personal data. Stamping the column
    -- while leaving the content would produce a row that claims to be redacted
    -- and is not — the worst possible outcome for an erasure report.
    IF NEW.purpose <> '' OR NEW.data_accessed <> '{}'
       OR NEW.sent_externally <> '{}' OR NEW.influence_sources <> '{}' THEN
        RAISE EXCEPTION
            'A receipt redaction must clear purpose, data_accessed, '
            'sent_externally and influence_sources — those are where the '
            'personal data is.';
    END IF;

    -- What survives is deliberate: that an action of this kind happened, when,
    -- through which connector, and whether the user approved it.
    IF NEW.connector IS DISTINCT FROM OLD.connector
       OR NEW.operation IS DISTINCT FROM OLD.operation
       OR NEW.performed_at IS DISTINCT FROM OLD.performed_at THEN
        RAISE EXCEPTION
            'A receipt redaction may not change what happened or when.';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS action_receipts_no_rewrite ON action_receipts;
CREATE TRIGGER action_receipts_no_rewrite
    BEFORE UPDATE OR DELETE ON action_receipts
    FOR EACH ROW EXECUTE FUNCTION action_receipts_append_only();

COMMIT;
