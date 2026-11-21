-- =============================================================================
-- 067_model_registry.sql — Phase 5, batch P5-01. Closes MOD-03, enables MOD-02.
--
-- `BASELINE_MODEL_INVENTORY.md` §6 lists what is absent, and names the columns:
--
--   Model registry (`base_model`, `adapter_version`, `artifact_hash`,
--   `commercial_licence`, `rollout_percentage`, `rollback_target`, …) — absent
--
-- and §8:
--
--   FINDING MOD-02 — model artefacts are neither hashed nor pinned.
--   No allowlist prevents an arbitrary `ollama pull`.
--   No separation between development, staging and production model registries.
--
-- Two tables, because they answer two different questions:
--
--   `model_registry`        which model versions may SERVE, and under what terms
--   `model_pull_allowlist`  which artefacts may be FETCHED at all
--
-- Keeping them apart matters. An artefact that may be downloaded is not thereby
-- a model that may answer a paying tenant's request — the second needs a licence
-- position and a rollout decision, the first needs only a pinned revision. One
-- table would have forced every fetch to carry a licence it does not have yet.
--
-- ── Classification: REFERENCE_DATA, no RLS ─────────────────────────────────
--
-- Same class as `model_providers` (029): platform-authored operator
-- configuration, no tenant column, no customer data. Protected by role grants
-- rather than by RLS, and `029` already records the reasoning — "global
-- reference data with no tenant".
--
-- This is a deliberate decision rather than an omission, so it is worth being
-- explicit about the alternative: making the registry tenant-scoped would let a
-- tenant register its own model. That is a Phase 6+ capability with a licence
-- and a supply-chain question attached to it, and building the column now would
-- create an authority surface nothing is ready to defend.
--
-- ── The runtime role cannot write here ─────────────────────────────────────
--
-- `ira_app` gets SELECT and nothing else. Registration is an operator act
-- performed by `ira_migrator`, for the reason MOD-02 exists: if the process
-- serving requests can also insert an allowlist entry, then a request-handling
-- path that is talked into writing one has defeated the allowlist. The gateway
-- reads; it never grants itself permission.
-- =============================================================================

BEGIN;

