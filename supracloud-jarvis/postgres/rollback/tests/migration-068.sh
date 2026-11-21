#!/usr/bin/env bash
#
# Migration 068 separates licence evidence from licence determination
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# Same shape as 054 through 067. On its own disposable database, so a
# destructive rollback test cannot reach the development one.
#
# MOD-01 is "the licence position of every configured model is
# unrecorded". 067 answered it with a CHECK that `commercial_licence IS
# NOT NULL`, and IMPLEMENTED_VS_PLANNED.md called that "a gate, not an
# answer". It is not a gate: the field is free text, so one character
# satisfies it. The first assertion below demonstrates that on the
# deployed schema rather than arguing it.
#
# Every other assertion is a REFUSAL. A register that accepted
# everything and a register that enforced everything look identical
# from a successful INSERT.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest068
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
q() { ${PSQL} -d ${DB} -tAc "$1"; }
reg() { ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "$1"; }
refuse() {
  # $1 = the constraint or message expected, $2 = the SQL, $3 = what
  # it would mean if it succeeded.
  if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "$2" 2>/tmp/c068.err; then
    echo "::error::$3"; exit 1
  fi
  grep -q "$1" /tmp/c068.err \
    || { echo "::error::refused for the wrong reason (wanted $1):"; cat /tmp/c068.err; exit 1; }
}
for f in postgres/init.sql postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f"
done

H=sha256:$(printf 'x%.0s' $(seq 1 64) | tr 'x' 'a')
D=sha256:$(printf 'x%.0s' $(seq 1 64) | tr 'x' 'b')

[ "$(q "SELECT to_regclass('public.model_licence_register') IS NOT NULL")" = "t" ] \
  || { echo "::error::068 did not create model_licence_register"; exit 1; }
[ "$(q "SELECT count(*) FROM pg_attribute WHERE attrelid='model_registry'::regclass AND attname='licence_id' AND NOT attisdropped")" = "1" ] \
  || { echo "::error::068 did not add model_registry.licence_id"; exit 1; }

# The runtime role reads and cannot write. Asserted HERE because a
# process that answers requests and can UPDATE this table can approve
# its own licence — and because 042's ALTER DEFAULT PRIVILEGES grants
# ira_app full DML on every new table, so 068's GRANT SELECT grants
# nothing on its own. P5-01 shipped exactly that mistake on 067.
#
# This database is built as `postgres`, so the tables are NOT owned by
# ira_schema_owner and 042's default ACL never fires here. That means
# this check cannot observe the bug it is named for — which is the
# second half of the P5-01 finding, recorded rather than repeated. The
# load-bearing version of this assertion is in
# tests/tenancy/test_p6_licence_register_schema.py, against a database
# the migrator built, and in the staging acceptance script.
for p in INSERT UPDATE DELETE; do
  [ "$(q "SELECT has_table_privilege('ira_app','model_licence_register','$p')")" = "f" ] \
    || { echo "::error::ira_app holds $p on model_licence_register"; exit 1; }
done
echo "✅ 068 creates the register and the runtime role cannot write it"

# ── The defect 068 exists for, demonstrated ──────────────────────
reg "INSERT INTO model_registry (model_key,version,base_model,revision,artifact_hash,environment,commercial_licence,licence_recorded_at) VALUES ('g',1,'org/m','abc123','${H}','development','x',NOW())"
echo "✅ 067's commercial_licence still accepts the string 'x' — the reason 068 exists"

# ── Serving now requires an approved licence row ─────────────────
refuse "no licence register row" \
  "UPDATE model_registry SET status='serving', rollout_percentage=100 WHERE model_key='g'" \
  "a model was set serving with a one-character licence string and no register row"
echo "✅ 068 refuses to serve a model with no licence register row"

reg "INSERT INTO model_licence_register (source,identifier,reference,licence_identifier,artifact_digest,evidence_url,evidence_method,evidence_retrieved_at,classification) VALUES ('ollama','ci/g','1b','Apache License','${D}','https://example.invalid/ci','owner-supplied',NOW(),'review_required')"
L=$(q "SELECT id FROM model_licence_register WHERE identifier='ci/g'")

refuse "its licence is review_required" \
  "UPDATE model_registry SET status='serving', rollout_percentage=100, licence_id='${L}'::uuid WHERE model_key='g'" \
  "a model served on a licence nobody has read — review_required must fail closed"
echo "✅ 068 refuses to serve on an unread licence"

# ── Engineering may not make the determination ───────────────────
for who in migration automation system script collector engineering ira bot ci operator postgres tbd none MIGRATION; do
  refuse "determination_names_a_human" \
    "UPDATE model_licence_register SET classification='known_approved', classified_by='${who}', classified_at=NOW(), determination_note='n', commercial_use='permitted', hosted_saas_redistribution='permitted' WHERE id='${L}'::uuid" \
    "an approval attributed to '${who}' was accepted — OPEN_DECISIONS D-08 is the open question of who may record one"
done
echo "✅ 068 refuses an approval attributed to a process"

refuse "undetermined_rows_answer_nothing" \
  "UPDATE model_licence_register SET commercial_use='permitted' WHERE id='${L}'::uuid" \
  "a review_required row answered the commercial question — the determination made in all but name"
echo "✅ 068 refuses an undetermined row that answers the questions"

refuse "approval_answers_the_commercial_questions" \
  "UPDATE model_licence_register SET classification='known_approved', classified_by='CI FIXTURE — not a licence determination', classified_at=NOW(), determination_note='fixture', commercial_use='permitted' WHERE id='${L}'::uuid" \
  "an approval left hosted SaaS redistribution unanswered — the question that matters for a paid product"
