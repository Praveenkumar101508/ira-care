#!/usr/bin/env bash
#
# Migration 067 pins what may serve and what may be fetched
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# Same shape as 054 through 066. On its own disposable database, so a
# destructive rollback test cannot reach the development one.
#
# Three properties, each proved by a REFUSAL rather than by a success:
# an unpinned revision, an unlicensed serving row, and a rollback while
# something is serving. MOD-02 exists because none of these was refused
# before.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest067
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
q() { ${PSQL} -d ${DB} -tAc "$1"; }
for f in postgres/init.sql postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f"
done

H=sha256:$(printf 'x%.0s' $(seq 1 64) | tr 'x' 'a')
D=sha256:$(printf 'x%.0s' $(seq 1 64) | tr 'x' 'b')
reg() { ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "$1"; }

# 068 is in the chain this step applies, so `status='serving'` now
# requires a model_licence_register row classified known_approved.
# Without this, every assertion below would fail on the licence gate
# instead of on the 067 constraint it is named for — the step would
# still be red, and it would be red for the wrong reason, which is
# worse than green.
#
# Two statements, mirroring how a real approval happens: the INSERT
# lands in a state engineering may write, the UPDATE is the human act.
# The names say FIXTURE in every column a reader sees; no real
# model's terms are asserted anywhere in this repository.
reg "INSERT INTO model_licence_register (source,identifier,reference,licence_identifier,artifact_digest,evidence_url,evidence_method,evidence_retrieved_at,classification) VALUES ('ollama','ci/fixture','k','ci-fixture-licence','${D}','https://example.invalid/ci','owner-supplied',NOW(),'review_required')"
reg "UPDATE model_licence_register SET classification='known_approved', classified_by='CI FIXTURE — not a licence determination', classified_at=NOW(), determination_note='CI fixture only. No licence text was read and no determination was made.', commercial_use='permitted', hosted_saas_redistribution='permitted' WHERE identifier='ci/fixture'"
L=$(q "SELECT id FROM model_licence_register WHERE identifier='ci/fixture'")

[ "$(q "SELECT to_regclass('public.model_registry') IS NOT NULL")" = "t" ] \
  || { echo "::error::067 did not create model_registry"; exit 1; }
[ "$(q "SELECT to_regclass('public.model_pull_allowlist') IS NOT NULL")" = "t" ] \
  || { echo "::error::067 did not create model_pull_allowlist"; exit 1; }

# NOT the grant check. This database is built by `postgres`, so its
# tables are owned by `postgres` and the ALTER DEFAULT PRIVILEGES that
# `042` attaches to `ira_schema_owner` never fires here. A
# has_table_privilege assertion in this step would therefore report
# "ira_app cannot INSERT" on every possible version of 067 — including
# the first one, which granted SELECT and revoked nothing and left
# `ira_app` able to widen its own allowlist in a real deployment.
#
# A check that cannot observe the failure is not a check. The grants
# are asserted where the ownership is real: the recompose step below,
# and tests/tenancy/test_p5_model_registry_schema.py, both of which run
# against a database the MIGRATOR built.

# MOD-02: a revision that does not pin is refused.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "INSERT INTO model_registry (model_key,version,base_model,revision,artifact_hash,environment) VALUES ('k',1,'org/m','latest','${H}','development')" 2>/tmp/c067a.err; then
  echo "::error::model_registry accepted revision='latest', which is not a pin — MOD-02 is the finding that this must not happen"
  exit 1
fi
grep -q "revision_is_actually_pinned" /tmp/c067a.err \
  || { echo "::error::the unpinned revision failed for the wrong reason:"; cat /tmp/c067a.err; exit 1; }
echo "✅ 067 refuses an unpinned revision"

# A hash that is not a sha256 verifies nothing while looking present.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "INSERT INTO model_registry (model_key,version,base_model,revision,artifact_hash,environment) VALUES ('k',1,'org/m','abc123','not-a-hash','development')" 2>/tmp/c067b.err; then
  echo "::error::model_registry accepted a malformed artifact_hash"
  exit 1
fi
grep -q "artifact_hash_is_a_sha256" /tmp/c067b.err \
  || { echo "::error::the malformed hash failed for the wrong reason:"; cat /tmp/c067b.err; exit 1; }
echo "✅ 067 refuses a malformed artefact hash"

reg "INSERT INTO model_registry (model_key,version,base_model,revision,artifact_hash,environment) VALUES ('k',1,'org/m','abc123','${H}','development')"

