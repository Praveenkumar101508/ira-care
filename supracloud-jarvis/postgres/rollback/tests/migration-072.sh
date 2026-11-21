#!/usr/bin/env bash
#
# Migration 072 will not let a string become a model
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# Same shape as 054 through 071, on its own disposable database.
#
# `067` created `model_registry.adapter_version` as free text and said
# plainly that nothing populates it. Left there, registering "an IRA
# model" is TYPING A STRING — the same shape as Phase 6's finding that
# `commercial_licence` read like a control and was satisfied by 'x'.
#
# The load-bearing assertion here is the digest one: a registry row must
# carry the adapter digest the run actually emitted. Without it the row
# may point at any artefact while naming a run that produced a different
# one, and both rows read as internally consistent.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest072
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
q() { ${PSQL} -d ${DB} -tAc "$1"; }
reg() { ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "$1"; }
refuse() {
  if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "$2" 2>/tmp/c072.err; then
    echo "::error::$3"; exit 1
  fi
  grep -q "$1" /tmp/c072.err \
    || { echo "::error::refused for the wrong reason (wanted $1):"; cat /tmp/c072.err; exit 1; }
}
for f in postgres/init.sql postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f"
done

ADAPTER=sha256:$(printf 'e%.0s' $(seq 1 64))
OTHER=sha256:$(printf '7%.0s' $(seq 1 64))
CARD=sha256:$(printf 'd%.0s' $(seq 1 64))
CONFIG=sha256:$(printf 'c%.0s' $(seq 1 64))
ART=sha256:$(printf 'f%.0s' $(seq 1 64))
DATA=sha256:$(printf 'a%.0s' $(seq 1 64))
REV=$(printf 'd%.0s' $(seq 1 40))
WHO='CI FIXTURE — not a determination'

[ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='model_registry' AND column_name IN ('training_run_id','model_card_sha256')")" = "2" ] \
  || { echo "::error::072 did not add its columns"; exit 1; }
[ "$(q "SELECT count(*) FROM pg_trigger WHERE tgname='model_registry_promotion_gate' AND NOT tgisinternal")" = "1" ] \
  || { echo "::error::072 did not create its gate"; exit 1; }
for p in INSERT UPDATE DELETE; do
  [ "$(q "SELECT has_table_privilege('ira_app','model_registry','$p')")" = "f" ] \
    || { echo "::error::ira_app holds $p on model_registry after 072"; exit 1; }
done
echo "✅ 072 adds the provenance columns and the gate, and widens nothing"

# A base model registered from upstream is untouched: Phase 5 allows
# it straight into production, and Phase 7 does not change Phase 5's
# contract as a side effect.
reg "INSERT INTO model_registry (model_key,version,base_model,revision,artifact_hash,environment) VALUES ('ci-base',1,'ollama/fixture:1b','${REV}','${OTHER}','production')"
echo "✅ 072 leaves a base-model registration alone"

refuse "an_ira_model_names_the_run_that_made_it" \
  "INSERT INTO model_registry (model_key,version,base_model,revision,artifact_hash,environment,adapter_version) VALUES ('ci-typed',1,'ollama/fixture:1b','${REV}','${ADAPTER}','development','v1')" \
  "adapter_version was typed with no run behind it — a string that looks exactly like a fine-tuned model"
echo "✅ 072 refuses an adapter_version that names no run"

# A completed run, built through the real 070 and 071 gates.
reg "INSERT INTO model_licence_register (source,identifier,reference,licence_identifier,artifact_digest,evidence_url,evidence_method,evidence_retrieved_at,classification) VALUES ('ollama','ci72','ref','fixture','${ART}','https://example.invalid/x','owner-supplied',NOW(),'review_required')"
L=$(q "SELECT id FROM model_licence_register WHERE identifier='ci72'")
reg "UPDATE model_licence_register SET classification='known_approved', classified_by='${WHO}', classified_at=NOW(), determination_note='CI fixture', commercial_use='permitted', hosted_saas_redistribution='permitted', derivative_distribution='permitted' WHERE id='${L}'::uuid"
reg "INSERT INTO training_datasets (name,version,source,content_sha256,record_count) VALUES ('ci72ds',1,'ci fixture','${DATA}',10)"
D=$(q "SELECT id FROM training_datasets WHERE name='ci72ds'")
for k in pii secret contamination licence; do
  reg "INSERT INTO dataset_checks (dataset_id,check_kind,content_sha256,outcome,tool,tool_version) VALUES ('${D}'::uuid,'$k','${DATA}','passed','ci','1')"
done
reg "UPDATE training_datasets SET licence_classification='known_approved', licence_classified_by='${WHO}', licence_classified_at=NOW(), lawful_basis='CI fixture only.', lawful_basis_by='${WHO}', lawful_basis_at=NOW(), contains_personal_data='no', state='train' WHERE id='${D}'::uuid"
reg "INSERT INTO model_training_runs (run_key,base_licence_id,base_model,base_revision,method,config_path,config_sha256) VALUES ('ci72',(SELECT id FROM model_licence_register WHERE identifier='ci72'),'ollama/fixture:1b','${REV}','lora','model-platform/configs/x.toml','${CONFIG}')"
R=$(q "SELECT id FROM model_training_runs WHERE run_key='ci72'")
reg "INSERT INTO training_run_datasets (run_id,dataset_id,role,bound_sha256) VALUES ('${R}'::uuid,'${D}'::uuid,'train','${DATA}')"
reg "UPDATE model_training_runs SET state='approved', approved_by='${WHO}', approved_at=NOW(), approval_note='CI fixture' WHERE id='${R}'::uuid"

