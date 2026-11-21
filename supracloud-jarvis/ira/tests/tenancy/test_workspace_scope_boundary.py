"""Workspace selection is required by workspace-scoped routes and by no others.

Batch 5C makes a cloud request name its workspace. That requirement has an
obvious failure mode in the safe direction and a less obvious one in the
dangerous direction, and this file guards both ends.

**Too broad.** If workspace resolution became a global dependency — a router
default, a middleware, or simply the habit of adding it everywhere — then
health, readiness, login, workspace *listing* and initial provisioning would
all demand a workspace before a caller could possibly have one. Sign-in would
require the thing sign-in produces. That is not a security failure, it is an
outage, and it is the kind that looks correct in review because every route
"has the check".

**Too narrow.** If a route that touches one of the seventeen workspace-scoped
tables does NOT resolve a workspace, it reaches customer data unscoped. The
positive control below exists so this file cannot pass by asserting nothing.

Neither list is maintained by hand as an allowance. `MUST_NOT_REQUIRE` is the
set of paths whose whole purpose is to work before a workspace exists;
`MUST_REQUIRE` is the set already rewired, and it grows as Batch 5C proceeds.
"""
from __future__ import annotations

import os
import sys

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

# Assembling the real app pulls memory.embeddings and worker.notifier. conftest
# stubs those modules but leaves attributes the import chain references unset;
# fill them in exactly as tests/test_app_factory.py does. Nothing here
# instantiates them — the app is built to be READ, not run.
_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    _st.SentenceTransformer = object
    _st.CrossEncoder = object
_wn = sys.modules.get("worker.notifier")
if _wn is not None and not hasattr(_wn, "REDIS_NOTIFY_CHANNEL"):
    _wn.REDIS_NOTIFY_CHANNEL = "ira:notify"
    # 3C-03 gave the notifier a per-tenant channel; the stub needs
    # the resolver too, because api/routes/notifications.py now
    # imports it rather than the bare constant.
    _wn.notify_channel = lambda principal=None: "ira:notify"

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("slowapi")

from api.middleware.workspace import require_workspace_principal  # noqa: E402

#: Paths that must remain reachable with no workspace selected, because a
#: caller cannot have selected one yet — or because no customer data is
#: involved at all. A route added here is a claim that it touches none of the
#: seventeen workspace-scoped tables.
MUST_NOT_REQUIRE = (
    "/livez",                 # liveness — never touches a dependency
    "/readyz",                # readiness — schema state, not customer data
    "/health",
    "/health/detail",
    "/auth/token",            # sign-in: produces the identity a workspace needs
    "/auth/refresh",
    "/auth/logout",
    "/auth/totp/verify",      # second factor, still mid-authentication
    "/api/v1/access/users",   # the membership view a picker reads
    "/api/v1/workspaces",         # the picker itself
    "/api/v1/workspaces/select",  # verifying one selection, before acting in it
)

#: Routes Batch 5C has bound. This is a positive control: without it, deleting
#: the dependency from every route in the application would leave this file
#: entirely green.
MUST_REQUIRE = (
    # 3C-10: these read and clear the `notifications` table, which `051` gave a
    # tenant and RLS. Before that they opened a bare connection and selected
    # with no predicate at all.
    # Mounted without the /api/v1 prefix, unlike most routers.
    "/notifications",
    "/notifications/read",
    "/notifications/{notif_id}/read",
    "/api/v1/memory",
    "/api/v1/memory/{memory_id}",
    "/api/v1/memory/{memory_id}/pin",
)


@pytest.fixture(scope="module")
def app():
    from app.factory import create_app

    return create_app()


def _dependency_calls(route) -> set:
    """Every dependency callable a route resolves, at any depth.

    Walks the tree rather than reading the signature, because a dependency
    nested inside another one (`_owner_workspace` wraps
    `require_workspace_principal`) is just as binding as one written on the
    handler — and is exactly what a signature check would miss.
    """
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return set()

    found, stack = set(), [dependant]
    while stack:
        node = stack.pop()
        if node.call is not None:
            found.add(node.call)
        stack.extend(node.dependencies)
    return found


