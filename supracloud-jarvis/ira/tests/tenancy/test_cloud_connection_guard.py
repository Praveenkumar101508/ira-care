"""A cloud request must never reach the database through `owner_connection`.

`owner_connection()` binds the single deterministic private tenant. In the
private edition that is correct — the subsystems using it have one owner by
construction. In the cloud edition it would hand whichever request happened to
arrive the owner's LifeOS, files, cognition and memory, which is the exact
failure the whole tenancy retrofit exists to prevent.

The helper already refuses in cloud. This file exists because a refusal in one
function is only as good as nobody routing around it, and because Batch 5B
rebound seven call sites to `owner_connection` as a **transitional** measure —
transitional being a word that means nothing unless something enforces it.

Two guards, doing different jobs:

  1. A behavioural test: the helper actually raises under the cloud edition.
     Without this, the ratchet below could pass forever while the refusal was
     quietly broken.
  2. A ratchet: the set of route modules using `owner_connection` may SHRINK
     but never GROW. The two current entries are recorded debt with a named
     owner batch, not an allowance.
"""
from __future__ import annotations

import inspect
import pathlib
import re

import pytest

from core.editions import EDITION_CLOUD, EDITION_PRIVATE

ROUTES = pathlib.Path(__file__).resolve().parents[2] / "api" / "routes"

#: Route modules that still reach the database through `owner_connection`.
#:
#: This is a DEBT LIST, not a permission list. Batch 5C rewires both onto
#: `workspace_connection` with a verified workspace, at which point they come
#: off this list and the set is empty. Adding a name here to make a new failure
#: go away is the one thing that defeats the guard — a new route with this
#: problem is a bug, not an entry.
# IRA CARE: the only entry (webhooks.py) was not carried over, so the debt is empty.
KNOWN_OWNER_CONNECTION_ROUTES: set[str] = set()


def _routes_using_owner_connection() -> set[str]:
    found = set()
    for path in ROUTES.glob("*.py"):
        src = path.read_text(encoding="utf-8")
        # Match the call, not the word: a comment or a docstring explaining why
        # a route does NOT use it must not trip the guard.
        if re.search(r"^\s*async with owner_connection\(", src, re.M):
            found.add(path.name)
    return found


# ─────────────────────────────────────────────────────────────────────────────
# 1. The refusal itself
# ─────────────────────────────────────────────────────────────────────────────


async def test_owner_connection_refuses_in_the_cloud_edition(monkeypatch):
    """The property everything else here depends on.

    Asserted by driving the helper rather than by reading its source, because
    the source could keep the check and lose the raise.
    """
    from core.identity import db_context

    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_CLOUD)

    with pytest.raises(db_context.MissingTenantContext) as e:
        async with db_context.owner_connection("guard test"):
            pass  # pragma: no cover — the context manager must not open

    message = str(e.value)
    assert "cloud edition" in message
    assert "guard test" in message, (
        "the refusal must name its caller — an operator reading this in a log "
        "needs to know which call site did it, not just that one did"
    )


async def test_owner_connection_does_not_refuse_in_the_private_edition(monkeypatch):
    """The other half. A guard that refuses everywhere would pass this file and
    break the product, so the refusal has to be edition-specific."""
    from core.identity import db_context

    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_PRIVATE)

    # It will fail on the absent pool, not on the edition check — which is the
    # distinction being asserted. Reaching `utils.db` at all means the edition
    # gate let it through.
    with pytest.raises(Exception) as e:
        async with db_context.owner_connection("guard test"):
            pass  # pragma: no cover
    assert "cloud edition" not in str(e.value)


def test_the_refusal_is_not_merely_documented():
    """`owner_connection` must contain a real edition branch that raises.

    Belt and braces with the behavioural test above: that one could be made to
    pass by a fixture, this one reads the function.
    """
    from core.identity.db_context import owner_connection

    src = inspect.getsource(owner_connection)
    assert "EDITION_CLOUD" in src and "raise" in src, (
        "owner_connection no longer refuses in cloud — that refusal is the only "
        "thing stopping a cloud request from being served the private owner's data"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. The ratchet
# ─────────────────────────────────────────────────────────────────────────────


def test_no_new_route_uses_owner_connection():
    """The set may shrink. It may not grow.

    Batch 5B bound seven call sites to `owner_connection` to keep the private
    edition working while the schema changed. Two of those are in route
    modules. They are transitional, and this is what makes that word mean
    something.
    """
    actual = _routes_using_owner_connection()
    new = actual - KNOWN_OWNER_CONNECTION_ROUTES
    assert not new, (
        f"route module(s) newly using owner_connection: {sorted(new)}.\n\n"
        "owner_connection binds the single private tenant and refuses in the "
        "cloud edition, so a cloud request reaching one of these gets an error "
        "rather than data — and in the private edition it gets the OWNER's "
        "data regardless of who asked. A route serving an authenticated user "
        "needs workspace_connection with a verified workspace. Do not add the "
        "file to KNOWN_OWNER_CONNECTION_ROUTES to silence this."
    )


def test_the_debt_list_does_not_go_stale():
    """A name that no longer uses `owner_connection` must leave the list.

    Without this the list would still read as two outstanding items long after
    Batch 5C fixed them, and a stale debt record is worse than none: it is
    trusted.
    """
    actual = _routes_using_owner_connection()
    fixed = KNOWN_OWNER_CONNECTION_ROUTES - actual
    assert not fixed, (
        f"{sorted(fixed)} no longer use owner_connection — remove them from "
        "KNOWN_OWNER_CONNECTION_ROUTES so the list keeps meaning what it says"
    )


def test_the_debt_is_shrinking_not_growing():
    """Records the size so the direction of travel is visible in one number."""
    assert len(KNOWN_OWNER_CONNECTION_ROUTES) <= 1, (
        "the owner_connection debt in route modules has grown beyond what "
        "Batch 5B left behind"
    )
