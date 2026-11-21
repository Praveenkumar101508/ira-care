"""Every route that reads a person's health data or words writes the access log.

Checked by reading the route module's source: a route whose path names one
of the health resources must reference `audit.record_access` in its body (or
delegate to a module that does: `history.search` writes its own row). A new
route added without the audit fails here on the day it is written.
"""
from __future__ import annotations

import ast
import pathlib

ROUTES = pathlib.Path(__file__).resolve().parents[2] / "care" / "routes.py"

#: Path fragments whose GET handlers read health data or the person's words.
AUDITED_FRAGMENTS = ("/vitals", "/checkins", "/transcripts", "/medications", "/medication-events",
                     "/reports", "/status", "/history/search")

#: Handlers whose audit row is written by the module they call.
DELEGATED = {"search_history": "history.search"}


def _routes():
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for deco in node.decorator_list:
            if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute) and deco.func.value.id == "router":
                method = deco.func.attr
                path = deco.args[0].value if deco.args and isinstance(deco.args[0], ast.Constant) else ""
                yield method, path, node


def _source_of(node) -> str:
    return ast.unparse(node)


def test_every_health_data_read_is_audited():
    missing = []
    seen = 0
    for method, path, node in _routes():
        if method != "get" or not any(f in path for f in AUDITED_FRAGMENTS):
            continue
        if path.startswith("/devices/me"):
            continue  # the watch's own reads of its own check-ins are not a person looking at health data
        seen += 1
        body = _source_of(node)
        ok = "audit.record_access(" in body or DELEGATED.get(node.name, "") in body
        if not ok:
            missing.append(f"{method.upper()} {path} ({node.name})")
    assert seen >= 8, f"expected to find the health-data routes; found {seen}"
    assert not missing, "routes that read health data without writing the access log:\n  " + "\n  ".join(missing)


def test_the_erasure_route_is_audited_before_it_deletes():
    for method, path, node in _routes():
        if method == "delete" and path == "/people/{person_id}":
            body = _source_of(node)
            assert "audit.record_access(" in body and body.index("audit.record_access(") < body.index("repo.erase_person(")
            return
    raise AssertionError("the person erasure route was not found")
