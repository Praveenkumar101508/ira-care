"""IRA Care Activity never reaches an alert (F7, D-084).

The estimate is a wellbeing context signal. These checks hold the wall in place by reading the
code: the hours live in their own table, which only the activity module, the routes that call
it and the retention purge touch; no module on the alert path (rules, escalation, ask-first,
falls, SOS, check-ins, delivery, push, reports, the companion) reads that table or imports the
activity module; and ingestion calls no safety rule. The database test
(tests/tenancy/test_care_activity_db.py::test_a_day_of_stillness_off_the_wrist_and_unknown_opens_no_alert)
checks the same thing by behaviour.
"""
from __future__ import annotations

import ast
import pathlib

CARE = pathlib.Path(__file__).resolve().parents[2] / "care"
IRA = CARE.parent

TABLE = "care_activity_periods"

#: The only code that may name the table.
TABLE_READERS = {"activity.py", "retention.py"}

#: Modules on the path from a reading to an alert, a message or a report.
ALERT_PATH = ("monitoring", "continuous", "escalation", "askfirst", "falls", "sos", "checkin_cycle", "checkins",
              "delivery", "push", "providers", "reports", "conversation", "talk", "followup", "visits", "overview",
              "repository", "location", "classifier")


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            out.add(mod)
            out.update(f"{mod}.{a.name}" if mod else a.name for a in node.names)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


def table_readers(root: pathlib.Path = IRA) -> set[str]:
    found = set()
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if rel.parts[0] in ("tests", "scripts") or "__pycache__" in rel.parts:
            continue
        if TABLE in path.read_text(encoding="utf-8", errors="ignore"):
            found.add(str(rel))
    return found


def test_only_the_activity_module_and_retention_name_the_table():
    assert table_readers() == {f"care/{n}" for n in TABLE_READERS}


def test_no_module_on_the_alert_path_imports_the_activity_module():
    offenders = []
    for name in ALERT_PATH:
        imports = _imports(CARE / f"{name}.py")
        if any(i.endswith("activity") or i.endswith(".activity") or i == "activity" for i in imports if "activity_estimate" not in i):
            offenders.append(name)
    assert offenders == []


def _code_words(path: pathlib.Path) -> str:
    """Every name, attribute and string in the module except docstrings, which may describe
    what the module does not do."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    words = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            words.append(node.id)
        elif isinstance(node, ast.Attribute):
            words.append(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            words.append(node.value)
    return "\n".join(words)


def test_the_activity_module_opens_nothing():
    imported = _imports(CARE / "activity.py")
    for mod in ("escalation", "askfirst", "monitoring", "continuous", "sos", "falls", "push", "delivery", "providers"):
        assert not any(i.endswith(mod) for i in imported), mod
    words = _code_words(CARE / "activity.py")
    for call in ("care_alerts", "care_checkins", "care_vitals", "evaluate_arrivals", "open_from_observation", "record_report"):
        assert call not in words, call


def test_ingestion_runs_no_safety_rule():
    tree = ast.parse((CARE / "routes.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "device_activity")
    body = ast.unparse(fn)
    assert "activity.record_periods" in body
    for forbidden in ("evaluate_arrivals", "record_report", "askfirst", "escalation", "falls", "sos."):
        assert forbidden not in body, forbidden


def test_the_scanner_finds_a_reader_on_the_alert_path(tmp_path):
    """Negative control for the table check: a copy of the care package in which the escalation
    module reads the table is caught."""
    root = tmp_path / "ira"
    (root / "care").mkdir(parents=True)
    for n in TABLE_READERS:
        (root / "care" / n).write_text(f"# {TABLE}\n")
    (root / "care" / "escalation.py").write_text(f'Q = "SELECT * FROM {TABLE}"\n')
    assert table_readers(root) == {"care/activity.py", "care/retention.py", "care/escalation.py"}
