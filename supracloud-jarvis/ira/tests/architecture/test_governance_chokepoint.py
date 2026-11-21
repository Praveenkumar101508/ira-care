"""Phase 4 — the permission chokepoint is enforced by the import graph, not by
reviewers remembering.

`tests/governance/test_no_execution_without_decision.py` proves the RUNTIME
property: without a valid ticket, nothing executes. That covers the code that
exists today. It does not stop somebody adding a second way to mint tickets next
year, which would reopen the hole without breaking a single existing test.

So this file states the structural rule and checks it with an AST scan:

    Only the Permission Broker may mint an ExecutionTicket.

If `ticket.issue(...)` is called from anywhere else, there is a path to
execution that did not pass through a permission decision — which is precisely
the thing Phase 4's acceptance criterion forbids. The same goes for constructing
an `ExecutionTicket` directly: it would fail signature verification at runtime,
but its presence in source is a sign somebody is trying to route around the
broker, and it should be caught in review rather than in production.

The scan is AST-based rather than textual so that a mention in a docstring, a
comment or a string literal is not mistaken for a call — and so a real call
cannot hide behind unusual formatting.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# tests/architecture/ -> tests/ -> ira/
IRA_ROOT = Path(__file__).resolve().parents[2]
GOVERNANCE_DIR = IRA_ROOT / "core" / "governance"

#: The only module allowed to mint tickets. Broker and gateway are both inside
#: `core/governance/`; the broker mints, the gateway consumes.
TICKET_MINTING_ALLOWED = {"broker.py", "ticket.py", "__init__.py"}

#: Functions that create an execution capability.
MINTING_NAMES = {"issue", "ExecutionTicket"}


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _called_names(tree: ast.AST) -> set[str]:
    """Every function/constructor name CALLED in this tree.

    Both `issue(...)` and `ticket.issue(...)` are captured, so importing the
    module under a different name does not evade the check.
    """
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            called.add(func.id)
        elif isinstance(func, ast.Attribute):
            called.add(func.attr)
    return called


def _production_files() -> list[Path]:
    """Every non-test Python file in the ira package.

    Tests are excluded deliberately: proving the guard works REQUIRES a test
    that forges a ticket, and a rule that forbade that would forbid testing the
    rule.
    """
    out: list[Path] = []
    for path in _python_files(IRA_ROOT):
        parts = path.relative_to(IRA_ROOT).parts
        if parts and parts[0] in ("tests", "scripts", "docs"):
            continue
        out.append(path)
    return out


def test_the_governance_package_exists():
    """Without this, every assertion below would pass over an empty list."""
    assert GOVERNANCE_DIR.is_dir(), f"missing {GOVERNANCE_DIR}"
    assert (GOVERNANCE_DIR / "ticket.py").is_file()
    assert (GOVERNANCE_DIR / "broker.py").is_file()
    assert (GOVERNANCE_DIR / "gateway.py").is_file()


def test_scanner_finds_production_files():
    """Guards against the scan silently covering nothing."""
    files = _production_files()
    assert len(files) > 50, f"only {len(files)} production files found — scan is wrong"
    names = {p.name for p in files}
    assert "broker.py" in names and "gateway.py" in names


def test_only_the_broker_mints_execution_tickets():
    """The structural half of Phase 4's acceptance criterion.

    A second minting site is a second way to reach execution without a decision.
    """
    offenders: list[str] = []

    for path in _production_files():
        if path.parent == GOVERNANCE_DIR and path.name in TICKET_MINTING_ALLOWED:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover - would fail the build elsewhere
            continue
        minted = _called_names(tree) & MINTING_NAMES
        # `issue` is a common enough verb that an unrelated function could share
        # the name. Only count it when the file actually reaches the ticket
        # module, which an unrelated `issue()` would not.
        if minted:
            source = path.read_text(encoding="utf-8")
            if "governance.ticket" in source or "governance import ticket" in source:
                offenders.append(
                    f"{path.relative_to(IRA_ROOT)}: calls {', '.join(sorted(minted))}"
                )

    assert not offenders, (
        "Execution tickets are minted outside the Permission Broker:\n"
        + "\n".join(offenders)
        + "\n\nOnly core/governance/broker.py may mint a ticket. Any other "
        "minting site is a path to executing an action without a permission "
        "decision, which Phase 4's acceptance criterion forbids."
    )


def test_the_signing_key_never_leaves_its_module():
    """A leaked key would make every forgery check meaningless."""
    offenders: list[str] = []
    for path in _production_files():
        if path == GOVERNANCE_DIR / "ticket.py":
            continue
        if "_SIGNING_KEY" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(IRA_ROOT)))
    assert not offenders, (
        "The ticket signing key is referenced outside core/governance/ticket.py: "
        + ", ".join(offenders)
    )


def test_the_gateway_does_not_import_connectors():
    """The gateway takes a callable; it must not grow into a connector hub.

    A hub would become the thing every future connector has to be wired into,
    and that wiring is what gets skipped under time pressure. Taking a callable
    means a connector cannot be reached except by a caller that already holds a
    ticket.
    """
    gateway = GOVERNANCE_DIR / "gateway.py"
    tree = ast.parse(gateway.read_text(encoding="utf-8"), filename=str(gateway))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden_prefixes = ("actions.calendar_dav", "actions.email_triage", "armory.", "coding.")
    leaked = sorted(
        name for name in imported if name.startswith(forbidden_prefixes)
    )
    assert not leaked, (
        f"core/governance/gateway.py imports connector modules: {leaked}. It must "
        f"take an executor callable instead."
    )


def test_the_broker_does_not_execute_anything():
    """A broker that could execute would make the ticket ceremonial.

    It decides and it mints; running the action is the gateway's job, after the
    ticket has been verified and consumed.
    """
    broker = GOVERNANCE_DIR / "broker.py"
    tree = ast.parse(broker.read_text(encoding="utf-8"), filename=str(broker))

    called = _called_names(tree)
    forbidden = {"execute", "run", "subprocess", "Popen", "system", "eval", "exec"}
    overlap = called & forbidden
    assert not overlap, (
        f"core/governance/broker.py calls {sorted(overlap)}. The broker decides; "
        f"it must never execute."
    )


# ── Prove the scanner can actually fail ──────────────────────────────────────
#
# A structural test that cannot fail is decoration. These run the same scanning
# logic over a PLANTED violation and require it to be caught.


def test_the_scanner_catches_a_planted_minting_call(tmp_path):
    planted = tmp_path / "sneaky_executor.py"
    planted.write_text(
        "from core.governance.ticket import issue\n"
        "\n"
        "def go(request):\n"
        "    ticket = issue(request, effect='allow', risk='low')\n"
        "    return ticket\n",
        encoding="utf-8",
    )

    tree = ast.parse(planted.read_text(encoding="utf-8"))
    minted = _called_names(tree) & MINTING_NAMES
    source = planted.read_text(encoding="utf-8")

    assert minted, "the AST scan missed a direct call to issue()"
    assert "governance.ticket" in source, "the import filter missed the module reference"


def test_the_scanner_ignores_a_mention_in_a_comment(tmp_path):
    """Textual matching would flag this; AST matching must not.

    False positives get a check disabled, so being precise is part of it working.
    """
    innocent = tmp_path / "documented.py"
    innocent.write_text(
        '"""This module explains how issue() and ExecutionTicket work."""\n'
        "# Do not call issue() here — see core/governance/ticket.py\n"
        "MESSAGE = 'ExecutionTicket'\n"
        "def unrelated():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    tree = ast.parse(innocent.read_text(encoding="utf-8"))
    assert not (_called_names(tree) & MINTING_NAMES), (
        "the scanner flagged a docstring/comment mention as a call"
    )


@pytest.mark.parametrize("attribute_form", ["ticket.issue(r)", "_ticket.issue(r)"])
def test_the_scanner_catches_attribute_style_calls(attribute_form, tmp_path):
    """Importing the module under another name must not evade the check."""
    planted = tmp_path / "aliased.py"
    planted.write_text(
        f"from core.governance import ticket as _ticket\n"
        f"def go(r):\n"
        f"    return {attribute_form}\n",
        encoding="utf-8",
    )
    tree = ast.parse(planted.read_text(encoding="utf-8"))
    assert "issue" in _called_names(tree)
