"""Phase 3A — trace real application access per table across the whole repo.

Searches backend, workers, scripts, tests and frontend. A table is only called
unused after every one of those has been searched.
"""
import json, re, sys
from pathlib import Path
from collections import defaultdict

ROOT = Path("/home/user/supracloud-ira")
IRA = ROOT / "supracloud-jarvis" / "ira"
SNAP = json.load(open(ROOT / "artifacts/cloud/phase-3a/database-schema.json"))
TABLES = sorted(SNAP["tables"])

SCAN_DIRS = [
    ("backend",  IRA, ("*.py",), ("tests",)),
    ("worker",   IRA / "worker", ("*.py",), ()),
    ("scripts",  IRA / "scripts", ("*.py", "*.sh"), ()),
    ("tests",    IRA / "tests", ("*.py",), ()),
    ("frontend", ROOT / "supracloud-jarvis/frontend", ("*.ts", "*.tsx"), ("node_modules",)),
    ("sql",      ROOT / "supracloud-jarvis/postgres", ("*.sql",), ()),
]

files = []
for label, base, globs, excl in SCAN_DIRS:
    if not base.exists():
        continue
    for g in globs:
        for p in base.rglob(g):
            s = str(p)
            if "__pycache__" in s or any(f"/{e}/" in s for e in excl):
                continue
            if label == "backend" and "/tests/" in s:
                continue
            files.append((label, p))

PATTERNS = {
    "read":   re.compile(r"\b(?:FROM|JOIN)\s+(\w+)", re.I),
    "insert": re.compile(r"\bINSERT\s+INTO\s+(\w+)", re.I),
    "update": re.compile(r"\bUPDATE\s+(\w+)\s+SET", re.I),
    "delete": re.compile(r"\bDELETE\s+FROM\s+(\w+)", re.I),
    "ddl":    re.compile(r"\b(?:CREATE TABLE|ALTER TABLE|DROP TABLE)\s+(?:IF (?:NOT )?EXISTS\s+)?(\w+)", re.I),
}

acc = defaultdict(lambda: defaultdict(set))
mentions = defaultdict(set)

for label, path in files:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        continue
    rel = str(path.relative_to(ROOT))
    for op, pat in PATTERNS.items():
        for m in pat.finditer(text):
            name = m.group(1).lower()
            if name in TABLES:
                acc[name][op].add(rel)
    for t in TABLES:
        # Bare mention: catches ORM/helper usage that no SQL regex would find.
        if re.search(rf"\b{re.escape(t)}\b", text):
            mentions[t].add(rel)

WORKER_HINT = re.compile(r"/worker/|/tasks/|scheduler|heartbeat|reflection", re.I)

out = {}
for t in TABLES:
    ops = acc[t]
    all_mentions = sorted(mentions[t])
    non_test = [m for m in all_mentions if "/tests/" not in m]
    sql_only = [m for m in non_test if m.endswith(".sql")]
    code_only = [m for m in non_test if not m.endswith(".sql")]
    out[t] = {
        "read_call_sites":    sorted(ops.get("read", ())),
        "insert_call_sites":  sorted(ops.get("insert", ())),
        "update_call_sites":  sorted(ops.get("update", ())),
        "delete_call_sites":  sorted(ops.get("delete", ())),
        "ddl_sites":          sorted(ops.get("ddl", ())),
        "background_worker_call_sites": sorted(
            m for m in non_test if WORKER_HINT.search(m)),
        "frontend_mentions":  sorted(m for m in all_mentions if "/frontend/" in m),
        "test_mentions":      len([m for m in all_mentions if "/tests/" in m]),
        "non_test_code_files": len(code_only),
        "sql_files": len(sql_only),
        "referenced_in_code": bool(code_only),
    }

json.dump(out, open(sys.argv[1], "w"), indent=1, sort_keys=True)

unused = [t for t in TABLES if not out[t]["referenced_in_code"]]
print("tables:", len(TABLES))
print("files scanned:", len(files))
print("with non-test code references:", sum(1 for t in TABLES if out[t]["referenced_in_code"]))
print("NO non-test code reference:", len(unused))
for t in unused:
    print("   ", t, "| tests:", out[t]["test_mentions"], "| sql:", out[t]["sql_files"])