# MOD-01 is Phase 6 and BLOCKING there. The registry does not decide a
# licence; it refuses to let an unlicensed version serve, so Phase 6
# is a gate rather than a note.
# licence_id and rollout are supplied so that 067's serving_is_licensed
# is the constraint that fires. 068's BEFORE trigger runs ahead of the
# CHECK constraints, so a row with no licence_id fails on the gate and
# this assertion would stop testing 067 while still passing.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "UPDATE model_registry SET status='serving', rollout_percentage=100, licence_id='${L}'::uuid WHERE model_key='k'" 2>/tmp/c067c.err; then
  echo "::error::a model with no recorded commercial licence was set serving — MOD-01 blocks Phase 6 precisely because this must not be possible"
  exit 1
fi
grep -q "serving_is_licensed" /tmp/c067c.err \
  || { echo "::error::the unlicensed serving update failed for the wrong reason:"; cat /tmp/c067c.err; exit 1; }
echo "✅ 067 refuses to serve a model whose licence is unrecorded"

# A serving row must reach somebody. rollout_percentage defaults to 0,
# so "set it serving" alone produces a model that serves NOBODY,
# silently — which is what an operator does while rolling back.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "UPDATE model_registry SET status='serving', commercial_licence='ci-fixture-not-a-real-determination', licence_recorded_at=NOW(), licence_id='${L}'::uuid WHERE model_key='k'" 2>/tmp/c067e.err; then
  echo "::error::a model was set serving at 0% rollout, so it serves nobody while the registry looks correct"
  exit 1
fi
grep -q "serving_reaches_somebody" /tmp/c067e.err \
  || { echo "::error::the zero-rollout serving update failed for the wrong reason:"; cat /tmp/c067e.err; exit 1; }
echo "✅ 067 refuses a serving model that reaches nobody"

# Now licence it and serve it, so the rollback guard has something to
# refuse over. This records a licence for a CI fixture row only; no
# real model's terms are asserted anywhere in this repository.
reg "UPDATE model_registry SET commercial_licence='ci-fixture-not-a-real-determination', licence_recorded_at=NOW(), status='serving', rollout_percentage=100, licence_id='${L}'::uuid WHERE model_key='k'"
[ "$(q "SELECT count(*) FROM model_registry WHERE status='serving'")" = "1" ] \
  || { echo "::error::the licensed row did not become serving"; exit 1; }

# Exactly one serving version per key and environment. Two makes
# "which model answered this request" unanswerable.
reg "INSERT INTO model_registry (model_key,version,base_model,revision,artifact_hash,environment,commercial_licence,licence_recorded_at) VALUES ('k',2,'org/m','def456','${H}','development','ci-fixture-not-a-real-determination',NOW())"
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "UPDATE model_registry SET status='serving', rollout_percentage=100, licence_id='${L}'::uuid WHERE model_key='k' AND version=2" 2>/tmp/c067d.err; then
  echo "::error::two versions of the same model are serving in one environment"
  exit 1
fi
grep -q "model_registry_one_serving_version" /tmp/c067d.err \
  || { echo "::error::the second serving row failed for the wrong reason:"; cat /tmp/c067d.err; exit 1; }
echo "✅ 067 permits exactly one serving version per model and environment"

# The rollback refuses while something is serving.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/067_model_registry_down.sql 2>/tmp/r067.err; then
  echo "::error::rollback/067 dropped the registry while a model was serving, destroying the record of what is running and what to roll back to"
  exit 1
fi
grep -q "067-down REFUSING" /tmp/r067.err \
  || { echo "::error::rollback/067 failed for the wrong reason:"; cat /tmp/r067.err; exit 1; }
echo "✅ rollback/067 refuses while a model is serving"

reg "UPDATE model_registry SET status='retired' WHERE status='serving'"
# Newest first, all the way down. 072 alters model_registry and
# references model_training_runs; 071 references the licence register;
# 068 hangs a column, a foreign key and a trigger off model_registry.
# Each of those rollbacks now refuses by name if run out of turn, so
# this order is asserted by the scripts themselves as well as here.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/072_model_promotion_gate_down.sql
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/071_model_training_runs_down.sql
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/068_model_licence_register_down.sql
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/067_model_registry_down.sql
[ "$(q "SELECT to_regclass('public.model_registry') IS NULL")" = "t" ] \
  || { echo "::error::rollback/067 refused but left the table"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/067_model_registry.sql
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/068_model_licence_register.sql
[ "$(q "SELECT to_regclass('public.model_registry') IS NOT NULL")" = "t" ] \
  || { echo "::error::067 did not re-apply"; exit 1; }
echo "✅ 067 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
