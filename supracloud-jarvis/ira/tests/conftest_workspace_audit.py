"""P12-01 — which callers touch a workspace-carrying table WITHOUT a workspace.

Answered by EXECUTION, not by grep. A grep for `workspace_connection` finds the
places that name the helper; it says nothing about the places that reach the
same tables another way, which are exactly the ones that matter.

── How it works ─────────────────────────────────────────────────────────────

Every asyncpg query method is wrapped. When a statement mentions one of the
tables that carries `workspace_id`, the wrapper asks the SAME CONNECTION what
`app.workspace_id` is at that moment, and records the answer against the call
site. Nothing is inferred from the source text.

Enable it by importing `install()` from a session-scoped fixture, then read
`findings()`. It is deliberately NOT installed by default: it adds a round trip
per query and would slow every suite that does not need it.

── Why the answer is not simply "wrap them all in workspace_connection" ─────

`045` put `workspace_id` on twenty tables and left it out of every policy, so
today an unbound caller works perfectly. The day P12-03 adds the predicate, an
unbound read stops returning rows — and **it does not error**. It matches
nothing. Every such caller becomes a surface that silently shows an empty page,
which is indistinguishable from "the user has no data yet".

So this audit exists to find them BEFORE the predicate lands. A caller listed
here is not necessarily a bug today; it is a bug scheduled for P12-03.
"""
from __future__ import annotations

import re
import traceback
from collections import defaultdict
from typing import Any

#: Filled from the live schema by `install()`. Hard-coding it would make this
#: audit answer a question about a schema that used to exist — the same error
#: the Phase 12 plan corrected when it quoted "ten tables" from an estimate.
WORKSPACE_TABLES: set[str] = set()

#: The subset of `WORKSPACE_TABLES` whose RLS POLICIES actually reference
#: `workspace_id`, also read from the live catalogue by `install()`.
#:
#: This distinction is the one the first version of this audit did not draw,
#: and drawing it changes what a finding MEANS. The Phase 12 plan assumed the
#: predicate was still to come — "a caller listed here is not necessarily a bug
#: today; it is a bug scheduled for P12-03". `049_workspace_policies.sql`
#: (Phase 3B Batch 5C) had already put it on seventeen tables, and `051` added
#: `notifications`. So for the tables in THIS set an unbound caller is not a
#: scheduled defect, it is a live one: the read returns zero rows now.
#:
#: Two of the twenty carry the column and no predicate — `biometric_audit` and
#: `workspace_memberships` — and an unbound caller of those is a different
#: question, answered by design rather than by defect.
PREDICATE_LIVE: set[str] = set()

#: call site -> {"bound": n, "unbound": n, "tables": {...},
#:               "per_table": {table: {"bound": n, "unbound": n}}}
#:
#: `per_table` is what makes a classification evidence-backed rather than
#: file-shaped. A file that binds correctly for nineteen tables and not for the
#: twentieth reports as "mostly bound" at file granularity, which is the
#: granularity that hid FINDING A's real shape.
_SEEN: dict[str, dict[str, Any]] = defaultdict(
    lambda: {"bound": 0, "unbound": 0, "tables": set(),
             "per_table": defaultdict(lambda: {"bound": 0, "unbound": 0})})

_INSTALLED = False
_ORIGINALS: dict[str, Any] = {}


def _tables_in(sql: str) -> set[str]:
    """Which workspace-carrying tables this statement mentions.

    Word-boundary matched so `tasks` does not match `subtasks` and `people`
    does not match `people_archive`. Crude on purpose: a false positive costs
    one extra round trip, a false negative loses a finding.
    """
    if not sql:
        return set()
    lowered = sql.lower()
    return {t for t in WORKSPACE_TABLES
            if re.search(rf"\b{re.escape(t)}\b", lowered)}


def _call_site() -> str:
    """The first frame outside this file, asyncpg and the test harness.

    The product code that issued the query, not the plumbing that carried it.
    """
    for frame in reversed(traceback.extract_stack()[:-2]):
        f = frame.filename
        if ("/asyncpg/" in f or f.endswith("conftest_workspace_audit.py")
                or "/_pytest/" in f or "/pluggy/" in f):
            continue
        if "/tests/" in f:
            # A test calling the database directly is not a product caller.
            return f"TEST:{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}"
        return f"{frame.filename.split('/ira/', 1)[-1]}:{frame.lineno}"
    return "unknown"


def install(table_names, predicate_live=()) -> None:
    global _INSTALLED
    WORKSPACE_TABLES.clear()
    WORKSPACE_TABLES.update(table_names)
    PREDICATE_LIVE.clear()
    PREDICATE_LIVE.update(predicate_live)
    if _INSTALLED:
        return

    import asyncpg

    def _wrap(name: str):
        original = getattr(asyncpg.Connection, name)

        async def wrapper(self, query, *args, **kwargs):
            touched = _tables_in(query if isinstance(query, str) else "")
            if touched:
                site = _call_site()
                try:
                    ws = await _ORIGINALS["fetchval"](
                        self, "SELECT current_setting('app.workspace_id', true)")
                except Exception:
                    ws = None
                record = _SEEN[site]
                record["tables"] |= touched
                key = "bound" if ws else "unbound"
                record[key] += 1
                for table in touched:
                    record["per_table"][table][key] += 1
            return await original(self, query, *args, **kwargs)

        _ORIGINALS[name] = original
        setattr(asyncpg.Connection, name, wrapper)

    # `fetchval` first: the wrapper above calls the ORIGINAL to ask for the
    # GUC, and asking through the wrapped one would recurse forever.
    for method in ("fetchval", "fetch", "fetchrow", "execute"):
        _wrap(method)
    _INSTALLED = True


def findings() -> list[dict[str, Any]]:
    """Call sites that touched a workspace table with NOTHING bound.

    Test call sites are excluded: a test reaching the database directly is not
    a product surface, and listing them would bury the real findings.

    Each finding carries `live_tables` — the tables it touched unbound whose
    policy ALREADY reads `app.workspace_id`. That list, not the raw unbound
    count, is what separates a defect from a design decision: an unbound read
    of `workspace_memberships` is how you find out which workspace to bind, and
    an unbound read of `messages` returns nothing and calls it no data.
    """
    out = []
    for site, rec in sorted(_SEEN.items()):
        if site.startswith("TEST:") or not rec["unbound"]:
            continue
        live = sorted(t for t, c in rec["per_table"].items()
                      if c["unbound"] and t in PREDICATE_LIVE)
        out.append({"site": site, "unbound": rec["unbound"],
                    "bound": rec["bound"], "tables": sorted(rec["tables"]),
                    "live_tables": live,
                    "per_table": {t: dict(c) for t, c in rec["per_table"].items()}})
    return out


def observed() -> dict[str, dict[str, Any]]:
    return dict(_SEEN)
