-- =============================================================================
-- 073_artefact_manifests.sql — Phase 7, batch P7-06.
--
-- `PHASE_5_CLOSURE_REPORT.md` §7 decision 3 left one thing open and assigned it
-- here:
--
--     Recording `artifact_hash` for allowlisted artefacts needs a
--     fetch-and-hash step. Phase 6 obtained Ollama artefact digests without
--     downloading, which may make part of this cheaper than assumed.
--
-- It is cheaper. It is also **not the same thing**, and that distinction is the
-- whole of this migration.
--
-- ── What `artifact_hash` means, and why it stays NULL ──────────────────────
--
-- `067` was explicit:
--
--     Recorded once the artefact has actually been fetched and hashed. NULL
--     means "allowed to fetch, not yet verified" — a real state, because the
--     hash cannot be known before the first pull.
--
-- A digest obtained from the Hub's API is not that. It is **what the publisher
-- says the bytes are**, retrieved over the network, and writing it into
-- `artifact_hash` would turn a column that means *"we downloaded this and
-- checked it"* into one that means *"they told us"* — while every existing
-- reader carried on believing the first thing.
--
-- That is Phase 6's finding, exactly: `commercial_licence` read like a control
-- and held whatever somebody typed. The remedy there was to separate EVIDENCE
-- from DETERMINATION. The remedy here is the same shape, one layer along:
-- separate **what the publisher publishes** from **what we verified**.
--
-- ── So this adds evidence columns, and refuses to let them impersonate ─────
--
-- `manifest_sha256` is a digest over the repository listing the Hub serves at
-- that exact revision — every file's path, size and content id, sorted. It
-- pins the SHAPE of the artefact: a file added, removed, renamed or changed
-- moves it. What it cannot tell you is whether the bytes on your disk match,
-- because nothing here has any bytes.
--
-- `a_published_manifest_is_not_a_verified_download` refuses to let the two be
-- equal. Copying the evidence digest into the verification column is the single
-- most likely way this control gets defeated — it looks like filling in a
-- missing field, it makes a NULL go away, and afterwards nothing anywhere
-- disagrees.
--
-- ── Why the derivation is recorded and constrained ────────────────────────
--
-- A digest whose derivation is unrecorded is a digest nobody can reproduce, and
-- an unreproducible digest cannot be compared to anything — which makes it
-- decoration. `manifest_method` names the algorithm, is constrained to the ones
-- this repository actually implements, and `scripts/models/collect_artefact_
-- manifests.py --verify` recomputes it.
--
-- REFERENCE_DATA. No new tables; `ira_app` reads `model_pull_allowlist` and
-- cannot write it, which `067` established and this does not change.
-- =============================================================================

BEGIN;

ALTER TABLE model_pull_allowlist
    ADD COLUMN IF NOT EXISTS manifest_sha256       TEXT,
    ADD COLUMN IF NOT EXISTS manifest_method       TEXT,
    ADD COLUMN IF NOT EXISTS manifest_source       TEXT,
    ADD COLUMN IF NOT EXISTS manifest_file_count   INTEGER,
    ADD COLUMN IF NOT EXISTS manifest_retrieved_at TIMESTAMPTZ;

COMMENT ON COLUMN model_pull_allowlist.manifest_sha256 IS
    'EVIDENCE, not verification. A digest over the file listing the source '
    'publishes at this revision. It is what the publisher says the artefact '
    'is; artifact_hash is what we checked after downloading, and the two may '
    'never be equal.';