refuse "not completed" \
  "INSERT INTO model_registry (model_key,version,base_model,revision,artifact_hash,environment,adapter_version,training_run_id,model_card_sha256) VALUES ('ci-early',1,'ollama/fixture:1b','${REV}','${ADAPTER}','development','v1','${R}'::uuid,'${CARD}')" \
  "an adapter was registered from a run that had not completed"
echo "✅ 072 refuses to register from a run that produced nothing"

reg "UPDATE model_training_runs SET state='completed', adapter_sha256='${ADAPTER}', adapter_path='adapters/ci72', finished_at=NOW() WHERE id='${R}'::uuid"

refuse "claims artefact" \
  "INSERT INTO model_registry (model_key,version,base_model,revision,artifact_hash,environment,adapter_version,training_run_id,model_card_sha256) VALUES ('ci-wrong',1,'ollama/fixture:1b','${REV}','${OTHER}','development','v1','${R}'::uuid,'${CARD}')" \
  "the registry pointed at an artefact the run did not emit, while naming that run"
echo "✅ 072 refuses a registry row that points at different bytes"

refuse "records base" \
  "INSERT INTO model_registry (model_key,version,base_model,revision,artifact_hash,environment,adapter_version,training_run_id,model_card_sha256) VALUES ('ci-basemismatch',1,'ollama/other:1b','${REV}','${ADAPTER}','development','v1','${R}'::uuid,'${CARD}')" \
  "the registry and the run disagreed about the base model, and both rows looked consistent"
echo "✅ 072 refuses a registry row that disagrees about the base"

refuse "an_ira_model_carries_its_card" \
  "INSERT INTO model_registry (model_key,version,base_model,revision,artifact_hash,environment,adapter_version,training_run_id) VALUES ('ci-nocard',1,'ollama/fixture:1b','${REV}','${ADAPTER}','development','v1','${R}'::uuid)" \
  "an IRA model was registered with no model card"
echo "✅ 072 refuses an IRA model with no card"

refuse "cannot enter staging" \
  "INSERT INTO model_registry (model_key,version,base_model,revision,artifact_hash,environment,adapter_version,training_run_id,model_card_sha256) VALUES ('ci-ladder',1,'ollama/fixture:1b','${REV}','${ADAPTER}','staging','v1','${R}'::uuid,'${CARD}')" \
  "an adapter entered staging without ever being registered in development"
echo "✅ 072 refuses a rung skipped"

reg "INSERT INTO model_registry (model_key,version,base_model,revision,artifact_hash,environment,adapter_version,training_run_id,model_card_sha256) VALUES ('ci-ladder',1,'ollama/fixture:1b','${REV}','${ADAPTER}','development','v1','${R}'::uuid,'${CARD}')"
reg "INSERT INTO model_registry (model_key,version,base_model,revision,artifact_hash,environment,adapter_version,training_run_id,model_card_sha256) VALUES ('ci-ladder',2,'ollama/fixture:1b','${REV}','${ADAPTER}','staging','v1','${R}'::uuid,'${CARD}')"
reg "INSERT INTO model_registry (model_key,version,base_model,revision,artifact_hash,environment,adapter_version,training_run_id,model_card_sha256) VALUES ('ci-ladder',3,'ollama/fixture:1b','${REV}','${ADAPTER}','production','v1','${R}'::uuid,'${CARD}')"
[ "$(q "SELECT count(*) FROM model_registry WHERE model_key='ci-ladder'")" = "3" ] \
  || { echo "::error::a fully governed adapter could not climb the ladder — the gate refuses everything"; exit 1; }
echo "✅ 072 promotes a fully governed adapter, one environment at a time"

# Permission expires. A withdrawal after registration must stop the NEXT one.
reg "UPDATE model_licence_register SET derivative_distribution='restricted' WHERE id='${L}'::uuid"
refuse "derivative distribution" \
  "INSERT INTO model_registry (model_key,version,base_model,revision,artifact_hash,environment,adapter_version,training_run_id,model_card_sha256) VALUES ('ci-withdrawn',1,'ollama/fixture:1b','${REV}','${ADAPTER}','development','v1','${R}'::uuid,'${CARD}')" \
  "an adapter was registered after its base licence stopped permitting derivatives"
echo "✅ 072 refuses a registration once the derivative permission is withdrawn"

# ── The rollback refuses to delete the provenance ────────────────
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/072_model_promotion_gate_down.sql 2>/tmp/r072.err; then
  echo "::error::rollback/072 deleted the link between a served artefact and the run behind it"
  exit 1
fi
grep -q "072-down REFUSING" /tmp/r072.err \
  || { echo "::error::rollback/072 failed for the wrong reason:"; cat /tmp/r072.err; exit 1; }
echo "✅ rollback/072 refuses to discard a registry row's provenance"

reg "UPDATE model_registry SET status='retired', training_run_id=NULL, adapter_version=NULL, model_card_sha256=NULL WHERE training_run_id IS NOT NULL"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/072_model_promotion_gate_down.sql
[ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='model_registry' AND column_name='training_run_id'")" = "0" ] \
  || { echo "::error::rollback/072 refused but left the column"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/072_model_promotion_gate.sql
[ "$(q "SELECT count(*) FROM pg_trigger WHERE tgname='model_registry_promotion_gate' AND NOT tgisinternal")" = "1" ] \
  || { echo "::error::072 re-applied without its gate"; exit 1; }
echo "✅ 072 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
