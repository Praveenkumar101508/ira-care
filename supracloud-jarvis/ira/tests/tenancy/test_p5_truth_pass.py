"""Phase 5 batch 06 — the adversarial truth pass over Phase 5's own claims.

The brief for this batch is to challenge my own work, not to re-run it. So for
every Phase 5 claim: does the code exist, is it reachable, does anything call
it, can the test that covers it genuinely fail, and does the documentation match
the executable tree.

── What it found ───────────────────────────────────────────────────────────

The "function exists but has no caller" class, in this phase's own code.
`require_serving`, `for_tenant` and `rollout_bucket` had no consumer outside
their module and their tests — a registry API that reads well and that nothing
in the request path uses.

`require_serving` was deleted: `serving` already does what any caller needs.

`for_tenant` and `rollout_bucket` were kept and recorded as UNCONSUMED, because
the reason they cannot be wired is structural rather than an omission — see
`UNCONSUMED` below. Keeping them silently would have been the defect; deleting
them would have thrown away the tested half of MOD-03's rollout and left the
schema columns with nothing behind them at all.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

IRA_ROOT = pathlib.Path(__file__).resolve().parents[2]
REPO = IRA_ROOT.parents[1]

#: Phase 5 modules whose public functions must be reachable from the product.
P5_MODULES = ("core/models/registry.py", "core/models/artefacts.py")

#: Deliberately not consumed yet, each with the reason it cannot be.
#:
#: **Empty, and that is the point of P5-07.** P5-06 found `for_tenant` and
#: `rollout_bucket` here with a structural reason: a provider reported a model
#: NAME, so the gateway could neither select nor verify a version. The owner
#: rejected that as a closure and asked for the contract to change instead —
#: which it did. `ApprovedModel` now carries the version and the revision, the
#: gateway resolves it before any provider runs, and `resolve_for_request`
#: consumes both functions on every enforced request.
#:
#: The list stays, with its enforcement, because the next mechanism that arrives
#: without a caller should have to argue for itself in writing.
UNCONSUMED: dict[str, str] = {}


def _public_functions(rel_path: str) -> dict[str, ast.AST]:
    tree = ast.parse((IRA_ROOT / rel_path).read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }


def _called_in_product(name: str, own_module: str) -> bool:
    """Is `name` CALLED anywhere in the product? Tests do not count.

    A function whose only callers are its own tests is the defect this checks
    for — the tests pass, the product never runs it, and P4-06 shipped exactly
    that.

    ── A correction to this check, recorded ────────────────────────────────

    The first version required a caller OUTSIDE the defining module, and
    flagged `current_environment` and `allowlisted`. Neither is a defect: the
    first is called by `serving` and the second by `require_allowlisted`, both
    in the same file. A helper used by its own module is reachable from the
    product, and a rule that says otherwise pushes people to flatten modules
    into one function to satisfy it.

    So the module is included, and only the `def` line is skipped — a
    definition is not a call.
    """
    for path in sorted(IRA_ROOT.rglob("*.py")):
        s = str(path)
        if "__pycache__" in s or "/tests/" in s:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called = (func.attr if isinstance(func, ast.Attribute)
                          else getattr(func, "id", None))
                if called == name:
                    return True
    return False


@pytest.mark.parametrize("module", P5_MODULES)
def test_every_phase_5_mechanism_has_a_caller_or_a_recorded_reason(module):
    """**The class this phase produced in its own code.**

    P4-06 shipped retention whose one consumer had no callers; P4-05 found a
    rotation step that reached two of three families of protected material.
    Phase 5 did it again with the registry's rollout API, and this is the guard
    that found it.
    """
    orphans = []
    for name in _public_functions(module):
        if name in UNCONSUMED:
            continue
        if not _called_in_product(name, module):
            orphans.append(f"{module}::{name}")
    assert not orphans, (
        "these Phase 5 functions have no caller in the product — only their "
        "own tests. Either wire them up, delete them, or add them to "
        "UNCONSUMED with the structural reason they cannot be reached:\n  "
        + "\n  ".join(orphans))


def test_the_unconsumed_list_is_empty_or_every_entry_still_exists():
    """An exemption for a function that no longer exists is a stale licence.

    Without this, deleting `for_tenant` would leave its exemption behind, and
    the next function that happened to share the name would inherit a waiver
    nobody granted.
    """
    from core.models import registry

    for name in UNCONSUMED:
        assert hasattr(registry, name), (
            f"UNCONSUMED names {name!r}, which no longer exists in the registry")


def test_the_unconsumed_functions_say_so_in_their_own_docstrings():
    """The list is in a test file. Somebody reading the module needs to know
    without going looking, or they will wire a caller to something whose
    semantics were never finished."""
    from core.models import registry

    for name in UNCONSUMED:
        doc = inspect.getdoc(getattr(registry, name)) or ""
        assert "NOT YET CONSUMED" in doc.upper(), (
            f"{name} is exempt from the caller rule and its docstring does not "
            f"say so")


# ─────────────────────────────────────────────────────────────────────────────
# Can the Phase 5 tests actually fail?
# ─────────────────────────────────────────────────────────────────────────────


def test_the_registry_schema_suite_asserts_refusals_rather_than_successes():
    """A schema suite made only of successful inserts passes against a table
    with no constraints at all. The refusals are the evidence."""
    text = (IRA_ROOT / "tests" / "tenancy"
            / "test_p5_model_registry_schema.py").read_text(encoding="utf-8")
    raises = text.count("pytest.raises")
    assert raises >= 10, (
        f"only {raises} refusals asserted in the registry schema suite; the "
        f"constraints are what 067 is for")
    for constraint in ("revision_is_actually_pinned", "artifact_hash_is_a_sha256",
                       "serving_is_licensed", "serving_reaches_somebody",
                       "rolled_back_names_its_target",
                       "model_registry_one_serving_version"):
        assert constraint in text, (
            f"no test names {constraint}, so a migration that dropped it would "
            f"be caught by nothing")


def test_no_real_models_licence_is_asserted_anywhere_in_the_repository():
    """**The honesty check, widened from the database to the tree.**

    MOD-01 is Phase 6 and blocking. Phase 5 may make the unrecorded state refuse
    to serve; it may not fill the field in. The database version of this test
    looks at rows — this one looks at the SQL and the fixtures, because a seed
    committed to a migration would be a legal representation nobody verified and
    would never appear in a test database until it ran.
    """
    offenders = []
    for path in list((REPO / "supracloud-jarvis" / "postgres").rglob("*.sql")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            low = line.lower()
            if "commercial_licence" not in low:
                continue
            # A column definition, a comment or a constraint is fine. A VALUES
            # row that assigns one a real licence name is not.
            for licence in ("apache", "mit ", "gemma", "llama", "bsd",
                            "gpl", "cc-by", "proprietary"):
                if licence in low and "fixture" not in low:
                    offenders.append(f"{path.name}: {line.strip()[:100]}")
    assert not offenders, (
        "a commercial licence is asserted in SQL. Determining a model's terms "
        "is MOD-01, Phase 6, and a legal determination:\n  "
        + "\n  ".join(offenders))



def test_the_gateway_resolves_the_model_before_it_calls_a_provider():
    """**The owner's rejection, as a guard.**

    Phase 5 first checked the model AFTER the call, so an unregistered model
    still ran and only its answer was discarded. Asserted on the ORDER of the
    two steps in the source, because a test that only checks the outcome would
    pass against a gateway that called the provider first and refused
    afterwards — which is exactly what it used to do.

    ── Rewritten in Phase 8, and the reason is worth recording ─────────────

    This read `IRAModelGateway.complete` and looked for `self._approve(` before
    `await provider.complete(`. Phase 8 extracted the whole admission sequence
    into `_admit` so that `complete()` and `stream()` could not drift apart —
    and the guard went red with `ValueError: substring not found`, because the
    string it looks for had moved into another method.

    The invariant was not broken; the guard's mechanism was. But "the test still
    passes" was never the property worth having, so it is not restored by
    pointing the old search at the new method. It now asserts something the
    original could not: that **both** entry points call the provider only after
    `_admit` has returned, and that `_admit` itself resolves the registry.

    Streaming did not exist when the original was written. If it had, the
    original would have said nothing about it.
    """
    import inspect

    from core.models.gateway import IRAModelGateway

    admit = inspect.getsource(IRAModelGateway._admit)
    assert "self._approve(" in admit, (
        "the admission sequence no longer resolves an approved model, so "
        "nothing decides which model may answer before a provider is called")
    assert "await provider." not in admit, (
        "the admission sequence now calls a provider. Admission is what "
        "decides whether a provider may be called at all; a call inside it is "
        "the check happening after the fact again.")

    # `complete` calls the provider itself; `stream` hands it to
    # `_metered_stream`. Both must reach `_admit` first, and the pair is named
    # explicitly so that adding a third entry point which forgets is a KeyError
    # here rather than a silent omission.
    for name, reaches_provider in (("complete", "await provider.complete("),
                                   ("stream", "self.providers[")):
        source = inspect.getsource(getattr(IRAModelGateway, name))
        admit_at = source.index("self._admit(")
        call_at = source.index(reaches_provider)
        assert admit_at < call_at, (
            f"IRAModelGateway.{name} reaches a provider before admission has "
            f"finished, so an unapproved model runs and only its answer is "
            f"thrown away")

    # And the streaming half really does call the provider, so the check above
    # is not vacuous on a `stream` that quietly stopped streaming.
    assert "provider.stream(" in inspect.getsource(
        IRAModelGateway._metered_stream)


# ─────────────────────────────────────────────────────────────────────────────
# The limitation P5-08 found — closed by Phase 8, and now asserted closed
# ─────────────────────────────────────────────────────────────────────────────


def test_the_gateway_is_on_the_live_inference_path():
    """**The inverse guard, inverted. This is what P5-08 asked for.**

    Until Phase 8 this test asserted a GAP: that nothing in the product
    constructed an `IRAModelGateway`, so "an unapproved model produces zero
    provider calls" was true of the gateway and not of IRA. Its own failure
    message said what to do when the gap closed:

        That is good news and this test is the wrong shape for it … replace
        this test with one asserting that the live path routes through it.

    So it is replaced rather than deleted, and the replacement checks the same
    seam from the other side. A deleted test would have left nothing watching a
    property three phases were built on.
    """
    composition = IRA_ROOT / "core" / "models" / "composition.py"
    assert composition.is_file(), (
        "core/models/composition.py is gone. It is the only thing that "
        "constructs a gateway; without it the live path has no chokepoint and "
        "the Phase 5, 6 and 7 guarantees describe a path nothing takes.")
    assert "IRAModelGateway(" in composition.read_text(encoding="utf-8"), (
        "the composition root no longer constructs a gateway")

    llm = (IRA_ROOT / "utils" / "llm.py").read_text(encoding="utf-8")
    assert "core.models.composition" in llm, (
        "utils/llm.py no longer reaches the gateway. The live inference path "
        "has gone back to calling a provider directly, which is the gap P5-08 "
        "found and Phase 8 closed.")

    # The direct client call must remain reachable ONLY as the no-provider
    # fallback. A second unconditional path would recreate the gap while every
    # assertion above still passed.
    assert "_complete_via_gateway" in llm and "_stream_via_gateway" in llm, (
        "utils/llm.py no longer routes its completions and streams through the "
        "gateway helpers")


