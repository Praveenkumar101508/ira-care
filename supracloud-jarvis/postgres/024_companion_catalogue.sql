-- =============================================================================
-- 024_companion_catalogue.sql — Phase 3: Companion Creator and Avatar Catalogue
--
-- Generalises IRA's single fixed avatar and single fixed voice into curated
-- CATALOGUES, and gives each user a companion profile that selects from them.
--
-- Two kinds of table live here and they have deliberately different rules:
--
--   1. CATALOGUE tables (avatar_catalogue, voice_catalogue, personality_presets)
--      are OPERATOR-CURATED GLOBAL REFERENCE DATA. They contain no customer
--      data — only which avatars/voices/personalities this deployment offers
--      and under what licence. They are therefore NOT tenant-scoped and carry
--      no RLS policy: every tenant reads the same catalogue, and only a
--      migration or an operator writes it. Making them tenant-scoped would mean
--      copying the whole catalogue per tenant, which is how a licence revocation
--      ends up applied to some tenants and not others.
--
--   2. COMPANION tables (companions, companion_preferences, companion_versions)
--      are CUSTOMER DATA and are tenant-scoped with RLS, exactly like Phase 2's
--      tables. A companion profile is a person's chosen identity for their
--      assistant; it must not be visible across tenants.
--
-- ── On `companions` vs the brief's `companion_profiles` ──────────────────────
-- The transformation brief names the profile entity `companion_profiles`.
-- 023_tenancy.sql already created `companions` for exactly that purpose, with
-- RLS already enabled and forced on it. This migration EXTENDS that table rather
-- than adding a second one — the same call Phase 2 made when it promoted the
-- pre-existing `tenants` table instead of introducing a rival. Two tables both
-- meaning "the user's companion" is how a query ends up reading the stale one.
-- `companions` IS the brief's `companion_profiles`; the mapping is recorded in
-- docs/cloud-transformation/DATA_MODEL.md and asserted by
-- tests/companion/test_companion_migration.py.
--
-- ── Fail closed on licensing ─────────────────────────────────────────────────
-- An avatar row is only offered to users when its licence metadata is complete
-- AND verified AND permits the use. The CHECK constraints below make some of
-- that structural; the rest is enforced in ira/core/companion/licence.py, which
-- denies on anything it cannot positively confirm. No row seeded by this
-- migration claims a licence it does not have: the unlicensed slots are seeded
-- INACTIVE with NULL licence fields, which is the honest representation of
-- "no model installed yet".
--
-- Migration safety: additive only. No column is dropped, no row deleted, no
-- existing behaviour changed. Idempotent — safe to re-run.
-- Rollback: rollback/024_companion_catalogue_down.sql.
-- =============================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Avatar catalogue — global, operator-curated reference data
--
-- `rig_type` is constrained to the three rigs IRA's renderer can actually drive
-- (the same list ira-avatar-manifest.json already declares as its preference
-- order). An avatar with a rig the lip-sync pipeline cannot address would load
-- and then simply never move its mouth, which is worse than refusing it.
--
-- `edition_availability` separates two genuinely different permissions:
--   * 'private' — the owner may use this asset on their own machine. Their own
--     portrait qualifies; it is theirs.
--   * 'cloud'   — the asset is REDISTRIBUTED to unrelated users of a hosted
--     service. That needs a verified commercial-use licence, which is a much
--     higher bar and is why the owner's personal likeness must never silently
--     become a cloud catalogue entry.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS avatar_catalogue (
    id                   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    slug                 TEXT NOT NULL UNIQUE,
    display_name         TEXT NOT NULL,
    thumbnail_url        TEXT,
    desktop_model_url    TEXT,
    mobile_model_url     TEXT,
    rig_type             TEXT
                             CHECK (rig_type IS NULL OR rig_type IN ('ARKit52', 'OculusVisemes', 'VRM')),
    style                TEXT NOT NULL DEFAULT 'stylised'
                             CHECK (style IN ('stylised', 'illustrated', 'abstract', 'photoreal')),
    -- Which renderer drives this entry. 'procedural' entries are generated from
    -- IRA's own code (the orb / cinematic face) and have no third-party asset,
    -- so they are licence-free by construction and always safe to offer.
    renderer             TEXT NOT NULL DEFAULT 'glb'
                             CHECK (renderer IN ('glb', 'portrait', 'procedural')),
    edition_availability TEXT NOT NULL DEFAULT 'private'
                             CHECK (edition_availability IN ('private', 'cloud', 'both')),

    -- Licence record. `licence_reference` points at the provenance file that
    -- holds the full record; the columns here are the fields an authorisation
    -- decision needs without opening it.
    licence_reference    TEXT,
    licence_name         TEXT,
    licence_verified     BOOLEAN NOT NULL DEFAULT FALSE,
    commercial_use       BOOLEAN,
    redistribution       BOOLEAN,
    modification         BOOLEAN,
    attribution          TEXT,

    -- Validation report from scripts/validate-avatar.mjs. NULL means "never
    -- validated", which is NOT the same as "validated and failed" — the licence
    -- gate refuses both, but the distinction matters when diagnosing.
    validation_status    TEXT NOT NULL DEFAULT 'not-installed'
                             CHECK (validation_status IN ('not-installed', 'pending', 'passed', 'failed')),
    validation_report    JSONB,
    supports_blink       BOOLEAN NOT NULL DEFAULT FALSE,
    supports_speech      BOOLEAN NOT NULL DEFAULT FALSE,
    triangles            INTEGER,
    texture_max_px       INTEGER,
    external_textures    BOOLEAN NOT NULL DEFAULT FALSE,

    -- Fallback slug used when this entry cannot load on the client (missing
    -- model, weak device, refused licence). NOT a foreign key: the fallback
    -- must survive its target being deactivated, and resolution is done in
    -- application code which already denies on an unresolvable chain.
    fallback_slug        TEXT,

    active               BOOLEAN NOT NULL DEFAULT FALSE,
    version              INTEGER NOT NULL DEFAULT 1,
    sort_order           INTEGER NOT NULL DEFAULT 100,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Voice catalogue — global, operator-curated reference data
--
-- `provider_voice_id` is the provider's own identifier (e.g. Kokoro's
-- "af_bella"). It is NOT a credential and is safe to send to a client. Provider
-- CREDENTIALS are never stored in this table and never leave the backend —
-- synthesis and preview are proxied through IRA's own API.
--
-- `privacy_classification` is the field a user is actually entitled to see
-- before picking a voice: does speaking with it send my words to someone else?
--   * 'on_device'  — synthesised in the browser/client. Nothing leaves.
--   * 'local'      — synthesised by this IRA deployment. Stays on the server.
--   * 'external'   — sent to a third-party provider.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS voice_catalogue (
    id                     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    slug                   TEXT NOT NULL UNIQUE,
    provider               TEXT NOT NULL,
    provider_voice_id      TEXT NOT NULL,
    display_name           TEXT NOT NULL,
    language               TEXT NOT NULL DEFAULT 'en',
    accent                 TEXT NOT NULL DEFAULT '',
    gender_presentation    TEXT NOT NULL DEFAULT 'unspecified'
                               CHECK (gender_presentation IN ('feminine', 'masculine', 'neutral', 'unspecified')),
    style                  TEXT NOT NULL DEFAULT 'warm',
    preview_url            TEXT,
    privacy_classification TEXT NOT NULL DEFAULT 'local'
                               CHECK (privacy_classification IN ('on_device', 'local', 'external')),
    edition_availability   TEXT NOT NULL DEFAULT 'private'
                               CHECK (edition_availability IN ('private', 'cloud', 'both')),
    -- Whether this voice needs a runtime component that may not be installed
    -- (a model download, a sidecar process). Offered but unavailable is a worse
    -- experience than not offered, so the API filters on it.
    requires_runtime       TEXT,
    active                 BOOLEAN NOT NULL DEFAULT FALSE,
    sort_order             INTEGER NOT NULL DEFAULT 100,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Personality presets — global, operator-curated reference data
--
-- A preset is a bundle of STYLE parameters, not permissions. It cannot widen
-- what IRA is allowed to do: autonomy and permissions live on the companion
-- profile and (from Phase 4) in the Constitution, so choosing a "proactive"
-- personality never grants the right to act without approval.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS personality_presets (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    slug           TEXT NOT NULL UNIQUE,
    display_name   TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    -- Style dials, 0-100. Deliberately a small fixed set rather than free-form
    -- prompt text: a preset that carried arbitrary instructions would be a
    -- prompt-injection surface curated by whoever can write this table.
    warmth         SMALLINT NOT NULL DEFAULT 50 CHECK (warmth        BETWEEN 0 AND 100),
    formality      SMALLINT NOT NULL DEFAULT 50 CHECK (formality     BETWEEN 0 AND 100),
    verbosity      SMALLINT NOT NULL DEFAULT 50 CHECK (verbosity     BETWEEN 0 AND 100),
    proactivity    SMALLINT NOT NULL DEFAULT 50 CHECK (proactivity   BETWEEN 0 AND 100),
    humour         SMALLINT NOT NULL DEFAULT 50 CHECK (humour        BETWEEN 0 AND 100),
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order     INTEGER NOT NULL DEFAULT 100,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Companion profile — EXTENDS 023's `companions` (see header)
--
-- Every column is added nullable-or-defaulted so an existing row keeps working.
--
-- ── Why no UUID foreign keys to the catalogues ───────────────────────────────
-- 023 already stores the selection as slugs (`avatar_slug`, `voice_id`,
-- `personality`). Adding parallel UUID FK columns would create two sources of
-- truth for the same choice, and the first query to read the stale one is a bug
-- nobody would see in review.
--
-- A slug FK was considered and rejected too. Referential integrity is not the
-- question the code actually asks: "may this user be offered this avatar?"
-- depends on `active`, `edition_availability`, `licence_verified`,
-- `commercial_use` and the validation report together. A FK that passes while
-- the licence gate refuses would be a green light attached to the wrong
-- question. Selection is therefore validated in ira/core/companion/, which
-- denies on anything it cannot positively confirm, and the slug columns stay the
-- single record of what was chosen — including when a catalogue entry is later
-- withdrawn, which a FK with ON DELETE SET NULL would silently erase.
-- ─────────────────────────────────────────────────────────────────────────────

-- 'draft' until the user finishes the creator. A half-finished companion must
-- not be treated as an active identity — the wizard can be abandoned.
ALTER TABLE companions ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'active', 'retired'));
ALTER TABLE companions ADD COLUMN IF NOT EXISTS activated_at TIMESTAMPTZ;
ALTER TABLE companions ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

