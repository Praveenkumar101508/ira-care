"""Staff roles (care/authz.py STAFF_ROLES, ACTIONS, permissions_for; D-078).

The permissions the dashboard is told about must be exactly the permissions the routes
enforce. Each action names its routes and its level; this reads every one of those routes'
source and checks it calls the matching `ensure_can_<level>`, so the table cannot drift from
the code. Then each role's answer is checked against the three levels: nothing is widened.
"""
from __future__ import annotations

import inspect

import pytest

from care import authz, routes
from tests.conftest import make_workspace_principal


def _endpoints() -> dict[str, object]:
    out = {}
    for r in routes.router.routes:
        for m in r.methods - {"HEAD", "OPTIONS"}:
            out[f"{m} {r.path}"] = r.endpoint
    return out


def test_every_action_names_real_routes_that_check_the_stated_level():
    eps = _endpoints()
    wrong = []
    for action, (level, keys) in authz.ACTIONS.items():
        assert level in ("read", "act", "manage"), action
        for key in keys:
            assert key in eps, f"{action}: no route {key}"
            src = inspect.getsource(eps[key])
            if f"ensure_can_{level}(" not in src:
                wrong.append(f"{action}: {key} does not call ensure_can_{level}")
    assert not wrong, "\n".join(wrong)


def test_the_care_plan_is_written_by_managers_only():
    assert authz.ACTIONS["edit_care_plan"][0] == "manage"
    assert authz.ACTIONS["view_care_plan"][0] == "read"


@pytest.mark.parametrize("role,label,act,manage", [
    ("workspace_admin", "Care manager", True, True),
    ("member", "Carer", True, False),
    ("viewer", "Viewer", False, False),
])
def test_each_staff_role_gets_exactly_its_level(role, label, act, manage):
    perms = authz.permissions_for(make_workspace_principal(role=role, is_owner=False))
    assert perms["role"] == role and perms["label"] == label
    assert perms["can_read"] is True and perms["can_act"] is act and perms["can_manage"] is manage
    assert perms["sees_everyone"] is manage
    for name, (level, _) in authz.ACTIONS.items():
        expected = {"read": True, "act": act, "manage": manage}[level]
        assert perms["actions"][name] is expected, (role, name)


def test_no_role_may_do_nothing():
    perms = authz.permissions_for(make_workspace_principal(role=None, is_owner=False))
    assert perms["role"] is None and perms["label"] == "No access"
    assert not perms["can_read"] and not any(perms["actions"].values())


def test_the_deployment_owner_is_a_care_manager():
    perms = authz.permissions_for(make_workspace_principal(role=None, is_owner=True))
    assert perms["label"] == "Care manager" and all(perms["actions"].values())
