#!/usr/bin/env bash
#
# Migration 073 keeps a publisher's claim out of the verification column
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# Same shape as 054 through 072, on its own disposable database.
#
# PHASE_5_CLOSURE_REPORT §7 decision 3 assigned the fetch-and-hash for
# `model_pull_allowlist.artifact_hash` to Phase 7, noting Phase 6 got
# Ollama digests without downloading. It IS cheaper — and it is a
# DIFFERENT FACT. 067 said artifact_hash NULL means "allowed to fetch,
# not yet verified"; filling it from an API response would keep the
# column's name and every reader's expectation while changing what it
# means, which is Phase 6's commercial_licence finding one layer along.
#
# The load-bearing assertion is the last one: the two digests may never
# be equal. Copying the evidence into the verification column looks like
# filling in a missing field and makes a NULL go away.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest073
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
q() { ${PSQL} -d ${DB} -tAc "$1"; }
reg() { ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "$1"; }
refuse() {
  if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "$2" 2>/tmp/c073.err; then
    echo "::error::$3"; exit 1
  fi
  grep -q "$1" /tmp/c073.err \
    || { echo "::error::refused for the wrong reason (wanted $1):"; cat /tmp/c073.err; exit 1; }
}
for f in postgres/init.sql postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f"
done

# 090 seeds five more allowlist rows, with manifests, on top of 073, and 098
# three more. This script proves 073 against the chain beneath it, so the newer
# layers that write the same table come off first, newest first — the chain
# cycle's rule — and every assertion below keeps its exact count rather than
# being widened to fit.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/098_memory_and_speaker_model_pins_down.sql
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/090_voice_model_pins_down.sql

M=sha256:$(printf '1%.0s' $(seq 1 64))
H=sha256:$(printf '2%.0s' $(seq 1 64))
R=$(printf 'c%.0s' $(seq 1 40))

# The seed landed on all three allowlisted artefacts, and NONE of them
# has a verified download.
[ "$(q "SELECT count(*) FROM model_pull_allowlist WHERE manifest_sha256 IS NOT NULL")" = "3" ] \
  || { echo "::error::073 did not seed the manifests; an empty manifest column and a correct one are indistinguishable"; exit 1; }
[ "$(q "SELECT count(*) FROM model_pull_allowlist WHERE artifact_hash IS NOT NULL")" = "0" ] \
  || { echo "::error::an artifact_hash is recorded and nothing has downloaded anything"; exit 1; }
[ "$(q "SELECT count(*) FROM model_pull_allowlist WHERE manifest_sha256 IS NOT NULL AND manifest_method <> 'huggingface-blobs-v1'")" = "0" ] \
  || { echo "::error::a manifest was recorded under a method nothing implements"; exit 1; }
for p in INSERT UPDATE DELETE; do
  [ "$(q "SELECT has_table_privilege('ira_app','model_pull_allowlist','$p')")" = "f" ] \
    || { echo "::error::ira_app holds $p on model_pull_allowlist after 073"; exit 1; }
done
echo "✅ 073 records the publisher's manifests and no verified download"

refuse "a_manifest_records_its_own_provenance" \
  "INSERT INTO model_pull_allowlist (source,identifier,revision,manifest_sha256) VALUES ('huggingface','ci/73a','${R}','${M}')" \
  "a manifest digest was recorded with no method, source or retrieval time — a number somebody put there"
echo "✅ 073 refuses a digest with no provenance"

refuse "manifest_method_is_implemented" \
  "INSERT INTO model_pull_allowlist (source,identifier,revision,manifest_sha256,manifest_method,manifest_source,manifest_file_count,manifest_retrieved_at) VALUES ('huggingface','ci/73b','${R}','${M}','trust-me','https://example.invalid/x',3,NOW())" \
  "a manifest was recorded under a derivation nothing can reproduce"
echo "✅ 073 refuses a method nobody implemented"

refuse "a_published_manifest_is_not_a_verified_download" \
  "INSERT INTO model_pull_allowlist (source,identifier,revision,manifest_sha256,manifest_method,manifest_source,manifest_file_count,manifest_retrieved_at,artifact_hash) VALUES ('huggingface','ci/73c','${R}','${M}','huggingface-blobs-v1','https://example.invalid/x',3,NOW(),'${M}')" \
  "the publisher's digest was copied into artifact_hash — a column that means 'we downloaded this and checked it'"
echo "✅ 073 refuses to let evidence impersonate verification"

reg "INSERT INTO model_pull_allowlist (source,identifier,revision,manifest_sha256,manifest_method,manifest_source,manifest_file_count,manifest_retrieved_at,artifact_hash) VALUES ('huggingface','ci/73d','${R}','${M}','huggingface-blobs-v1','https://example.invalid/x',3,NOW(),'${H}')"
echo "✅ 073 still allows a genuinely verified download beside a manifest"

# ── The rollback does NOT refuse, and that is deliberate ─────────
#
# 068, 070, 071 and 072 all refuse while they hold something a person
# decided. A manifest digest is machine-collected evidence the
# collector regenerates in seconds, and a guard that protects a
# reproducible value teaches operators that these refusals are noise.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/073_artefact_manifests_down.sql
[ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='model_pull_allowlist' AND column_name='manifest_sha256'")" = "0" ] \
  || { echo "::error::rollback/073 left the column"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/073_artefact_manifests.sql
[ "$(q "SELECT count(*) FROM model_pull_allowlist WHERE manifest_sha256 IS NOT NULL")" = "3" ] \
  || { echo "::error::073 re-applied without re-seeding its evidence"; exit 1; }
echo "✅ 073 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