-- 023 defaulted autonomy_level to 'confirm_before_action' with no CHECK. Add
-- the constraint now that Phase 3 has a defined ladder. NOT VALID first would
-- let an existing bad row survive silently; there is no such row (the column has
-- a valid default and nothing writes it yet), so validate immediately.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'companions_autonomy_level_check'
    ) THEN
        ALTER TABLE companions ADD CONSTRAINT companions_autonomy_level_check
            CHECK (autonomy_level IN (
                'never', 'observe', 'suggest', 'prepare',
                'confirm_before_action', 'automatic_for_approved_routine'
            ));
    END IF;
END
$$;

-- One ACTIVE companion per user. A partial unique index rather than a plain
-- one, so a user may keep retired versions and an in-progress draft alongside
-- the companion they are actually talking to.
CREATE UNIQUE INDEX IF NOT EXISTS companions_one_active_per_user
    ON companions (tenant_id, user_id) WHERE status = 'active';

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Companion preferences — tenant-scoped customer data
--
-- Split from the profile because these change often and independently (a user
-- adjusting speech rate is not editing their companion's identity), and because
-- Phase 5's Privacy Centre needs to expose exactly these without exposing the
-- profile row.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS companion_preferences (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id)    ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id)      ON DELETE CASCADE,
    companion_id    UUID NOT NULL REFERENCES companions(id) ON DELETE CASCADE,
    speech_rate     NUMERIC(3,2) NOT NULL DEFAULT 1.00 CHECK (speech_rate BETWEEN 0.50 AND 2.00),
    subtitles       BOOLEAN NOT NULL DEFAULT TRUE,
    -- Avatar motion off is an accessibility setting (reduced motion) as well as
    -- a performance one, so it is a stored preference and not only a media query.
    reduced_motion  BOOLEAN NOT NULL DEFAULT FALSE,
    avatar_quality  TEXT NOT NULL DEFAULT 'auto'
                        CHECK (avatar_quality IN ('auto', 'high', 'low', 'off')),
    greeting        TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (companion_id)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. Companion versions — tenant-scoped customer data
