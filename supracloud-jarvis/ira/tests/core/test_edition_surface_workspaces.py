"""Workspace management is a cloud surface. The picker is not.

WHAT THIS IS FOR

Creating, renaming and leaving a workspace are tenant-shaped acts. Each derives
its authority from a rule about an organisation with more than one person in
it — `INVITE_CAPABLE_ROLES` for create, `workspace.manage_settings` for rename,
and for leave a guard against stranding the last administrator. A private
install has one owner and a deterministic tenant, and no client in this
repository calls any of the three.

Worse than unused: in private, a request that names no workspace falls back to
`legacy_owner_workspace()`, which serves the migration workspace specifically.
So a second workspace created through the API is one the owner's own client
would never open. The surface could manufacture state the product has no way to
reach, which is what moved this from tidying to a defect.

THE HALF THAT MATTERS MORE

`test_the_picker_survives_in_both_editions` is the guard, not the gate. Listing
and selecting must stay mounted everywhere: `WorkspaceGate` calls the listing on
every sign-in, and it is that response's `selection_required` that tells a
client whether the cloud rule applies at all. Gating the picker alongside the
management verbs — the obvious reading of "gate /api/v1/workspaces" — would
break the private product to remove something the private product uses, and
`tests/tenancy/test_workspace_scope_boundary.py` names both paths as routes that
must work before a workspace exists.

The two halves are asserted in one file deliberately. Whoever later decides the
whole prefix should move will read the reason it did not.
"""
from __future__ import annotations

import pytest

from tests.core.edition_surfaces import method_paths
from tests.route_introspection import assert_enumeration_is_not_empty


def _app(monkeypatch, edition: str | None):
    if edition is None:
        monkeypatch.delenv("IRA_EDITION", raising=False)
    else:
        monkeypatch.setenv("IRA_EDITION", edition)
        #: Cloud refuses to start under DEV_MODE; that guard has its own tests
        #: and tripping it here would fail this file for an unrelated reason.
        monkeypatch.delenv("DEV_MODE", raising=False)

    from app.factory import create_app

    app = create_app()
    #: Prove the enumeration found something before asserting anything is
    #: absent from it — an empty list satisfies every absence assertion.
    assert_enumeration_is_not_empty(app)
    return app


#: What a client must be able to do in EVERY edition, or it cannot start.
PICKER = (
    ("GET", "/api/v1/workspaces"),
    ("POST", "/api/v1/workspaces/select"),
)

#: What only the cloud edition publishes.
MANAGEMENT = (
    ("POST", "/api/v1/workspaces"),
    ("PATCH", "/api/v1/workspaces/{workspace_id}"),
    ("DELETE", "/api/v1/workspaces/{workspace_id}/membership"),
)


@pytest.mark.parametrize("edition", [None, "cloud"], ids=["private", "cloud"])
def test_the_picker_survives_in_both_editions(monkeypatch, edition):
    """The half that would break the product if it were got wrong."""
    served = method_paths(_app(monkeypatch, edition))

    missing = [f"{m} {p}" for m, p in PICKER if (m, p) not in served]
    assert not missing, (
        f"the {edition or 'private'} edition no longer serves {missing}. A "
        f"client cannot send X-Workspace-ID before it knows which workspaces "
        f"exist, and it is the listing's `selection_required` that tells it "
        f"whether it needs to — so gating these makes the product unable to "
        f"bootstrap. Gate the three management verbs, never the picker."
    )


def test_private_does_not_publish_workspace_management(monkeypatch):
    served = method_paths(_app(monkeypatch, None))

    present = [f"{m} {p}" for m, p in MANAGEMENT if (m, p) in served]
    assert not present, (
        f"the private edition published workspace management: {present}. A "
        f"single-owner install has one deterministic tenant, and a workspace "
        f"created here is one the unheadered fallback would never serve back."
    )


def test_cloud_does_publish_workspace_management(monkeypatch):
    """The complement, and it is not decoration.

    Without it the test above would pass just as happily if the three verbs had
    been deleted outright — a regression satisfying the check by removing the
    feature, which is how a gate becomes a green tick over nothing.
    """
    served = method_paths(_app(monkeypatch, "cloud"))

    missing = [f"{m} {p}" for m, p in MANAGEMENT if (m, p) not in served]
    assert not missing, (
        f"the cloud edition does NOT serve {missing}. Either the mount is on "
        f"the wrong side of the condition, or the verbs have been removed."
    )


def test_the_switch_is_declared_where_every_other_edition_difference_lives():
    """Not an `if is_cloud()` buried in a router file.

    `core/editions.py` exists so the differences between the editions can be
    read in one table rather than hunted for. A gate that works but is declared
    elsewhere is a gate the next person will not find — and `GET /api/v1/edition`
    derives its answer from that table, so a difference missing from it is also
    a difference no client is told about.
    """
    from core.editions import (
        EDITION_CLOUD, EDITION_PRIVATE, edition_default, edition_default_keys,
    )

    assert "workspace_management_available" in edition_default_keys()
    assert edition_default("workspace_management_available", EDITION_PRIVATE) is False
    assert edition_default("workspace_management_available", EDITION_CLOUD) is True


def test_the_gated_verbs_are_the_ones_the_router_split_off(monkeypatch):
    """The declaration and the code are the same three verbs.

    `MANAGEMENT` above is a hand-written list, so on its own it proves only that
    those three behave as expected — a fourth verb added to `management_router`
    would be gated silently and no test would mention it. This reads the router
    instead, so the list cannot fall behind the module.
    """
    from api.routes.workspaces import management_router
    from tests.route_introspection import iter_routes

    actual = {
        (method, f"/api/v1{path}")
        for route, path in iter_routes(management_router)
        for method in getattr(route, "methods", ()) or ()
        if method not in {"HEAD", "OPTIONS"}
    }

    assert actual == set(MANAGEMENT), (
        f"the cloud-only router now serves {sorted(actual)}, while this file "
        f"asserts against {sorted(MANAGEMENT)}. A verb was added to or removed "
        f"from the gated router without the tests following it."
    )