-- ── Which model versions may serve ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS model_registry (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- The stable name a caller uses. `model_key` + `version` is the identity;
    -- callers name the key and the registry decides the version, which is what
    -- makes a rollback possible without touching call sites.
    model_key           TEXT NOT NULL,
    version             INTEGER NOT NULL CHECK (version > 0),

    -- Upstream identity. `base_model` is what was pulled; `revision` pins it.
    base_model          TEXT NOT NULL,
    revision            TEXT NOT NULL,

    -- Phase 7 territory, present and NULL. Recording the column now costs
    -- nothing and means a fine-tuned model does not need a schema change to be
    -- registrable — but nothing populates it, and MOD-04 says so plainly.
    adapter_version     TEXT,

    -- MOD-02. NOT NULL: a registry entry without a hash cannot answer "is the
    -- artefact on disk the artefact we approved", which is the entire point.
    artifact_hash       TEXT NOT NULL,

    -- MOD-01, which is PHASE 6 and blocking there. NULL means "unrecorded", not
    -- "permissive" — `serving_is_licensed` below refuses to let an unlicensed
    -- row serve. Engineering must not populate this: what a model's commercial
    -- terms are is a legal determination, not a code change.
    commercial_licence  TEXT,
    licence_recorded_at TIMESTAMPTZ,
    licence_source      TEXT,

    -- MOD-02's "no separation between development, staging and production model
    -- registries". A row is scoped to one environment; promoting a model is
    -- registering it in the next one, deliberately.
    environment         TEXT NOT NULL
                        CHECK (environment IN ('development', 'staging', 'production')),

    status              TEXT NOT NULL DEFAULT 'registered'
                        CHECK (status IN ('registered', 'serving', 'rolled_back', 'retired')),

    -- MOD-03's rollout and rollback.
    rollout_percentage  INTEGER NOT NULL DEFAULT 0
                        CHECK (rollout_percentage BETWEEN 0 AND 100),
    rollback_target     UUID REFERENCES model_registry(id) ON DELETE RESTRICT,

    registered_by       TEXT NOT NULL DEFAULT 'migration',
    notes               TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- One row per (key, version, environment). The same version may exist in
    -- development and production with different rollout percentages, which is
    -- what "separate registries" means in one table.
    CONSTRAINT model_registry_identity UNIQUE (model_key, version, environment),

    -- A serving model must have a recorded licence. This is the constraint that
    -- makes MOD-01 a *gate* rather than a note: Phase 6 can populate the field
    -- and flip status, and until then nothing can be set serving by accident.
    CONSTRAINT serving_is_licensed CHECK (
        status <> 'serving'
        OR (commercial_licence IS NOT NULL AND licence_recorded_at IS NOT NULL)
    ),

    -- A serving version must be rolled out to somebody.
    --
    -- `rollout_percentage` defaults to 0, so an operator who sets status =
    -- 'serving' and nothing else creates a model that is serving to NOBODY —
    -- and the symptom is not an error. Requests simply stop being answered by
    -- that model, silently, and the registry looks correct.
    --
    -- Found by writing the rollback test: restoring a previous version with
    -- `status = 'serving'` left every tenant with nothing, because the row
    -- still carried the 0 it was registered with. Making the state
    -- unrepresentable is better than documenting the trap.
    CONSTRAINT serving_reaches_somebody CHECK (
        status <> 'serving' OR rollout_percentage > 0
    ),

    -- A rolled-back version must say what replaced it. "This model is no longer
    -- serving" without a target is an outage described as a status.
    CONSTRAINT rolled_back_names_its_target CHECK (
        status <> 'rolled_back' OR rollback_target IS NOT NULL
    ),

    -- A hash has to look like one. Not cryptography — arithmetic: a truncated
    -- or placeholder hash silently verifies nothing, and 'sha256:' + 64 hex is
    -- cheap to require at the boundary.
    CONSTRAINT artifact_hash_is_a_sha256 CHECK (
        artifact_hash ~ '^sha256:[0-9a-f]{64}$'
    ),

    -- A pinned revision is the point of MOD-02. 'latest', 'main' and an empty
    -- string are the three ways a pin stops pinning.
    CONSTRAINT revision_is_actually_pinned CHECK (
        revision <> '' AND lower(revision) NOT IN ('latest', 'main', 'master', 'head')
    )
);

COMMENT ON TABLE model_registry IS
    'MOD-03. Which model versions may serve, in which environment, under what '
    'licence, at what rollout, with what rollback target. Platform-authored: '
    'ira_app may read it and may not write it.';

COMMENT ON COLUMN model_registry.commercial_licence IS
    'MOD-01, Phase 6. NULL means UNRECORDED, never permissive — the '
    'serving_is_licensed constraint refuses to let an unlicensed row serve. '
    'Populating this is a legal determination and is not an engineering task.';

COMMENT ON COLUMN model_registry.adapter_version IS
    'Phase 7 (MOD-04). Present so a fine-tuned model needs no schema change; '
    'nothing populates it today and no IRA-specific model exists.';

CREATE INDEX IF NOT EXISTS model_registry_serving_idx
    ON model_registry (environment, model_key)
    WHERE status = 'serving';

CREATE INDEX IF NOT EXISTS model_registry_key_idx
    ON model_registry (model_key, environment, version DESC);

-- Exactly one serving version per (key, environment). Two would make "which
-- model answered this request" unanswerable, and the rollout percentage
-- meaningless.
CREATE UNIQUE INDEX IF NOT EXISTS model_registry_one_serving_version
    ON model_registry (model_key, environment)
    WHERE status = 'serving';


-- ── Which artefacts may be fetched at all ───────────────────────────────────