--
-- An append-only history of what the companion looked like and when. Two
-- reasons it exists: a user can undo a change they regret, and Phase 5's
-- Privacy Centre must be able to answer "when did my assistant's settings
-- change, and to what?" without that answer depending on the current row.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS companion_versions (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id)    ON DELETE CASCADE,
    user_id       UUID NOT NULL REFERENCES users(id)      ON DELETE CASCADE,
    companion_id  UUID NOT NULL REFERENCES companions(id) ON DELETE CASCADE,
    version       INTEGER NOT NULL,
    snapshot      JSONB NOT NULL,
    changed_by    TEXT NOT NULL DEFAULT 'user',
    reason        TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (companion_id, version)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 7. Indexes
-- ─────────────────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS avatar_catalogue_active_idx  ON avatar_catalogue (active, sort_order);
CREATE INDEX IF NOT EXISTS voice_catalogue_active_idx   ON voice_catalogue (active, sort_order);
CREATE INDEX IF NOT EXISTS personality_presets_active_idx ON personality_presets (active, sort_order);
CREATE INDEX IF NOT EXISTS companion_prefs_tenant_idx   ON companion_preferences (tenant_id);
CREATE INDEX IF NOT EXISTS companion_versions_tenant_idx ON companion_versions (tenant_id);
CREATE INDEX IF NOT EXISTS companion_versions_lookup_idx ON companion_versions (companion_id, version DESC);

