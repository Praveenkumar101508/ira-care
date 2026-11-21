#!/usr/bin/env bash
#
# Migration 071 will not let a training run start
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# Same shape as 054 through 070, on its own disposable database.
#
# CLOUD_PHASE_STATUS row 7: "needs owner approval before any training".
# COST_AND_CAPACITY_REGISTER: "Renting GPU capacity | 7", "Starting
# expensive model training | 7".
#
# The assertion that matters most here is that a licence approved for
# SERVING is not thereby approved for TRAINING. MODEL_LICENSE_REGISTER
# asks the second question separately — "Whether fine-tuning and
# distributing a derivative is permitted (Phase 7)" — and the intuition
# that an approved licence is an approved licence is exactly the mistake
# that would let IRA fine-tune weights whose terms forbid derivatives.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest071
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
q() { ${PSQL} -d ${DB} -tAc "$1"; }
reg() { ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "$1"; }
refuse() {
  if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "$2" 2>/tmp/c071.err; then
    echo "::error::$3"; exit 1
  fi
  grep -q "$1" /tmp/c071.err \
    || { echo "::error::refused for the wrong reason (wanted $1):"; cat /tmp/c071.err; exit 1; }
}
for f in postgres/init.sql postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f"
done

A=sha256:$(printf 'x%.0s' $(seq 1 64) | tr 'x' 'a')
C=sha256:$(printf 'x%.0s' $(seq 1 64) | tr 'x' 'c')
E=sha256:$(printf 'x%.0s' $(seq 1 64) | tr 'x' 'e')
REV=$(printf 'd%.0s' $(seq 1 40))
WHO='CI FIXTURE — not an approval'

for tbl in model_training_runs training_run_datasets; do
  [ "$(q "SELECT to_regclass('public.${tbl}') IS NOT NULL")" = "t" ] \
    || { echo "::error::071 did not create ${tbl}"; exit 1; }
  for p in INSERT UPDATE DELETE; do
    [ "$(q "SELECT has_table_privilege('ira_app','${tbl}','$p')")" = "f" ] \
      || { echo "::error::ira_app holds $p on ${tbl} — a request-handling process could approve its own training run"; exit 1; }
  done
done
echo "✅ 071 creates the run tables and the runtime role cannot write them"

# Every seeded licence is review_required with derivative_distribution
# unknown — that is 069, and it is why nothing can train today.
[ "$(q "SELECT count(*) FROM model_licence_register WHERE derivative_distribution <> 'unknown'")" = "0" ] \
  || { echo "::error::a seeded licence permits derivatives; L-01/L-02 are owner decisions"; exit 1; }
echo "✅ no seeded licence permits a derivative — L-01/L-02 blocking"

L=$(q "SELECT id FROM model_licence_register WHERE identifier='qwen3' AND reference='14b'")
reg "INSERT INTO model_training_runs (run_key,base_licence_id,base_model,base_revision,method,config_path,config_sha256) VALUES ('ci71','${L}'::uuid,'qwen3','${REV}','lora','model-platform/configs/ci.yaml','${C}')"
R=$(q "SELECT id FROM model_training_runs WHERE run_key='ci71'")

refuse "licence is review_required" \
  "UPDATE model_training_runs SET state='approved', approved_by='${WHO}', approved_at=NOW(), approval_note='ci' WHERE id='${R}'::uuid" \
  "a run was approved against a base model whose licence nobody has read"
echo "✅ 071 refuses a base model with no approved licence"

# Approve the licence for SERVING only. Training must still refuse.
reg "UPDATE model_licence_register SET classification='known_approved', classified_by='${WHO}', classified_at=NOW(), determination_note='CI fixture only.', commercial_use='permitted', hosted_saas_redistribution='permitted' WHERE id='${L}'::uuid"
refuse "derivative distribution is unknown" \
  "UPDATE model_training_runs SET state='approved', approved_by='${WHO}', approved_at=NOW(), approval_note='ci' WHERE id='${R}'::uuid" \
  "a licence approved for SERVING was treated as approval to fine-tune — MODEL_LICENSE_REGISTER asks the two questions separately"
echo "✅ 071 refuses to train on a licence approved only for serving"

reg "UPDATE model_licence_register SET derivative_distribution='permitted' WHERE id='${L}'::uuid"
refuse "no training dataset is bound" \
  "UPDATE model_training_runs SET state='approved', approved_by='${WHO}', approved_at=NOW(), approval_note='ci' WHERE id='${R}'::uuid" \
  "a run with no data was approved — its provenance would be empty"
