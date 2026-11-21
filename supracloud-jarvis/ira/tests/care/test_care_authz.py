"""Who may read, act and manage in a care workspace (care/authz.py).

Negative controls for each: the function must refuse the role below the line.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from care import authz
from tests.conftest import make_workspace_principal


def _p(role, *, owner=False):
    return make_workspace_principal(role=role, is_owner=owner)


@pytest.mark.parametrize("role", ["viewer", "member", "workspace_admin"])
def test_every_workspace_role_may_read(role):
    authz.ensure_can_read(_p(role))


def test_no_role_may_not_read():
    with pytest.raises(HTTPException) as exc:
        authz.ensure_can_read(_p(None))
    assert exc.value.status_code == 403


def test_viewer_may_not_act_or_manage():
    with pytest.raises(HTTPException):
        authz.ensure_can_act(_p("viewer"))
    with pytest.raises(HTTPException):
        authz.ensure_can_manage(_p("viewer"))


def test_member_may_act_but_not_manage():
    authz.ensure_can_act(_p("member"))
    with pytest.raises(HTTPException):
        authz.ensure_can_manage(_p("member"))


def test_workspace_admin_may_do_everything():
    authz.ensure_can_read(_p("workspace_admin"))
    authz.ensure_can_act(_p("workspace_admin"))
    authz.ensure_can_manage(_p("workspace_admin"))


def test_the_deployment_owner_may_do_everything_without_a_workspace_role():
    p = _p(None, owner=True)
    authz.ensure_can_read(p)
    authz.ensure_can_act(p)
    authz.ensure_can_manage(p)


def test_an_unknown_role_string_denies_everything():
    """Fail closed: a role the table does not know is not a member."""
    p = _p("superuser")
    assert not authz.can_read(p) and not authz.can_act(p) and not authz.can_manage(p)


def test_the_refusal_does_not_name_tables_or_roles():
    with pytest.raises(HTTPException) as exc:
        authz.ensure_can_manage(_p("viewer"))
    detail = exc.value.detail.lower()
    assert "workspace_admin" not in detail and "care_" not in detail