def _routes_by_path(app) -> dict:
    out = {}
    from tests.route_introspection import iter_routes, assert_enumeration_is_not_empty
    assert_enumeration_is_not_empty(app)
    # P16-R1b: key on the EFFECTIVE path. Under FastAPI 0.141 `route.path` is
    # the path as written inside its router, without the include prefix, so
    # keying on it made every /api/v1/... lookup miss.
    for route, effective_path in iter_routes(app):
        if effective_path:
            out.setdefault(effective_path, []).append(route)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Too broad
# ─────────────────────────────────────────────────────────────────────────────


def test_the_routes_that_precede_a_workspace_do_not_demand_one(app):
    """Health, auth and the workspace picker must work with no workspace.

    The picker especially: a route that requires a selected workspace in order
    to tell you which workspaces you may select cannot be used to select one.
    """
    routes = _routes_by_path(app)

    # Every listed path must EXIST. Five of the eight originally written here
    # did not — `/auth/login`, `/metrics`, `/health/ready` and two others were
    # plausible names for routes this application does not have — so the loop
    # below skipped them and the test passed while checking almost nothing.
    # A rename must break this list loudly rather than empty it silently.
    absent = [p for p in MUST_NOT_REQUIRE if p not in routes]
    assert not absent, (
        f"{absent} are not routes in this application, so listing them here "
        f"guards nothing. Correct the paths — do not delete them."
    )

    offenders = []
    for path in MUST_NOT_REQUIRE:
        for route in routes.get(path, []):
            if require_workspace_principal in _dependency_calls(route):
                offenders.append(f"{sorted(getattr(route, 'methods', []))} {path}")

    assert not offenders, (
        f"these routes now require a workspace and must not: {offenders}.\n\n"
        "A caller reaches them BEFORE a workspace exists — sign-in, the "
        "readiness probe, the workspace picker itself. Requiring one here "
        "makes the application unable to bootstrap: the check looks right on "
        "every route and the product cannot start."
    )