-- ─────────────────────────────────────────────────────────────────────────────
-- 8. Row-Level Security on the new CUSTOMER-DATA tables
--
-- Same uniform policy as 023: a row is visible only when its tenant_id equals
-- the `app.tenant_id` GUC, an unset context matches nothing, NULLIF keeps a
-- CLEARED context denying cleanly rather than raising a uuid cast error, and
-- WITH CHECK blocks cross-tenant writes as well as reads. FORCE makes it apply
-- to the table owner too.
--
-- The catalogue tables are NOT in this list, on purpose — see the file header.
-- tests/companion/test_companion_catalogue_rls.py asserts BOTH halves of that
-- decision: the customer tables have policies, and the catalogue tables are
-- readable without a tenant context (because they contain nobody's data) while
-- still being write-protected by role grants rather than by RLS.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t TEXT;
    scoped TEXT[] := ARRAY[
        'companion_preferences', 'companion_versions'
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
-- 9. Seed data
--
-- What is seeded here is only what this repository can honestly claim.
--
-- PERSONALITIES and VOICES are seeded active: they are IRA's own presets and
-- IRA's own already-installed local TTS engines respectively. Nothing about
-- them is a third-party redistribution claim.
--
-- AVATARS are the careful case. Exactly one entry is seeded active-and-cloud:
-- 'ira-signature', which is drawn procedurally by IRA's own renderer and
-- contains no third-party asset at all. Every entry that would need a licensed
-- GLB is seeded INACTIVE with NULL licence fields and validation_status
-- 'not-installed' — the truthful state of "an operator has not installed this
-- yet". No row here asserts a licence that has not been verified.
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO personality_presets (slug, display_name, description, warmth, formality, verbosity, proactivity, humour, sort_order)
VALUES
    ('supportive',  'Supportive',  'Warm and encouraging. Checks in, softens bad news, offers the next step.', 85, 35, 55, 45, 45, 10),
    ('professional','Professional','Concise and precise. Minimal small talk, answers first.',                  40, 80, 35, 35, 15, 20),
    ('creative',    'Creative',    'Playful and associative. Offers alternatives and unexpected angles.',       70, 30, 65, 60, 70, 30),
    ('analytical',  'Analytical',  'Structured and evidence-led. States assumptions and confidence.',           35, 65, 60, 30, 15, 40)
ON CONFLICT (slug) DO NOTHING;

-- Voices. Provider ids are the engines' own names (see ira/voice/tts_factory.py).
-- All three engines run locally, hence privacy_classification 'local'. Only
-- Kokoro is seeded active: it is the default engine and needs no extra install.
-- Supertonic and OmniVoice are real, present in the codebase, and OFF until an
-- operator installs their runtime — `requires_runtime` records what is missing.
INSERT INTO voice_catalogue
    (slug, provider, provider_voice_id, display_name, language, accent, gender_presentation, style,
     privacy_classification, edition_availability, requires_runtime, active, sort_order)
VALUES
    ('kokoro-bella',   'kokoro',     'af_bella', 'Bella — warm',        'en', 'light Indian warmth', 'feminine', 'warm',        'local', 'both',    NULL,                     TRUE,  10),
    ('kokoro-heart',   'kokoro',     'af_heart', 'Heart — bright',      'en', 'neutral',             'feminine', 'bright',      'local', 'both',    NULL,                     TRUE,  20),
    ('supertonic-f1',  'supertonic', 'F1',       'Supertonic F1',       'en', 'neutral',             'feminine', 'neutral',     'local', 'private', 'supertonic model files', FALSE, 30),
    ('supertonic-m1',  'supertonic', 'M1',       'Supertonic M1',       'en', 'neutral',             'masculine','neutral',     'local', 'private', 'supertonic model files', FALSE, 40),
    ('omnivoice-design','omnivoice', 'design',   'OmniVoice (designed)','en', 'configurable',        'unspecified','configurable','local','private','omnivoice sidecar venv', FALSE, 50)
ON CONFLICT (slug) DO NOTHING;

-- Avatars.
INSERT INTO avatar_catalogue
    (slug, display_name, thumbnail_url, desktop_model_url, mobile_model_url, rig_type, style, renderer,
     edition_availability, licence_reference, licence_name, licence_verified, commercial_use, redistribution,
     modification, attribution, validation_status, supports_blink, supports_speech, external_textures,
     fallback_slug, active, sort_order)
VALUES
    -- Licence-free by construction: no asset file exists to license. This is the
    -- entry that makes "a cloud user can preview a valid avatar" true today
    -- without anybody fabricating a licence for a model nobody has bought.
    ('ira-signature', 'IRA Signature', NULL, NULL, NULL, NULL, 'abstract', 'procedural',
     'both', 'in-repo: rendered by IRA''s own components', 'IRA original — no third-party asset', TRUE, TRUE, TRUE,
     TRUE, 'IRA', 'passed', TRUE, TRUE, FALSE,
     NULL, TRUE, 10),

    -- The owner's approved identity portrait. PRIVATE ONLY and deliberately so:
    -- ira-avatar-license.json records licenseVerified=false, and it is a
    -- likeness of a real person. It may not be redistributed to cloud users.
    ('ira-portrait', 'IRA Portrait', '/avatar/ira-reference.png', NULL, NULL, NULL, 'photoreal', 'portrait',
     'private', '/avatar/ira-avatar-license.json', 'owner-declared', FALSE, NULL, NULL,
     NULL, 'IRA — Praveen Kamineti', 'passed', FALSE, FALSE, FALSE,
     'ira-signature', TRUE, 20),

    -- The rigged digital-human slot. No GLB is installed and none is committed.
    ('ira-digital-human', 'IRA Digital Human', NULL, '/avatar/ira-head.glb', '/avatar/ira-head-mobile.glb', 'ARKit52', 'photoreal', 'glb',
     'private', '/avatar/ira-avatar-license.json', NULL, FALSE, NULL, NULL,
     NULL, NULL, 'not-installed', FALSE, FALSE, FALSE,
     'ira-portrait', FALSE, 30)
ON CONFLICT (slug) DO NOTHING;

COMMIT;