echo "✅ 068 refuses an approval that answers only half"

# An artefact whose publisher declares no licence cannot be approved
# at all. That is k2-fsa/OmniVoice, which IRA already fetches.
reg "INSERT INTO model_licence_register (source,identifier,reference,artifact_digest,evidence_url,evidence_method,evidence_retrieved_at,classification) VALUES ('huggingface','ci/no-licence','main','${D}','https://example.invalid/none','huggingface-model-api',NOW(),'unknown')"
N=$(q "SELECT id FROM model_licence_register WHERE identifier='ci/no-licence'")
refuse "approval_rests_on_evidence" \
  "UPDATE model_licence_register SET classification='known_approved', classified_by='CI FIXTURE — not a licence determination', classified_at=NOW(), determination_note='fixture', commercial_use='permitted', hosted_saas_redistribution='permitted' WHERE id='${N}'::uuid" \
  "a licence with no recorded terms was approved — unknown must not become approved by editing a column"
echo "✅ 068 refuses to approve a licence with no evidence"

# ── A named human may approve, and then the model may serve ──────
reg "UPDATE model_licence_register SET classification='known_approved', classified_by='CI FIXTURE — not a licence determination', classified_at=NOW(), determination_note='CI fixture only. No licence text was read and no determination was made.', commercial_use='permitted', hosted_saas_redistribution='permitted' WHERE id='${L}'::uuid"
reg "UPDATE model_registry SET status='serving', rollout_percentage=100, licence_id='${L}'::uuid WHERE model_key='g'"
[ "$(q "SELECT status FROM model_registry WHERE model_key='g'")" = "serving" ] \
  || { echo "::error::an approved licence did not let the model serve — the gate refuses everything"; exit 1; }
echo "✅ 068 lets an approved licence serve"

# ── Evidence freezes, and withdrawal stops the model ─────────────
refuse "evidence cannot change while the row is approved" \
  "UPDATE model_licence_register SET artifact_digest='${H}' WHERE id='${L}'::uuid" \
  "the evidence under a live approval was repointed at a different artefact"
echo "✅ 068 freezes evidence under a live approval"

reg "UPDATE model_licence_register SET classification='known_restricted', determination_note='CI fixture withdrawal' WHERE id='${L}'::uuid"
[ "$(q "SELECT status FROM model_registry WHERE model_key='g'")" = "retired" ] \
  || { echo "::error::withdrawing the approval left the model serving under an approval that was taken back"; exit 1; }
[ "$(q "SELECT notes LIKE '%licence approval withdrawn%' FROM model_registry WHERE model_key='g'")" = "t" ] \
  || { echo "::error::the model was retired with no record of why"; exit 1; }
echo "✅ 068 stops a model serving when its approval is withdrawn"

# ── The rollback refuses while something serves under an approval ─
reg "UPDATE model_licence_register SET classification='known_approved' WHERE id='${L}'::uuid"
reg "UPDATE model_registry SET status='serving', rollout_percentage=100 WHERE model_key='g'"
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/068_model_licence_register_down.sql 2>/tmp/r068.err; then
  echo "::error::rollback/068 dropped the register while a model served under a recorded approval, leaving it serving with no record of who approved it"
  exit 1
fi
grep -q "068-down REFUSING" /tmp/r068.err \
  || { echo "::error::rollback/068 failed for the wrong reason:"; cat /tmp/r068.err; exit 1; }
echo "✅ rollback/068 refuses while a model serves under an approval"

reg "UPDATE model_registry SET status='retired' WHERE status='serving'"

# ── A chain reverses in reverse order, and says so ──────────────
#
# This database holds the WHOLE chain. 071 gave
# model_training_runs.base_licence_id a foreign key to the register,
# so 068-down cannot drop it while 071 stands. Before P7-05 this step
# failed here on PostgreSQL's own "other objects depend on it",
# arriving after 068-down's serving guard had already passed.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/068_model_licence_register_down.sql 2>/tmp/o068.err; then
  echo "::error::rollback/068 ran while 071 was applied"
  exit 1
fi
grep -q "068-down OUT OF ORDER" /tmp/o068.err \
  || { echo "::error::rollback/068 failed without naming the files to run first:"; cat /tmp/o068.err; exit 1; }
echo "✅ rollback/068 refuses while a later migration depends on it, by name"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/072_model_promotion_gate_down.sql
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/071_model_training_runs_down.sql
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/068_model_licence_register_down.sql
[ "$(q "SELECT to_regclass('public.model_licence_register') IS NULL")" = "t" ] \
  || { echo "::error::rollback/068 refused but left the table"; exit 1; }
[ "$(q "SELECT count(*) FROM pg_trigger WHERE tgname='model_registry_licence_gate' AND NOT tgisinternal")" = "0" ] \
  || { echo "::error::rollback/068 left the licence gate trigger behind, so every model_registry INSERT now raises"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/068_model_licence_register.sql
[ "$(q "SELECT to_regclass('public.model_licence_register') IS NOT NULL")" = "t" ] \
  || { echo "::error::068 did not re-apply"; exit 1; }
[ "$(q "SELECT count(*) FROM pg_trigger WHERE tgname='model_registry_licence_gate' AND NOT tgisinternal")" = "1" ] \
  || { echo "::error::068 re-applied without its gate trigger"; exit 1; }
echo "✅ 068 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
