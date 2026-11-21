#!/usr/bin/env bash
#
# The owner's determinations apply, and refuse, as recorded
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# The one script in this repository that writes `known_approved`. It is
# a script and not a migration on purpose: a determination is a person's
# statement about ONE environment at ONE time, and a migration would
# replay it into every future database as though they had been there.
# `test_no_migration_records_a_licence_determination` still forbids the
# migration form, and 068 still refuses a determination attributed to a
# process. Neither was weakened.
#
# So a database built from the chain alone must have NO determinations,
# and this step proves that first — the fresh-database state is the
# thing most easily lost by accident.
#
# Then it runs the applier the way a person would, and then it plants.
# The writes are the easy half; the refusals are what makes an approval
# mean something.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: the repository root`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../../.."

DB=iratestdet
PSQL="psql -h localhost -U postgres"
DSN="postgresql://postgres:postgres@localhost:5432/${DB}"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
q() { ${PSQL} -d ${DB} -tAc "$1"; }
for f in supracloud-jarvis/postgres/init.sql supracloud-jarvis/postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f"
done

[ "$(q "SELECT count(*) FROM model_licence_register WHERE classified_by IS NOT NULL")" = "0" ] \
  || { echo "::error::the migration chain alone produced a licence determination"; exit 1; }
echo "✅ a fresh database carries no determination"

# A dry run is a dry run. This is worth asserting rather than trusting:
# the failure is silent and the state it leaves is indistinguishable
# from an intentional apply.
python3 scripts/models/record_determination.py --dsn "${DSN}" --i-am "Praveen Kumar" >/dev/null
[ "$(q "SELECT count(*) FROM model_licence_register WHERE classified_by IS NOT NULL")" = "0" ] \
  || { echo "::error::a dry run wrote to the register"; exit 1; }
echo "✅ a dry run writes nothing"

python3 scripts/models/record_determination.py --dsn "${DSN}" --i-am "Praveen Kumar" --apply >/dev/null
python3 scripts/models/record_determination.py --dsn "${DSN}" --verify
[ "$(q "SELECT count(*) FROM model_licence_register WHERE classification='known_approved'")" = "10" ] \
  || { echo "::error::expected 10 approvals"; exit 1; }
[ "$(q "SELECT count(*) FROM model_licence_register WHERE classification='known_restricted'")" = "1" ] \
  || { echo "::error::expected 1 restriction"; exit 1; }
[ "$(q "SELECT count(*) FROM model_licence_register WHERE classification='review_required'")" = "12" ] \
  || { echo "::error::expected 12 rows still held for review"; exit 1; }
[ "$(q "SELECT count(DISTINCT classified_by) FROM model_licence_register WHERE classified_by IS NOT NULL")" = "1" ] \
  || { echo "::error::more than one person is named on these determinations"; exit 1; }
echo "✅ 10 approved, 1 restricted, 12 held, one named human"

# The owner's instruction, enforced rather than restated. 072 reads
# this field before promoting a fine-tuned model, so leaving it
# `unknown` is what keeps training blocked while serving is allowed.
[ "$(q "SELECT count(*) FROM model_licence_register WHERE derivative_distribution <> 'unknown'")" = "0" ] \
  || { echo "::error::a serving approval authorised a training base"; exit 1; }
echo "✅ no serving approval authorises a training base"

# Gemma, pyannote, the five voice rows 090 seeds and the three 098 seeds are HELD. Held and
# forgotten look identical from a count, so they are named.
for held in "gemma3|4b" "gemma3|12b" "gemma3n|e4b" "pyannote/speaker-diarization-3.1|84fd25912480287da0247647c3d2b4853cb3ee5d" \
            "Supertone/supertonic-3|724fb5abbf5502583fb520898d45929e62f02c0b" \
            "Systran/faster-whisper-base|ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66" \
            "Systran/faster-whisper-small|536b0662742c02347bc0e980a01041f333bce120" \
            "Systran/faster-distil-whisper-large-v3|c3058b475261292e64a0412df1d2681c06260fab" \
            "Systran/faster-whisper-large-v3|edaa852ec7e145841d8ffdb056a99866b5f0a478" \
            "BAAI/bge-large-en-v1.5|d4aa6901d3a41ba39fb536a557fa166f842b0e09" \
            "BAAI/bge-reranker-v2-m3|953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e" \
            "speechbrain/spkrec-ecapa-voxceleb|0f99f2d0ebe89ac095bcc5903c4dd8f72b367286"; do
  id="${held%%|*}"; ref="${held##*|}"
  got=$(q "SELECT classification FROM model_licence_register WHERE identifier='${id}' AND reference='${ref}'")
  [ "${got}" = "review_required" ] \
    || { echo "::error::${id}/${ref} is '${got}', but the owner held it for review"; exit 1; }
