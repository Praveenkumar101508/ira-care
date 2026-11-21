-- =============================================================================
-- 069_model_licence_evidence.sql — Phase 6, batch P6-02.
--
-- Seeds `model_licence_register` with the licence EVIDENCE for every model IRA
-- is configured to use, collected from authoritative source metadata by
-- `scripts/models/collect_licence_evidence.py` and recorded verbatim in
-- `docs/cloud/evidence/MODEL_LICENCE_EVIDENCE.json`.
--
-- ── Every row below is `review_required` or `unknown`. That is the point. ──
--
-- `MODEL_LICENSE_REGISTER.md` says it plainly:
--
--   Nothing below is filled in from memory. Model licences change between
--   releases and a wrong entry here is worse than an empty one, because it
--   would be relied upon.
--
-- So this migration records WHAT THE PUBLISHER PUBLISHED, with provenance, and
-- records nothing about what it permits. Every row leaves `commercial_use`,
-- `hosted_saas_redistribution`, `derivative_distribution`,
-- `attribution_required` and `output_restrictions` at `unknown`, and
-- `classified_by` NULL.
--
-- This is not a formality. `068`'s `undetermined_rows_answer_nothing` and
-- `determination_names_a_human` would REFUSE this migration if it tried to do
-- otherwise — a migration cannot approve a licence, and the failure would be at
-- apply time rather than in review.
--
-- Consequence, stated so nobody has to discover it: **no model can serve after
-- this migration**, because `model_registry_licence_gate` requires
-- `known_approved`. That is MOD-01 doing what it was always described as doing
-- — blocking — and it is why `OPEN_DECISIONS.md` D-08 and
-- `docs/cloud/MODEL_LICENCE_DETERMINATIONS_REQUIRED.md` exist.
--
-- ── Where each value came from ─────────────────────────────────────────────
--
-- **Ollama** (`registry.ollama.ai/v2/library/<name>/manifests/<tag>`) — the
-- `licence_identifier` is the FIRST LINE of the licence text the publisher
-- ships inside the artefact, quoted exactly; `licence_text_sha256` is that
-- text hashed; `artifact_digest` is the `vnd.ollama.image.model` layer digest,
-- available without downloading the weights.
--
-- **Hugging Face** (`huggingface.co/api/models/<id>/revision/<sha>`) — the
-- `licence_identifier` is `cardData.license` at the pinned revision, and
-- `artifact_digest` is that commit sha.
--
-- Nothing is normalised. `Apache License` is stored as `Apache License` and not
-- as `apache-2.0`, because the first is what the file says and the second is a
-- reading of it. `Gemma Terms of Use` is stored as itself for the same reason,
-- and it is exactly the row `MODEL_LICENSE_REGISTER.md` warned about in advance:
--
--   Gemma ships under a use-policy licence rather than, say, Apache-2.0.
--
-- ── The one row with no licence at all ─────────────────────────────────────
--
-- `k2-fsa/OmniVoice` declares nothing: no `cardData.license`, no `license_name`,
-- no `license:*` tag. IRA ALREADY FETCHES IT — `067` seeds it into
-- `model_pull_allowlist` and `core/models/artefacts.py` pins it. It is recorded
-- `unknown` with a NULL identifier, and `068`'s `approval_rests_on_evidence`
-- makes it unapprovable until somebody records terms obtained from the
-- publisher. Inferring one from its neighbours would be the failure this whole
-- phase exists to prevent. Tracked as **L-03**.
--
-- ── Why the evidence lives in a migration and not only in the JSON ─────────
--
-- The gate reads the database. Evidence held only in a file in the repository
-- would leave every deployed environment with an empty register, which fails
-- closed correctly and tells an operator nothing about WHY. Seeding it means a
-- fresh environment comes up already knowing which licences are outstanding.
--
-- Re-collecting evidence later produces a new JSON and a new forward migration.
-- These rows are not edited in place: an approval binds to `artifact_digest`,
-- and rewriting a digest under a live approval is what `068`'s withdrawal
-- trigger refuses.
--
-- ON CONFLICT DO NOTHING because the rollback cycle re-applies this file.
-- =============================================================================

