#!/usr/bin/env bash
#
# Migration 074 records the terms the collector could not see
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# A DATA migration, on its own disposable database.
#
# L-03 said of k2-fsa/OmniVoice "there is nothing to read". At the same
# pinned revision the model card says the code is Apache 2.0 and the
# pre-trained model is CC-BY-NC. The collector read structured metadata
# only, so the register held `unknown` while the publisher had declared
# terms — on the one artefact IRA already fetches.
#
# This step asserts the row carries the evidence AND that recording it
# did not become a determination: classified_by stays NULL and the
# licence questions stay unanswered.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest074
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
q() { ${PSQL} -d ${DB} -tAc "$1"; }
for f in postgres/init.sql postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f"
done

[ "$(q "SELECT licence_identifier FROM model_licence_register WHERE identifier='k2-fsa/OmniVoice'")" = "CC-BY-NC" ] \
  || { echo "::error::074 did not record the weights licence the card states"; exit 1; }
[ "$(q "SELECT classification FROM model_licence_register WHERE identifier='k2-fsa/OmniVoice'")" = "review_required" ] \
  || { echo "::error::the OmniVoice row is not review_required"; exit 1; }
[ "$(q "SELECT evidence_method FROM model_licence_register WHERE identifier='k2-fsa/OmniVoice'")" = "huggingface-model-card" ] \
  || { echo "::error::074 did not record WHERE the statement was read from"; exit 1; }
echo "✅ 074 records the card evidence"

# Recording evidence is not determining anything.
[ "$(q "SELECT count(*) FROM model_licence_register WHERE classified_by IS NOT NULL")" = "0" ] \
  || { echo "::error::a migration recorded a licence determination"; exit 1; }
[ "$(q "SELECT count(*) FROM model_licence_register WHERE commercial_use <> 'unknown' OR derivative_distribution <> 'unknown'")" = "0" ] \
  || { echo "::error::a migration answered a licence question"; exit 1; }
[ "$(q "SELECT count(*) FROM model_licence_register WHERE classification NOT IN ('unknown','review_required')")" = "0" ] \
  || { echo "::error::a register row carries a determination"; exit 1; }
echo "✅ 074 determines nothing"

# The method vocabulary widened by exactly one, and nothing else.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "UPDATE model_licence_register SET evidence_method='publisher-document' WHERE identifier='k2-fsa/OmniVoice'"
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "UPDATE model_licence_register SET evidence_method='trust-me' WHERE identifier='k2-fsa/OmniVoice'" 2>/tmp/c074.err; then
  echo "::error::a method nothing implements was accepted"; exit 1
fi
grep -q "evidence_method_check" /tmp/c074.err \
  || { echo "::error::refused for the wrong reason:"; cat /tmp/c074.err; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "UPDATE model_licence_register SET evidence_method='huggingface-model-card' WHERE identifier='k2-fsa/OmniVoice'"
echo "✅ 074 widened the evidence-method vocabulary by exactly one"

# ── The rollback refuses to discard a human determination ────────
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "UPDATE model_licence_register SET classification='known_restricted', classified_by='CI FIXTURE — not a determination', classified_at=NOW(), determination_note='ci', commercial_use='restricted', hosted_saas_redistribution='restricted' WHERE identifier='k2-fsa/OmniVoice'"
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/074_omnivoice_card_evidence_down.sql 2>/tmp/r074.err; then
  echo "::error::rollback/074 deleted the evidence a determination was made against"
  exit 1
fi
grep -q "074-down REFUSING" /tmp/r074.err \
  || { echo "::error::rollback/074 failed for the wrong reason:"; cat /tmp/r074.err; exit 1; }
echo "✅ rollback/074 refuses to discard a determination's evidence"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "UPDATE model_licence_register SET classification='review_required', classified_by=NULL, classified_at=NULL, commercial_use='unknown', hosted_saas_redistribution='unknown', derivative_distribution='unknown' WHERE identifier='k2-fsa/OmniVoice'"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/074_omnivoice_card_evidence_down.sql
[ "$(q "SELECT classification FROM model_licence_register WHERE identifier='k2-fsa/OmniVoice'")" = "unknown" ] \
  || { echo "::error::rollback/074 did not restore 069's state"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/074_omnivoice_card_evidence.sql
[ "$(q "SELECT licence_identifier FROM model_licence_register WHERE identifier='k2-fsa/OmniVoice'")" = "CC-BY-NC" ] \
  || { echo "::error::074 did not re-apply"; exit 1; }
echo "✅ 074 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
