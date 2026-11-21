"""P16-02 — FINDING AH, and the class of defect it belongs to.

`api/routes/brain.py` built a closure that called `retrieve(principal, ...)`
where **`principal` was bound nowhere**. Every call raised `NameError`, which
the closure's own `except Exception` swallowed into `[]`, so the affect layer's
relational recall returned nothing for fifteen phases while looking deliberate.

── Why this test reads code objects and not source ─────────────────────────

ruff's `F821` finds it. **A lint rule can be silenced with one comment**, and
the finding was originally invisible precisely because no linter ran in CI at
all — so a test that depends on a lint rule inherits the fragility that caused
the defect.

So this reads the **compiled** function: `code.co_names` is the set of names
Python will look up in globals-then-builtins at runtime. If a name in there
exists in neither, the lookup is a guaranteed `NameError` the moment that line
executes. That is not a style opinion; it is what the interpreter will do.

`# noqa` does not change a code object.

── What it deliberately does not do ────────────────────────────────────────

It is **not** a reimplementation of pyflakes over the whole package. `co_names`
also carries attribute names and names bound later at module scope, so a naive
sweep produces false positives, and a test with false positives gets deleted.

It is scoped to the functions where the failure mode actually bites: those whose
body swallows exceptions, where a `NameError` cannot reach anybody. A defect in
a function that raises gets caught the first time it runs. This one could not.
"""
from __future__ import annotations

import builtins
import os
import types

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")


def _code_objects(code):
    """`code` and every code object nested inside it, recursively."""
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _code_objects(const)


def _unresolvable_globals(func) -> list[tuple[str, str]]:
    """Every global name the function (or a closure inside it) cannot resolve.

    Returns (code object name, global name) pairs. A name counts as resolvable
    if it is a module global, a builtin, or bound anywhere inside the function
    — `co_varnames` and `co_cellvars` cover locals and captured cells, and
    imports inside the body land in `co_varnames` too.
    """
    module_globals = set(func.__globals__)
    builtin_names = set(dir(builtins))
    out = []
    for code in _code_objects(func.__code__):
        local = set(code.co_varnames) | set(code.co_cellvars) | set(code.co_freevars)
        for name in code.co_names:
            if name in module_globals or name in builtin_names or name in local:
                continue
            # Attribute access also lands in co_names (`x.foo` stores "foo").
            # Only flag a name that is LOADed as a global by the bytecode.
            import dis
            for instr in dis.get_instructions(code):
                if instr.opname in ("LOAD_GLOBAL", "LOAD_NAME") and instr.argval == name:
                    out.append((code.co_name, name))
                    break
    return out


def test_build_affect_has_no_unresolvable_global():
    """FINDING AH, asserted where the interpreter would have failed.

    On the code as it stood, this reports `('_recall', 'principal')` — the exact
    lookup that raised on every call and was swallowed one line later.
    """
    from api.routes import brain

    bad = _unresolvable_globals(brain._build_affect)
    assert bad == [], (
        f"_build_affect (or a closure inside it) loads global name(s) that exist "
        f"in neither the module nor builtins: {bad}. Each is a guaranteed "
        f"NameError the moment that line runs — and this function's closures "
        f"swallow exceptions, so nobody would ever see it. That is FINDING AH.")


def test_the_affect_layer_is_told_it_has_no_recall():
    """The fix, asserted as a STATE rather than as an absence of errors.

    `AffectLayer` checks `self.recall is None` before calling, so "not wired"
    and "returned nothing" take different branches. FINDING AH was the second
    one wearing the first one's clothes.

    Asserted positively — `recall is None` — rather than "does not raise",
    because "does not raise" is exactly what the broken version did.
    """
    from api.routes.brain import _build_affect

    affect = _build_affect()
    if affect is None:
        pytest.skip("IRA_BRAIN_AFFECT_ENABLED is off in this environment, so "
                    "there is no layer to inspect. This skip is reported "
                    "rather than passing silently.")
    assert affect.recall is None, (
        f"the affect layer was given a recall ({affect.recall!r}). If one has "
        f"been wired deliberately it must reach `memory.store.retrieve` with a "
        f"VERIFIED principal — not a name invented at startup, which is the "
        f"workspace authority Phase 12 refuses to mint. Update this test with "
        f"the provenance of that principal if so.")


def test_a_recall_that_never_reaches_retrieve_is_detectable():
    """The negative control: this file's technique must catch the old defect.

    Reconstructs the broken closure — a function whose body calls a name bound
    nowhere — and requires `_unresolvable_globals` to find it. Without this, a
    helper that silently stopped detecting would leave both tests above green,
    which is the failure this phase is named for.
    """
    ns: dict = {}
    exec(  # noqa: S102 - constructing the historical defect deliberately
        "def _rebuilt():\n"
        "    def _recall(q):\n"
        "        try:\n"
        "            return retrieve(principal, q)\n"
        "        except Exception:\n"
        "            return []\n"
        "    return _recall\n",
        ns)

    bad = _unresolvable_globals(ns["_rebuilt"])
    assert ("_recall", "principal") in bad, (
        f"the detector did not find the historical defect it was written for. "
        f"It reported {bad}. Both tests above are therefore worthless.")