COMMENT ON COLUMN model_pull_allowlist.manifest_method IS
    'How manifest_sha256 was derived. A digest whose derivation is unrecorded '
    'cannot be reproduced, and an unreproducible digest cannot be compared to '
    'anything.';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'manifest_sha256_is_a_sha256') THEN
        ALTER TABLE model_pull_allowlist ADD CONSTRAINT
            manifest_sha256_is_a_sha256 CHECK (
                manifest_sha256 IS NULL
                OR manifest_sha256 ~ '^sha256:[0-9a-f]{64}$');
    END IF;

    -- A method nobody implemented cannot be recorded. `collect_artefact_
    -- manifests.py` implements exactly these, and `--verify` recomputes them.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'manifest_method_is_implemented') THEN
        ALTER TABLE model_pull_allowlist ADD CONSTRAINT
            manifest_method_is_implemented CHECK (
                manifest_method IS NULL
                OR manifest_method IN ('huggingface-blobs-v1',
                                       'ollama-registry-manifest-v1'));
    END IF;

    -- All or nothing. A digest with no method, no source and no retrieval time
    -- is a number somebody put there.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'a_manifest_records_its_own_provenance') THEN
        ALTER TABLE model_pull_allowlist ADD CONSTRAINT
            a_manifest_records_its_own_provenance CHECK (
                (manifest_sha256 IS NULL
                 AND manifest_method IS NULL
                 AND manifest_source IS NULL
                 AND manifest_file_count IS NULL
                 AND manifest_retrieved_at IS NULL)
                OR (manifest_sha256 IS NOT NULL
                    AND manifest_method IS NOT NULL
                    AND btrim(coalesce(manifest_source, '')) <> ''
                    AND manifest_file_count IS NOT NULL
                    AND manifest_file_count > 0
                    AND manifest_retrieved_at IS NOT NULL));
    END IF;

    -- **The one that matters.** Copying the evidence digest into the
    -- verification column looks like filling in a missing field, makes a NULL
    -- go away, and afterwards nothing anywhere disagrees.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'a_published_manifest_is_not_a_verified_download') THEN
        ALTER TABLE model_pull_allowlist ADD CONSTRAINT
            a_published_manifest_is_not_a_verified_download CHECK (
                artifact_hash IS NULL
                OR manifest_sha256 IS NULL
                OR artifact_hash <> manifest_sha256);
    END IF;
END $$;


-- ── The evidence ────────────────────────────────────────────────────────────
--
-- Seeded here for the reason `067` seeded the allowlist itself: an empty
-- manifest column and a correct one are indistinguishable from outside. Both
-- verify nothing, and the first one also cannot tell an operator that the
-- artefact's shape changed under a pinned revision.
--
-- Every value below was RETRIEVED from the HuggingFace model API at the pinned
-- revision by `scripts/models/collect_artefact_manifests.py`, recorded in
-- `docs/cloud/evidence/MODEL_ARTEFACT_MANIFESTS.json`, and is recomputable from
-- that file offline with `--verify` — which recomputes each digest from its own
-- stored listing rather than reading the digest back.
--
-- None of it was guessed. A plausible 64-hex string satisfies every shape check
-- in this file and pins nothing, which is why the collector exists and why the
-- evidence file is committed beside it.
--
-- `artifact_hash` stays NULL on all three rows. Nothing has downloaded them.

UPDATE model_pull_allowlist SET
    manifest_sha256       = 'sha256:4d768c7d8f9d390ae4f61910621f4c8221a7b43d594e282347e8de2eb8caf1a6',
    manifest_method       = 'huggingface-blobs-v1',
    manifest_source       = 'https://huggingface.co/api/models/ai4bharat/indic-parler-tts/revision/7b527af5ee8ed1f9a28d80b19703ed9bb8ba10ca?blobs=true',
    manifest_file_count   = 10,
    manifest_retrieved_at = TIMESTAMPTZ '2026-08-09T14:26:02+00:00'
 WHERE source = 'huggingface' AND identifier = 'ai4bharat/indic-parler-tts'
   AND revision = '7b527af5ee8ed1f9a28d80b19703ed9bb8ba10ca';

UPDATE model_pull_allowlist SET
    manifest_sha256       = 'sha256:a620d42aac8083af7edbd7bbaae31b55d626815f1b0a5a1ba1ef6e08c6a63ea6',
    manifest_method       = 'huggingface-blobs-v1',
    manifest_source       = 'https://huggingface.co/api/models/k2-fsa/OmniVoice/revision/c5fdb5ccb189668d56333f77ba2629f4cd7535f4?blobs=true',
    manifest_file_count   = 13,
    manifest_retrieved_at = TIMESTAMPTZ '2026-08-09T14:26:02+00:00'
 WHERE source = 'huggingface' AND identifier = 'k2-fsa/OmniVoice'
   AND revision = 'c5fdb5ccb189668d56333f77ba2629f4cd7535f4';

UPDATE model_pull_allowlist SET
    manifest_sha256       = 'sha256:5ced4785df40fdba88888ff3bcb6c544d75646bb76323a654474abcb4ed2ee21',
    manifest_method       = 'huggingface-blobs-v1',
    manifest_source       = 'https://huggingface.co/api/models/pyannote/speaker-diarization-3.1/revision/84fd25912480287da0247647c3d2b4853cb3ee5d?blobs=true',
    manifest_file_count   = 24,
    manifest_retrieved_at = TIMESTAMPTZ '2026-08-09T14:26:02+00:00'
 WHERE source = 'huggingface' AND identifier = 'pyannote/speaker-diarization-3.1'
   AND revision = '84fd25912480287da0247647c3d2b4853cb3ee5d';

COMMIT;