CREATE TABLE IF NOT EXISTS model_pull_allowlist (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Where it would come from. Constrained rather than free text: a source
    -- nobody wrote a fetcher for cannot be allowlisted into existence.
    source        TEXT NOT NULL CHECK (source IN ('huggingface', 'ollama')),

    -- The upstream identifier, e.g. 'ai4bharat/indic-parler-tts'.
    identifier    TEXT NOT NULL,

    -- Pinned. Same rule as the registry, same reason.
    revision      TEXT NOT NULL,

    -- Recorded once the artefact has actually been fetched and hashed. NULL
    -- means "allowed to fetch, not yet verified" — a real state, because the
    -- hash cannot be known before the first pull. What must never happen is a
    -- LOAD proceeding against a NULL, and that is the loader's job to refuse.
    artifact_hash TEXT
                  CHECK (artifact_hash IS NULL OR artifact_hash ~ '^sha256:[0-9a-f]{64}$'),

    purpose       TEXT NOT NULL DEFAULT '',
    added_by      TEXT NOT NULL DEFAULT 'migration',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT model_pull_allowlist_identity UNIQUE (source, identifier, revision),
    CONSTRAINT allowlist_revision_is_pinned CHECK (
        revision <> '' AND lower(revision) NOT IN ('latest', 'main', 'master', 'head')
    )
);

COMMENT ON TABLE model_pull_allowlist IS
    'MOD-02. The only artefacts IRA may fetch, each at a pinned revision. '
    'Separate from model_registry because permission to download is not '
    'permission to serve.';

CREATE INDEX IF NOT EXISTS model_pull_allowlist_lookup_idx
    ON model_pull_allowlist (source, identifier);


-- ── The artefacts IRA currently fetches ────────────────────────────────────
--
-- Seeded here rather than left to an operator, because an empty allowlist and a
-- correct allowlist are indistinguishable from the outside: both refuse
-- everything, and the first one refuses the three artefacts the product needs.
-- Recording them is what makes the refusal of anything ELSE meaningful.
--
-- Each revision was RESOLVED from the Hub's model API and recorded, not
-- guessed — a plausible forty-hex string would satisfy every "is it pinned"
-- check while pinning nothing. They match `core/models/artefacts.py`, and
-- `check_model_artefacts` fails readiness in a production-like environment if
-- they ever drift apart.
--
-- `artifact_hash` stays NULL: the hash of the downloaded artefact is not known
-- until something downloads it, and inventing one would be worse than leaving
-- it absent. A NULL here says "allowed to fetch, not yet verified", which is
-- true.
--
-- ON CONFLICT DO NOTHING because the rollback cycle re-applies this file.
INSERT INTO model_pull_allowlist (source, identifier, revision, purpose) VALUES
    ('huggingface', 'ai4bharat/indic-parler-tts',
     '7b527af5ee8ed1f9a28d80b19703ed9bb8ba10ca', 'Indic TTS for ta/te/kn/ml'),
    ('huggingface', 'k2-fsa/OmniVoice',
     'c5fdb5ccb189668d56333f77ba2629f4cd7535f4', 'OmniVoice sidecar'),
    ('huggingface', 'pyannote/speaker-diarization-3.1',
     '84fd25912480287da0247647c3d2b4853cb3ee5d', 'speaker diarisation for meetings')
ON CONFLICT (source, identifier, revision) DO NOTHING;


-- ── Grants — and the REVOKE that makes them mean something ─────────────────
--
-- The first version of this migration wrote only the GRANTs below and believed
-- it had made the tables read-only. It had not, and the test that caught it is
-- worth recording because the mistake is invisible in the SQL:
--
--   `042` sets ALTER DEFAULT PRIVILEGES FOR ROLE ira_schema_owner GRANT
--   SELECT, INSERT, UPDATE, DELETE ON TABLES TO ira_app, ira_worker.
--
-- So every new table owned by `ira_schema_owner` arrives with full DML for both
-- runtime roles already attached. `GRANT SELECT` on top of that grants nothing
-- new and revokes nothing — the statement reads like a restriction and is a
-- no-op. `ira_app` could INSERT into its own allowlist.
--
-- Default privileges are the right default for the 90-odd tenant-scoped tables
-- this schema is mostly made of. These two are the exception, so they say so
-- explicitly. A table that must be read-only has to REVOKE, because a database
-- that grants by default cannot be made restrictive by granting less.
GRANT SELECT ON model_registry       TO ira_app, ira_worker;
GRANT SELECT ON model_pull_allowlist TO ira_app, ira_worker;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON model_registry       FROM ira_app, ira_worker;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON model_pull_allowlist FROM ira_app, ira_worker;

COMMIT;