BEGIN;

INSERT INTO model_licence_register
    (source, identifier, reference, licence_identifier, licence_text_sha256,
     artifact_digest, evidence_url, evidence_method, evidence_retrieved_at,
     classification)
VALUES
    ('huggingface', 'ai4bharat/indic-parler-tts', '7b527af5ee8ed1f9a28d80b19703ed9bb8ba10ca', 'apache-2.0', NULL, '7b527af5ee8ed1f9a28d80b19703ed9bb8ba10ca', 'https://huggingface.co/api/models/ai4bharat/indic-parler-tts/revision/7b527af5ee8ed1f9a28d80b19703ed9bb8ba10ca', 'huggingface-model-api', TIMESTAMPTZ '2026-08-09T08:10:23+00:00', 'review_required'),
    ('huggingface', 'k2-fsa/OmniVoice', 'c5fdb5ccb189668d56333f77ba2629f4cd7535f4', NULL, NULL, 'c5fdb5ccb189668d56333f77ba2629f4cd7535f4', 'https://huggingface.co/api/models/k2-fsa/OmniVoice/revision/c5fdb5ccb189668d56333f77ba2629f4cd7535f4', 'huggingface-model-api', TIMESTAMPTZ '2026-08-09T08:10:23+00:00', 'unknown'),
    ('huggingface', 'pyannote/speaker-diarization-3.1', '84fd25912480287da0247647c3d2b4853cb3ee5d', 'mit', NULL, '84fd25912480287da0247647c3d2b4853cb3ee5d', 'https://huggingface.co/api/models/pyannote/speaker-diarization-3.1/revision/84fd25912480287da0247647c3d2b4853cb3ee5d', 'huggingface-model-api', TIMESTAMPTZ '2026-08-09T08:10:23+00:00', 'review_required'),
    ('ollama', 'bge-m3', 'latest', 'MIT License', 'sha256:a406579cd136771c705c521db86ca7d60a6f3de7c9b5460e6193a2df27861bde', 'sha256:daec91ffb5dd0c27411bd71f29932917c49cf529a641d0168496c3a501e3062c', 'https://registry.ollama.ai/v2/library/bge-m3/manifests/latest', 'ollama-registry-manifest', TIMESTAMPTZ '2026-08-09T08:10:23+00:00', 'review_required'),
    ('ollama', 'deepseek-r1', '14b', 'MIT License', 'sha256:6e4c38e1172f42fdbff13edf9a7a017679fb82b0fde415a3e8b3c31c6ed4a4e4', 'sha256:6e9f90f02bb3b39b59e81916e8cfce9deb45aeaeb9a54a5be4414486b907dc1e', 'https://registry.ollama.ai/v2/library/deepseek-r1/manifests/14b', 'ollama-registry-manifest', TIMESTAMPTZ '2026-08-09T08:10:23+00:00', 'review_required'),
    ('ollama', 'deepseek-r1', '32b', 'MIT License', 'sha256:6e4c38e1172f42fdbff13edf9a7a017679fb82b0fde415a3e8b3c31c6ed4a4e4', 'sha256:6150cb382311b69f09cc0f9a1b69fc029cbd742b66bb8ec531aa5ecf5c613e93', 'https://registry.ollama.ai/v2/library/deepseek-r1/manifests/32b', 'ollama-registry-manifest', TIMESTAMPTZ '2026-08-09T08:10:23+00:00', 'review_required'),
    ('ollama', 'deepseek-r1', '8b', 'MIT License', 'sha256:6e4c38e1172f42fdbff13edf9a7a017679fb82b0fde415a3e8b3c31c6ed4a4e4', 'sha256:e6a7edc1a4d7d9b2de136a221a57336b76316cfe53a252aeba814496c5ae439d', 'https://registry.ollama.ai/v2/library/deepseek-r1/manifests/8b', 'ollama-registry-manifest', TIMESTAMPTZ '2026-08-09T08:10:23+00:00', 'review_required'),
    ('ollama', 'gemma3', '12b', 'Gemma Terms of Use', 'sha256:dd084c7d92a3c1c14cc09ae77153b903fd2024b64a100a0cc8ec9316063d2dbc', 'sha256:e8ad13eff07a78d89926e9e8b882317d082ef5bf9768ad7b50fcdbbcd63748de', 'https://registry.ollama.ai/v2/library/gemma3/manifests/12b', 'ollama-registry-manifest', TIMESTAMPTZ '2026-08-09T08:10:23+00:00', 'review_required'),
    ('ollama', 'gemma3', '4b', 'Gemma Terms of Use', 'sha256:dd084c7d92a3c1c14cc09ae77153b903fd2024b64a100a0cc8ec9316063d2dbc', 'sha256:aeda25e63ebd698fab8638ffb778e68bed908b960d39d0becc650fa981609d25', 'https://registry.ollama.ai/v2/library/gemma3/manifests/4b', 'ollama-registry-manifest', TIMESTAMPTZ '2026-08-09T08:10:23+00:00', 'review_required'),
    ('ollama', 'gemma3n', 'e4b', 'Gemma Terms of Use', 'sha256:1adbfec9dcf025cbf301c072f3847527468dcfa399da7491ee4a1c9e9f1b33e9', 'sha256:38e8dcc30df4eb0e29eaf5c74ba6ce3f2cd66badad50768fc14362acfb8b8cb6', 'https://registry.ollama.ai/v2/library/gemma3n/manifests/e4b', 'ollama-registry-manifest', TIMESTAMPTZ '2026-08-09T08:10:23+00:00', 'review_required'),
    ('ollama', 'nomic-embed-text', 'latest', 'Apache License', 'sha256:c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4', 'sha256:970aa74c0a90ef7482477cf803618e776e173c007bf957f635f1015bfcfef0e6', 'https://registry.ollama.ai/v2/library/nomic-embed-text/manifests/latest', 'ollama-registry-manifest', TIMESTAMPTZ '2026-08-09T08:10:23+00:00', 'review_required'),
    ('ollama', 'qwen3', '14b', 'Apache License', 'sha256:d18a5cc71b84bc4af394a31116bd3932b42241de70c77d2b76d69a314ec8aa12', 'sha256:a8cc1361f3145dc01f6d77c6c82c9116b9ffe3c97b34716fe20418455876c40e', 'https://registry.ollama.ai/v2/library/qwen3/manifests/14b', 'ollama-registry-manifest', TIMESTAMPTZ '2026-08-09T08:10:23+00:00', 'review_required'),
    ('ollama', 'qwen3', '4b', 'Apache License', 'sha256:d18a5cc71b84bc4af394a31116bd3932b42241de70c77d2b76d69a314ec8aa12', 'sha256:3e4cb14174460404e7a233e531675303b2fbf7749c02f91864fe311ab6344e4f', 'https://registry.ollama.ai/v2/library/qwen3/manifests/4b', 'ollama-registry-manifest', TIMESTAMPTZ '2026-08-09T08:10:23+00:00', 'review_required'),
    ('ollama', 'qwen3', '8b', 'Apache License', 'sha256:d18a5cc71b84bc4af394a31116bd3932b42241de70c77d2b76d69a314ec8aa12', 'sha256:a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f', 'https://registry.ollama.ai/v2/library/qwen3/manifests/8b', 'ollama-registry-manifest', TIMESTAMPTZ '2026-08-09T08:10:23+00:00', 'review_required'),
    ('ollama', 'qwen3-coder-next', 'latest', 'Apache License', 'sha256:7339fa418c9ad3e8e12e74ad0fd26a9cc4be8703f9c110728a992b193be85cb2', 'sha256:30e51a7cb1cf1333b9e298b90b4c7790fe2572d8736b002482a0ac96328a2ffb', 'https://registry.ollama.ai/v2/library/qwen3-coder-next/manifests/latest', 'ollama-registry-manifest', TIMESTAMPTZ '2026-08-09T08:10:23+00:00', 'review_required')
ON CONFLICT (source, identifier, reference) DO NOTHING;

COMMIT;
