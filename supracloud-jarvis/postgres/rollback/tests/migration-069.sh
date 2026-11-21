#!/usr/bin/env bash
#
# Migration 069 seeds licence evidence and no determination
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# A DATA migration. It creates nothing, alters nothing and indexes
# nothing — it seeds fifteen rows of publisher licence evidence into the
# table 068 created.
#
# That shape has its own failure mode, and it is quiet: peel 069 and
# never re-apply it, and every table exists, every index exists, every
# privilege is right, and the register is simply EMPTY. The system still
# fails closed — nothing can serve without an approved licence — it just
# loses the ability to say which licences are outstanding. So this step
# counts rows rather than checking the table is there.
#
# The second half is the one that matters more: none of those rows may
# carry a determination. 068's constraints make that unrepresentable,
# and this proves the constraints are in force on a database built from
# the whole chain rather than trusting that they are.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest069
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
q() { ${PSQL} -d ${DB} -tAc "$1"; }
for f in postgres/init.sql postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f"
done

seeded=$(q "SELECT count(*) FROM model_licence_register")
[ "${seeded}" -ge 15 ] \
  || { echo "::error::069 seeded ${seeded} licence rows, expected at least 15"; exit 1; }
echo "✅ 069 seeds ${seeded} rows of licence evidence"

determined=$(q "SELECT count(*) FROM model_licence_register WHERE classification NOT IN ('unknown','review_required')")
[ "${determined}" = "0" ] \
  || { echo "::error::${determined} seeded licence row(s) carry a determination — only a named human may record one"; exit 1; }
echo "✅ no seeded row carries a licence determination"

# Every row has provenance. An identifier with no URL and no timestamp
# is a remembered licence, not a recorded one — the P5-03 shape.
unsourced=$(q "SELECT count(*) FROM model_licence_register WHERE evidence_url = '' OR evidence_retrieved_at IS NULL")
[ "${unsourced}" = "0" ] \
  || { echo "::error::${unsourced} licence row(s) have no provenance"; exit 1; }
echo "✅ every seeded row carries its provenance"

# The two rows this phase exists to have found. Named explicitly,
# because a re-collection that silently dropped either would leave the
# counts above intact.
#
# This assertion used to read `= 'unknown'` for OmniVoice, and it was
# asserting the defect. 069 seeded `unknown` because the collector read
# structured metadata only; the publisher had declared CC-BY-NC in the
# card body all along, and 074 records it. This step applies the WHOLE
# chain, so what it sees is the post-074 state, and pinning it to
# `unknown` would have made the fix unmergeable — a guard defending the
# bug it was written to expose.
#
# What it checks now is strictly more than it did: the row is still
# there, it is still pre-determination, no human is named on it, and
# the terms the publisher stated are recorded rather than merely
# absent. A silent drop still fails, and so does a silent approval.
omni_class=$(q "SELECT classification FROM model_licence_register WHERE identifier='k2-fsa/OmniVoice'")
[ "${omni_class}" = "review_required" ] \
  || { echo "::error::k2-fsa/OmniVoice, which IRA already fetches, is '${omni_class}' after the full chain, not review_required"; exit 1; }
[ "$(q "SELECT coalesce(classified_by,'') FROM model_licence_register WHERE identifier='k2-fsa/OmniVoice'")" = "" ] \
  || { echo "::error::a human is named on the OmniVoice row — the chain must not record a determination"; exit 1; }
[ "$(q "SELECT licence_identifier FROM model_licence_register WHERE identifier='k2-fsa/OmniVoice'")" = "CC-BY-NC" ] \
  || { echo "::error::the terms the OmniVoice card states are not recorded — absent metadata is not absent terms"; exit 1; }
gemma=$(q "SELECT count(*) FROM model_licence_register WHERE licence_identifier='Gemma Terms of Use'")
[ "${gemma}" -ge 3 ] \
  || { echo "::error::the Gemma use-policy rows are missing or were normalised into a licence identifier"; exit 1; }
echo "✅ the CC-BY-NC row is recorded, undetermined, and the three use-policy rows are verbatim"

# 069's own seeding, before 074 amended it, is still what it was: it
# recorded no licence identifier for that artefact and did not guess
# one. Read from the file rather than the database, because the
# database now legitimately shows the amended state.
grep -q "k2-fsa/OmniVoice" postgres/069_model_licence_evidence.sql \
  || { echo "::error::069 no longer seeds the OmniVoice row at all"; exit 1; }
echo "✅ 069 still seeds the row 074 later amended"

# And nothing serves. Every licence is review_required or unknown, so
# 068's gate refuses every version — MOD-01 blocking, working.
[ "$(q "SELECT count(*) FROM model_registry WHERE status='serving'")" = "0" ] \
  || { echo "::error::a model is serving on a chain where no licence has been determined"; exit 1; }
echo "✅ nothing serves — MOD-01 blocking, as described"

# The rollback removes what it seeded and REFUSES to discard a human
# determination made on top of it.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "UPDATE model_licence_register SET classification='known_approved', classified_by='CI FIXTURE — not a licence determination', classified_at=NOW(), determination_note='CI fixture only.', commercial_use='permitted', hosted_saas_redistribution='permitted' WHERE identifier='qwen3' AND reference='14b'"
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/069_model_licence_evidence_down.sql 2>/tmp/r069.err; then
  echo "::error::rollback/069 deleted a row carrying a named person's recorded reading of a licence"
  exit 1
fi
grep -q "069-down REFUSING" /tmp/r069.err \
  || { echo "::error::rollback/069 failed for the wrong reason:"; cat /tmp/r069.err; exit 1; }
echo "✅ rollback/069 refuses to discard a human determination"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "UPDATE model_licence_register SET classification='review_required', classified_by=NULL, classified_at=NULL, determination_note='', commercial_use='unknown', hosted_saas_redistribution='unknown' WHERE identifier='qwen3' AND reference='14b'"
# 090 seeds five more register rows on top of 069, and 098 three more. The newer
# layers that write the same table come off first, newest first — the chain
# cycle's rule — so the "left nothing behind" assertion below keeps its exact zero.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/098_memory_and_speaker_model_pins_down.sql
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/090_voice_model_pins_down.sql
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/069_model_licence_evidence_down.sql
[ "$(q "SELECT count(*) FROM model_licence_register")" = "0" ] \
  || { echo "::error::rollback/069 left seeded rows behind"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/069_model_licence_evidence.sql
[ "$(q "SELECT count(*) FROM model_licence_register")" -ge 15 ] \
  || { echo "::error::069 did not re-apply"; exit 1; }
echo "✅ 069 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