def test_no_global_dependency_or_middleware_forces_a_workspace(app):
    """The mechanism, not just the symptom.

    The list above enumerates paths, so it can only catch what it names. This
    catches the change that would break every unnamed one at once: a workspace
    dependency attached to the application or to a router as a default.
    """
    from fastapi.dependencies.utils import get_dependant

    class _Holder:
        def __init__(self, dependant):
            self.dependant = dependant

    app_level = set()
    for dep in getattr(app.router, "dependencies", None) or []:
        app_level |= _dependency_calls(
            _Holder(get_dependant(path="/", call=dep.dependency))
        )

    assert require_workspace_principal not in app_level, (
        "workspace resolution is attached as an application-level dependency. "
        "It must be declared by the routes that need it, so that adding a "
        "route which does not touch workspace-scoped tables does not silently "
        "acquire the requirement."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Too narrow
# ─────────────────────────────────────────────────────────────────────────────


def test_the_rewired_routes_actually_require_a_workspace(app):
    """Positive control. Without this the file passes on an empty application."""
    routes = _routes_by_path(app)
    missing = []
    for path in MUST_REQUIRE:
        matched = routes.get(path, [])
        if not matched:
            missing.append(f"{path} (route not found — has it been renamed?)")
            continue
        if not any(require_workspace_principal in _dependency_calls(r) for r in matched):
            missing.append(path)

    assert not missing, (
        f"these routes touch workspace-scoped tables and no longer resolve a "
        f"workspace: {missing}. They would reach customer data on a connection "
        f"with no workspace bound."
    )


# ─────────────────────────────────────────────────────────────────────────────
# The completion guard
# ─────────────────────────────────────────────────────────────────────────────

#: The transitional test that records the cross-workspace gap while the call
#: sites are being rewired. It asserts that workspace A CAN read workspace B,
#: which is true today and must stop being true.
TRANSITIONAL_TEST = "test_cross_workspace_read_is_not_refused_yet"

#: The predicate whose appearance in a migration means workspace isolation is
#: being enforced by the database.
WORKSPACE_PREDICATE = "app.workspace_id"


def _policies_read_the_workspace_guc() -> list[str]:
    """Migrations that put `app.workspace_id` inside a policy.

    Matched against `CREATE POLICY` blocks rather than the whole file, because
    `045` mentions the GUC in a column DEFAULT and in prose explaining why it
    is deliberately NOT in a policy. A file-wide grep would read that comment
    as the thing it warns against.
    """
    import pathlib
    import re

    sql_dir = pathlib.Path(__file__).resolve().parents[3] / "postgres"
    hits = []
    for path in sorted(sql_dir.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        for block in re.findall(
            r"CREATE\s+POLICY\b.*?;", text, re.S | re.I
        ):
            if WORKSPACE_PREDICATE in block:
                hits.append(path.name)
                break
    return hits


def test_the_transitional_vulnerability_test_does_not_outlive_the_policies():
    """When workspace RLS lands, the test that documents its absence must go.

    A test asserting "A can read B" is a useful record during a migration and a
    liability afterwards: it would keep passing only if the isolation were
    broken, and its name would still read as an accepted state of affairs. So
    the two are wired together — the moment a policy reads `app.workspace_id`,
    this fails until the transitional test is replaced by the denial tests.

    The replacement is not "delete it". It is:

      * same tenant, workspace A cannot READ workspace B
      * same tenant, workspace A cannot UPDATE or DELETE workspace B
      * cross-tenant access refused
      * an unbound connection can neither read nor write workspace data
      * a worker with missing or revoked workspace context cannot execute
    """
    import pathlib

    suite = pathlib.Path(__file__).with_name("test_phase3b_batch5c.py")
    # `def <name>` rather than the bare name. The test is gone; what remains are
    # a docstring and a comment explaining what replaced it, and a substring
    # match reads those as the test still being present — flagging a file for
    # documenting its own history.
    transitional_present = (
        suite.exists()
        and f"def {TRANSITIONAL_TEST}" in suite.read_text(encoding="utf-8")
    )
    enforcing = _policies_read_the_workspace_guc()

    assert not (enforcing and transitional_present), (
        f"{enforcing} now put {WORKSPACE_PREDICATE!r} inside an RLS policy, so "
        f"workspace isolation is enforced — but {TRANSITIONAL_TEST!r} is still "
        f"in tests/tenancy/test_phase3b_batch5c.py asserting that a workspace "
        f"CAN read another's rows.\n\n"
        f"Replace it with the denial tests listed in this test's docstring. "
        f"Deleting it without adding them removes the record AND the proof."
    )


def test_the_completion_guard_is_not_vacuous():
    """The guard above only bites if its scanner can actually see a policy.

    A regex that matched nothing would make the guard permanently, silently
    green — the worst outcome for a guard, because it reads as a passing check.
    So this asserts the exact mechanism the guard relies on: that a
    `CREATE POLICY` block is found, and that a GUC inside one is visible from
    within the block. `app.tenant_id` is the stand-in, because it is the same
    shape as the `app.workspace_id` the guard is waiting for and it is already
    there to be found.
    """
    import pathlib
    import re

    sql_dir = pathlib.Path(__file__).resolve().parents[3] / "postgres"
    blocks = []
    for path in sorted(sql_dir.glob("*.sql")):
        blocks += re.findall(
            r"CREATE\s+POLICY\b.*?;", path.read_text(encoding="utf-8"), re.S | re.I
        )

    assert len(blocks) >= 40, (
        f"the scanner found only {len(blocks)} CREATE POLICY blocks across the "
        f"migrations — it is not reading what it thinks it is reading"
    )
    assert any("app.tenant_id" in b for b in blocks), (
        "no CREATE POLICY block contains a GUC reference, so the block-level "
        "match is not capturing policy bodies — the completion guard would "
        "never see app.workspace_id either"
    )
    # The assertion this test was missing, and the reason it mattered.
    #
    # `049` creates the seventeen tables' workspace policies inside a DO block,
    # by `EXECUTE format(...)` over a table list — so there is no literal
    # `CREATE POLICY ... app.workspace_id` anywhere in it. The scanner above
    # therefore returned EMPTY from 049 until `051` (3C-10) wrote its policies
    # out longhand, which meant the completion guard was **silently vacuous for
    # two migrations**: permanently green, watching for something it could not
    # see.
    #
    # The two checks above did not catch that. They prove the scanner can find
    # policy blocks and can see a GUC inside one — both true throughout — while
    # the specific GUC the guard waits for was invisible. So this asserts the
    # exact thing, not a proxy for it.
    assert _policies_read_the_workspace_guc(), (
        "no migration contains a literal CREATE POLICY reading "
        f"{WORKSPACE_PREDICATE!r}, so the completion guard above can never "
        "fire. Note that a policy created dynamically — `EXECUTE format(...)` "
        "inside a DO block, as 049 does — is invisible to this scanner, and a "
        "guard that cannot see is worse than one that is absent."
    )