done
echo "✅ the three Gemma rows, pyannote, the five voice rows and the three memory/voice-print rows are still held"

[ "$(q "SELECT count(*) FROM model_registry WHERE status='serving'")" = "0" ] \
  || { echo "::error::approving a licence started serving a model — those are separate acts"; exit 1; }
echo "✅ approving a licence did not start serving anything"

# ── The refusals ────────────────────────────────────────────────
refuse() {
  # $1 = expected text, $2 = decision file, $3.. = extra args
  local want="$1"; shift
  local file="$1"; shift
  if python3 scripts/models/record_determination.py --dsn "${DSN}" \
       --i-am "Praveen Kumar" --apply --file "${file}" "$@" 2>/tmp/det.err; then
    echo "::error::the applier accepted ${file}"; exit 1
  fi
  grep -q "${want}" /tmp/det.err \
    || { echo "::error::refused for the wrong reason (wanted ${want}):"; cat /tmp/det.err; exit 1; }
}

python3 - <<'PY'
import json, pathlib
src = json.loads(pathlib.Path("docs/cloud/evidence/OWNER_LICENCE_DETERMINATIONS.json").read_text())
out = pathlib.Path("/tmp/plants"); out.mkdir(exist_ok=True)
d = json.loads(json.dumps(src)); d["decisions"][0]["artifact_digest"] = "sha256:" + "0" * 64
(out / "moved.json").write_text(json.dumps(d))
d = json.loads(json.dumps(src)); d["decisions"][0]["licence_identifier"] = "MIT License"
(out / "terms.json").write_text(json.dumps(d))
d = json.loads(json.dumps(src)); d["deliberately_not_decided"] = []
(out / "unaccounted.json").write_text(json.dumps(d))
d = json.loads(json.dumps(src)); d["classified_by"] = "automation"
(out / "process.json").write_text(json.dumps(d))
# The last decision points at nothing, so a partial apply would leave
# the first ten written and the eleventh not.
d = json.loads(json.dumps(src)); d["decisions"][-1]["reference"] = "no-such-revision"
(out / "partial.json").write_text(json.dumps(d))
PY

refuse "does not travel to it" /tmp/plants/moved.json
refuse "recorded terms changed" /tmp/plants/terms.json
refuse "neither decided nor deliberately held" /tmp/plants/unaccounted.json
echo "✅ a moved digest, changed terms and an unaccounted row are all refused"

if python3 scripts/models/record_determination.py --check --file /tmp/plants/process.json 2>/tmp/det.err; then
  echo "::error::a determination attributed to a process was accepted"; exit 1
fi
grep -q "names a process" /tmp/det.err \
  || { echo "::error::wrong reason:"; cat /tmp/det.err; exit 1; }
if python3 scripts/models/record_determination.py --dsn "${DSN}" --i-am "Somebody Else" --apply 2>/tmp/det.err; then
  echo "::error::somebody recorded a determination in another person's name"; exit 1
fi
grep -q "recorded by the person who made it" /tmp/det.err \
  || { echo "::error::wrong reason:"; cat /tmp/det.err; exit 1; }
echo "✅ a process name and a second person are both refused"

# ── Atomicity, on a database with nothing applied yet ────────────
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}2"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}2"
for f in supracloud-jarvis/postgres/init.sql supracloud-jarvis/postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB}2 -v ON_ERROR_STOP=1 -q -f "$f"
done
if python3 scripts/models/record_determination.py \
     --dsn "postgresql://postgres:postgres@localhost:5432/${DB}2" \
     --i-am "Praveen Kumar" --apply --file /tmp/plants/partial.json 2>/dev/null; then
  echo "::error::the applier accepted a decision naming a row that does not exist"; exit 1
fi
left=$(${PSQL} -d ${DB}2 -tAc "SELECT count(*) FROM model_licence_register WHERE classified_by IS NOT NULL")
[ "${left}" = "0" ] \
  || { echo "::error::${left} determination(s) survived a refused run — a half-applied set of readings is a register nobody can trust"; exit 1; }
echo "✅ a refused run leaves no determination behind"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}2"
