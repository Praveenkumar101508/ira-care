#!/usr/bin/env bash
#
# Migration 088 gives the Technical Library an index for keyword search
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# Three properties, and the THIRD is the one a careless edit would break:
#
# 1. the index exists after 088 with exactly the expression and predicate the
#    retrieval query in memory/technical_library.py writes;
# 2. the planner USES it for that query. An index whose expression differs by
#    one token from the query's is built, maintained, and never read — which
#    looks exactly like success in every check that only asks whether it exists;
# 3. it is PARTIAL: a personal memory row is not in it. A full index would make
#    every chat message's embedding write pay for to_tsvector, which is the cost
#    088's header says this design exists to avoid.
#
# And the rollback is proved to be a clean reverse: it removes only the index,
# every row survives, and 088 re-applies over the same data.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest088
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE ${DB}"
# Everything UP TO 088, so the table is in its pre-088 shape.
for f in postgres/init.sql postgres/0[0-9][0-9]_*.sql; do
  case "$f" in *088_*) continue ;; esac
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f" >/dev/null
done

q() { ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -tAc "$1"; }

TENANT=e91aceeb-89fb-5ad2-b43c-4eee2bad079c
WORKSPACE=$(q "SELECT id FROM workspaces WHERE tenant_id = '${TENANT}' AND is_default")
[ -n "${WORKSPACE}" ] \
  || { echo "::error::the seeded private tenant has no default workspace; there is nowhere to put a row"; exit 1; }

row() {   # $1 = user_id, $2 = content
  q "INSERT INTO memory_embeddings
         (source_id, source_type, content, embedding, user_id, tenant_id, workspace_id)
     VALUES (gen_random_uuid(), '$1', '$2',
             array_fill(0.0::real, ARRAY[1024])::vector, '$1',
             '${TENANT}', '${WORKSPACE}')" >/dev/null
}
row technical_library 'GROUP BY collapses rows into one summary row per group'
row owner 'remember to buy milk'
# Two thousand library rows that do NOT match, so the plan reflects a real
# library — where the term is rare — rather than a two-row table that is
# cheaper to scan than to probe by any index at all.
q "INSERT INTO memory_embeddings
       (source_id, source_type, content, embedding, user_id, tenant_id, workspace_id)
   SELECT gen_random_uuid(), 'technical_library', 'attention weights value vectors ' || n,
          array_fill(0.0::real, ARRAY[1024])::vector, 'technical_library',
          '${TENANT}', '${WORKSPACE}'
     FROM generate_series(1, 2000) AS n" >/dev/null

# The exact keyword query the library runs, minus its vector column.
KEYWORD_QUERY="SELECT id FROM memory_embeddings, websearch_to_tsquery('english', 'group by') AS q
 WHERE user_id = 'technical_library' AND source_type = 'technical_library'
   AND to_tsvector('english', content) @@ q"

# 0. The negative control: before 088 there is no such index.
[ "$(q "SELECT count(*) FROM pg_indexes WHERE indexname = 'idx_memory_embeddings_techlib_fts'")" = "0" ] \
  || { echo "::error::idx_memory_embeddings_techlib_fts exists BEFORE 088; this step is testing nothing"; exit 1; }
echo "✅ EXPECTED / PASS — no full-text index before 088"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/088_technical_library_fulltext.sql
q "ANALYZE memory_embeddings" >/dev/null

# 1. Exactly the expression and predicate the query writes.
DEF=$(q "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_memory_embeddings_techlib_fts'")
EXPECTED="CREATE INDEX idx_memory_embeddings_techlib_fts ON public.memory_embeddings USING gin (to_tsvector('english'::regconfig, content)) WHERE (user_id = 'technical_library'::text)"
[ "${DEF}" = "${EXPECTED}" ] \
  || { echo "::error::088's index definition drifted:"; echo "  got:      ${DEF}"; echo "  expected: ${EXPECTED}"; exit 1; }
echo "✅ PASS — the index matches the query's expression and predicate"

# 2. The planner uses it — on the data above it is the cheapest path. Sequential
#    scans are also disabled, so a cost-model change cannot turn this green by
#    scanning.
PLAN=$(${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -tA \
  -c "SET enable_seqscan = off" -c "EXPLAIN ${KEYWORD_QUERY}")
echo "${PLAN}" | grep -q "idx_memory_embeddings_techlib_fts" \
  || { echo "::error::the library's keyword query does not use the index:"; echo "${PLAN}"; exit 1; }
[ "$(q "${KEYWORD_QUERY}" | wc -l)" = "1" ] \
  || { echo "::error::the keyword query did not find the one library row"; exit 1; }
echo "✅ PASS — the keyword query uses the index and finds the row"

# 3. Partial: a personal row is not indexed. `pgstattuple` is not assumed, so
#    this asks the planner instead — a query for the owner's row cannot be
#    answered from an index whose predicate excludes it.
OWNER_PLAN=$(${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -tA -c "SET enable_seqscan = off" \
  -c "EXPLAIN SELECT id FROM memory_embeddings WHERE user_id = 'owner'
        AND to_tsvector('english', content) @@ websearch_to_tsquery('english', 'milk')")
if echo "${OWNER_PLAN}" | grep -q "idx_memory_embeddings_techlib_fts"; then
  echo "::error::a personal-memory query used the library index — it is not partial"; exit 1
fi
echo "✅ PASS — personal memory is outside the index"

# 4. Rollback removes the index and nothing else; 088 re-applies over the data.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/088_technical_library_fulltext_down.sql
[ "$(q "SELECT count(*) FROM pg_indexes WHERE indexname = 'idx_memory_embeddings_techlib_fts'")" = "0" ] \
  || { echo "::error::rollback/088 left the index in place"; exit 1; }
[ "$(q "SELECT count(*) FROM memory_embeddings")" = "2002" ] \
  || { echo "::error::rollback/088 changed the rows in memory_embeddings"; exit 1; }
[ "$(q "${KEYWORD_QUERY}" | wc -l)" = "1" ] \
  || { echo "::error::without the index the keyword query should still find the row"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/088_technical_library_fulltext.sql
[ "$(q "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_memory_embeddings_techlib_fts'")" = "${EXPECTED}" ] \
  || { echo "::error::088 did not re-apply cleanly after its rollback"; exit 1; }
echo "✅ PASS — rollback is a clean reverse and 088 re-applies"

${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE IF EXISTS ${DB}"
echo "✅ migration 088 verified"