echo "✅ 071 refuses a run with no training dataset"

# A governed dataset, promoted through 070's real gate.
reg "INSERT INTO training_datasets (name,version,source,content_sha256,record_count) VALUES ('ci71ds',1,'ci fixture','${A}',10)"
D=$(q "SELECT id FROM training_datasets WHERE name='ci71ds'")
for k in pii secret contamination licence; do
  reg "INSERT INTO dataset_checks (dataset_id,check_kind,content_sha256,outcome,tool,tool_version) VALUES ('${D}'::uuid,'$k','${A}','passed','ci','1')"
done
reg "UPDATE training_datasets SET licence_classification='known_approved', licence_classified_by='${WHO}', licence_classified_at=NOW(), lawful_basis='CI fixture only.', lawful_basis_by='${WHO}', lawful_basis_at=NOW(), contains_personal_data='no' WHERE id='${D}'::uuid"
reg "UPDATE training_datasets SET state='train' WHERE id='${D}'::uuid"
reg "INSERT INTO training_run_datasets (run_id,dataset_id,role,bound_sha256) VALUES ('${R}'::uuid,'${D}'::uuid,'train','${A}')"

refuse "no owner approval is recorded" \
  "UPDATE model_training_runs SET state='approved' WHERE id='${R}'::uuid" \
  "a run reached approved with nobody's approval — PP-04"
for who in migration automation system script engineering ci pipeline operator postgres tbd; do
  refuse "training_approval_is_attributed" \
    "UPDATE model_training_runs SET state='approved', approved_by='${who}', approved_at=NOW(), approval_note='x' WHERE id='${R}'::uuid" \
    "a run approved by '${who}' was accepted"
done
echo "✅ 071 refuses a run with no owner approval, or one attributed to a process"

reg "UPDATE model_training_runs SET state='approved', approved_by='${WHO}', approved_at=NOW(), approval_note='CI fixture only.' WHERE id='${R}'::uuid"
[ "$(q "SELECT state FROM model_training_runs WHERE run_key='ci71'")" = "approved" ] \
  || { echo "::error::a fully permitted run could not be approved — the gate refuses everything"; exit 1; }
echo "✅ 071 approves a run when all three conditions hold"

refuse "its datasets cannot change" \
  "INSERT INTO training_run_datasets (run_id,dataset_id,role,bound_sha256) VALUES ('${R}'::uuid,'${D}'::uuid,'validation','${A}')" \
  "an approved run took on data nobody approved"
echo "✅ 071 freezes a run's datasets once it leaves draft"

refuse "a_completed_run_emitted_an_adapter" \
  "UPDATE model_training_runs SET state='completed', finished_at=NOW() WHERE id='${R}'::uuid" \
  "a run claimed completion with no adapter behind it"
echo "✅ 071 refuses a completed run with no adapter"

# ── The rollback refuses to discard an approval ──────────────────
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/071_model_training_runs_down.sql 2>/tmp/r071.err; then
  echo "::error::rollback/071 discarded an owner approval"
  exit 1
fi
grep -q "071-down REFUSING" /tmp/r071.err \
  || { echo "::error::rollback/071 failed for the wrong reason:"; cat /tmp/r071.err; exit 1; }
echo "✅ rollback/071 refuses to discard an owner approval"

reg "UPDATE model_training_runs SET state='cancelled', approved_by=NULL, approved_at=NULL"

# ── A chain reverses in reverse order, and says so ──────────────
#
# 072 hangs model_registry.training_run_id off this table, so 071-down
# cannot drop it while 072 stands.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/071_model_training_runs_down.sql 2>/tmp/o071.err; then
  echo "::error::rollback/071 ran while 072 was applied"
  exit 1
fi
grep -q "071-down OUT OF ORDER" /tmp/o071.err \
  || { echo "::error::rollback/071 failed without naming the file to run first:"; cat /tmp/o071.err; exit 1; }
echo "✅ rollback/071 refuses while a later migration depends on it, by name"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/072_model_promotion_gate_down.sql
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/071_model_training_runs_down.sql
[ "$(q "SELECT to_regclass('public.model_training_runs') IS NULL")" = "t" ] \
  || { echo "::error::rollback/071 refused but left the table"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/071_model_training_runs.sql
[ "$(q "SELECT count(*) FROM pg_trigger WHERE tgname='model_training_run_gate_trigger' AND NOT tgisinternal")" = "1" ] \
  || { echo "::error::071 re-applied without its gate"; exit 1; }
echo "✅ 071 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
