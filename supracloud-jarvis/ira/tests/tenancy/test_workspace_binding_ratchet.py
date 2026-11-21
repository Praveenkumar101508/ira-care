"""The number of modules reaching workspace-scoped tables unbound may only fall.

Batch 5C is a migration in the literal sense: a known set of call sites moving
from `acquire()` to `workspace_connection()`, one component at a time, with the
application working throughout. That shape has a specific failure mode — the
count stops going down, or quietly goes back up when somebody adds a module
that looks like its neighbours.

So the remaining work is written down as data, and this file asserts three
things about it:

  1. Nothing is missing from the list. A module that touches one of the
     seventeen tables and binds no workspace must be named here.
  2. Nothing stale is on it. A module that has been bound must come off, or
     the list stops describing reality and starts being ignored.
  3. The list only shrinks. `MAX_UNBOUND` is a ceiling that gets lowered as
     components land, never raised to make a failure go away.

The policy migration must not run while this list is non-empty. Adding
`app.workspace_id` to a policy while any writer is unbound makes every one of
its reads silently empty and every one of its writes fail —
`test_workspace_scope_boundary.py` holds the guard that ties those together.
"""
from __future__ import annotations

import collections
import pathlib
import re

#: The seventeen tables `045_workspace_columns.sql` gave a `workspace_id`.
WORKSPACE_SCOPED_TABLES = (
    "agents", "calendar_events", "commitments", "companion_versions",
    "companions", "conversations", "decisions", "edges", "entities", "files",
    "lifeos_events", "memory_embeddings", "messages", "people", "projects",
    "reminders", "tasks",
)

#: Modules that still reach those tables without binding a workspace.
#:
#: **EMPTY.** Batch 5C finished the rewire, so this is no longer a work list —
#: it is a floor. A name appearing here again means a module was added that
#: reaches customer data with no workspace bound, and the fix is to bind it,
#: never to add it here.
KNOWN_UNBOUND: set[str] = set()

#: Lower this as components land. Never raise it.
MAX_UNBOUND = 0

IRA_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: `FROM agents.graph import ...` is a Python import, not a query against the
#: `agents` table. An earlier version of this scan matched it and inflated the
#: inventory from 27 files to 55 — the count that drives this whole batch has
#: to be right, so import lines are excluded and a trailing `.` or word
#: character disqualifies a match.
_STATEMENT = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM)\s+(\"?)("
    + "|".join(WORKSPACE_SCOPED_TABLES)
    + r")\1(?![\w.])",
    re.I,
)
_NOT_A_STATEMENT = re.compile(r"^\s*(from|import)\s|^\s*#")


def _scan() -> dict[str, collections.Counter]:
    """Modules touching the seventeen tables, split by whether they bind."""
    found: dict[str, collections.Counter] = {}
    for path in sorted(IRA_ROOT.rglob("*.py")):
        rel = path.relative_to(IRA_ROOT).as_posix()
        if rel.startswith("tests/") or "__pycache__" in rel:
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        if not _STATEMENT.search(source):
            continue
        counts = collections.Counter()
        for line in source.splitlines():
            if _NOT_A_STATEMENT.match(line):
                continue
            for match in _STATEMENT.finditer(line):
                counts[match.group(2).lower()] += 1
        if counts:
            found[rel] = counts
    return found


def _unbound() -> dict[str, collections.Counter]:
    out = {}
    for rel, counts in _scan().items():
        source = (IRA_ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        # A module that mentions `workspace_connection` at all is treated as
        # bound. That is deliberately coarse: this is a progress ratchet, and
        # the proof that a bound module is CORRECTLY bound is the RLS test
        # suite against a real database, not a substring.
        if "workspace_connection" not in source:
            out[rel] = counts
    return out


def test_no_new_module_reaches_workspace_data_unbound():
    actual = set(_unbound())
    new = actual - KNOWN_UNBOUND
    assert not new, (
        f"module(s) newly reaching workspace-scoped tables with no workspace "
        f"bound: {sorted(new)}.\n\n"
        "Under forced RLS an unbound connection matches no rows on read and is "
        "refused on write, so this is not a style point: the module either "
        "silently returns nothing or loudly fails, depending on which way it "
        "runs. Bind it with workspace_connection(principal). Do not add the "
        "name to KNOWN_UNBOUND to silence this."
    )


def test_the_work_list_does_not_go_stale():
    actual = set(_unbound())
    done = KNOWN_UNBOUND - actual
    assert not done, (
        f"{sorted(done)} now bind a workspace — remove them from KNOWN_UNBOUND "
        f"and lower MAX_UNBOUND to {len(actual)} so the list keeps meaning what "
        f"it says"
    )


def test_the_count_only_falls():
    actual = _unbound()
    assert len(actual) <= MAX_UNBOUND, (
        f"{len(actual)} modules reach workspace-scoped tables unbound, above "
        f"the ceiling of {MAX_UNBOUND}. The ceiling comes down as components "
        f"land; it does not go up."
    )


def test_the_scanner_finds_the_modules_that_are_already_bound():
    """Non-vacuity. A scan that matched nothing would make every test above
    pass permanently and silently."""
    bound = set(_scan()) - set(_unbound())
    assert {"memory/store.py", "api/routes/memory.py"} <= bound, (
        f"the scanner does not see the modules Batch 5C has already bound "
        f"(found {sorted(bound)}) — it is not reading what it thinks it is"
    )


def test_the_policy_migration_waits_for_zero():
    """The ratchet's purpose, stated as an assertion.

    Adding `app.workspace_id` to a policy while any writer is unbound empties
    that writer's reads and refuses its writes. This fails if a policy migration
    lands early, and stops applying once the list is empty — at which point the
    denial tests take over.
    """
    policies_enforce = any(
        "app.workspace_id" in block
        for path in sorted((IRA_ROOT.parent / "postgres").glob("*.sql"))
        for block in re.findall(
            r"CREATE\s+POLICY\b.*?;", path.read_text(encoding="utf-8"), re.S | re.I
        )
    )
    remaining = sorted(_unbound())
    assert not (policies_enforce and remaining), (
        f"a policy now reads app.workspace_id, but {len(remaining)} module(s) "
        f"still reach workspace-scoped tables unbound: {remaining}.\n\n"
        "Their reads will return nothing and their writes will be refused. "
        "Bind them first, or roll the policy migration back."
    )
