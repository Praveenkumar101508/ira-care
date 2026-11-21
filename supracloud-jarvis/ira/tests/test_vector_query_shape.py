"""No query may order `memory_embeddings` by distance straight off the table.

`ORDER BY embedding <=> $1 LIMIT n` directly on `memory_embeddings` lets the
planner answer it from `idx_memory_embedding_hnsw`, which covers every tenant,
workspace and namespace. HNSW yields about `hnsw.ef_search` rows of the WHOLE
table and RLS and the namespace filter run afterwards, so on a crowded table
the caller gets back nothing at all. The full argument, the measurements and
the DB-backed proof are in `tests/tenancy/test_vector_recall_crowded_table.py`.

Every vector query over the table therefore computes the distance inside a
`MATERIALIZED` CTE and orders by that column outside it. The index can serve
neither half, so the search is exact over the caller's own rows. This file is
the cheap half of the guard: it runs without a database, in the main suite,
and names the line that reintroduced the shape. The tenancy test is the half
that proves the shape is safe.
"""
from __future__ import annotations

import ast
import pathlib
import re

IRA_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: The forbidden shape: the ORDER BY is the raw distance expression on the
#: table's own column (optionally through a table alias), which is exactly
#: what an ANN index's ordering operator matches.
_RAW_DISTANCE_ORDER = re.compile(r"ORDER\s+BY\s+(?:\w+\.)?embedding\s*<=>", re.I)

#: Every module known to run a vector query over `memory_embeddings`. The scan
#: must find each of them, or it is not reading what it claims to guard.
KNOWN_VECTOR_CALLERS = {
    "memory/store.py",
    "memory/self_knowledge.py",
    "memory/technical_library.py",
    "memory/temporal.py",
}


def _strings(source: str):
    """Every string literal in `source`, f-strings with their holes blanked.

    `memory/temporal.py` and `lifeos/privacy.py` build their SQL as f-strings,
    so plain constants alone would miss two of the six callers. An f-string's
    literal parts are yielded once, as part of the f-string, not again alone.
    """
    nodes = list(ast.walk(ast.parse(source)))
    parts = {id(v) for n in nodes if isinstance(n, ast.JoinedStr) for v in n.values}
    for node in nodes:
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in parts):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            yield node.lineno, "".join(
                str(v.value) if isinstance(v, ast.Constant) else "{}" for v in node.values)


def _vector_queries():
    """(module, line, sql) for each literal that searches `memory_embeddings`."""
    for path in sorted(IRA_ROOT.rglob("*.py")):
        rel = path.relative_to(IRA_ROOT).as_posix()
        if rel.startswith(("tests/", ".venv/", "venv/")) or "site-packages" in rel:
            continue
        for lineno, s in _strings(path.read_text(encoding="utf-8")):
            if "memory_embeddings" in s and "<=>" in s:
                yield rel, lineno, s


def test_no_query_orders_memory_embeddings_by_raw_distance():
    offenders = [f"{rel}:{lineno}" for rel, lineno, s in _vector_queries()
                 if _RAW_DISTANCE_ORDER.search(s)]
    assert not offenders, (
        "these queries order memory_embeddings by `embedding <=> ...` directly, "
        "which the planner may serve from the global HNSW index — whose "
        "candidates are drawn from every tenant before RLS runs:\n  "
        + "\n  ".join(offenders)
        + "\n\nCompute the distance in a MATERIALIZED CTE and ORDER BY that "
        "column outside it, as memory/store.py::retrieve() does.")


def test_the_scan_reaches_every_known_vector_caller():
    found = {rel for rel, _, _ in _vector_queries()}
    missing = KNOWN_VECTOR_CALLERS - found
    assert not missing, (
        f"the scan found no vector query in {sorted(missing)}. Either the "
        f"module stopped searching memory_embeddings (take it off "
        f"KNOWN_VECTOR_CALLERS) or the scan stopped seeing it, and the guard "
        f"above is reading less than it says.")


def test_the_scan_recognises_the_shape_it_forbids():
    """A ratchet that matches nothing proves nothing."""
    before = '''
SQL = """SELECT content FROM memory_embeddings WHERE user_id = $3
         ORDER BY embedding <=> $1::vector LIMIT $2"""
ALIASED = f"""SELECT t.content FROM memory_embeddings t WHERE {1}
              ORDER BY t.embedding <=> $1 LIMIT $3"""
'''
    after = '''
SQL = """WITH scoped AS MATERIALIZED (
             SELECT content, embedding <=> $1::vector AS distance
               FROM memory_embeddings WHERE user_id = $3)
         SELECT content FROM scoped ORDER BY distance LIMIT $2"""
'''
    assert sum(bool(_RAW_DISTANCE_ORDER.search(s)) for _, s in _strings(before)) == 2
    assert not any(_RAW_DISTANCE_ORDER.search(s) for _, s in _strings(after))
