-- =============================================================================
-- rollback/073_artefact_manifests_down.sql
--
-- Removes the artefact-manifest evidence columns.
--
-- ── This one does NOT refuse, and the asymmetry is the point ───────────────
--
-- `068`, `070`, `071` and `072` all refuse to reverse while they hold something
-- **a person decided**: a licence classification, a lawful basis, an owner's
-- approval to spend money, the link between a served artefact and the run
-- behind it. None of those can be reconstructed by re-running anything.
--
-- A manifest digest is the opposite kind of value. It is machine-collected
-- evidence about what a publisher publishes, and
-- `scripts/models/collect_artefact_manifests.py` regenerates it from the same
-- source in seconds. Guarding it would make an emergency rollback harder for
-- nothing, and a guard that protects a reproducible value teaches operators
-- that these refusals are noise.
--
-- ── Nor can dropping it widen anything ─────────────────────────────────────
--
-- `artifact_hash` is untouched and is NULL on every row either way, so the
-- fetch path fails closed before and after. What is lost is the ability to
-- notice that an artefact's SHAPE changed under a pinned revision — a
-- detection, not a permission.
-- =============================================================================

BEGIN;

ALTER TABLE model_pull_allowlist
    DROP CONSTRAINT IF EXISTS a_published_manifest_is_not_a_verified_download,
    DROP CONSTRAINT IF EXISTS a_manifest_records_its_own_provenance,
    DROP CONSTRAINT IF EXISTS manifest_method_is_implemented,
    DROP CONSTRAINT IF EXISTS manifest_sha256_is_a_sha256;

ALTER TABLE model_pull_allowlist
    DROP COLUMN IF EXISTS manifest_sha256,
    DROP COLUMN IF EXISTS manifest_method,
    DROP COLUMN IF EXISTS manifest_source,
    DROP COLUMN IF EXISTS manifest_file_count,
    DROP COLUMN IF EXISTS manifest_retrieved_at;

COMMIT;
