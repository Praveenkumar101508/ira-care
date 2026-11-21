"""Startup stops on a reserved owner login, and on nothing else from provisioning.

`app.lifespan._provision_private_owner` keeps owner provisioning non-fatal, so a
read-only replica or an older schema still serves the endpoints that need no
workspace. The one exception is `ReservedOwnerLogin`: a new install whose owner
login is a name IRA reserves for its own memory must not start
(`tests/tenancy/test_reserved_owner_login.py` covers the decision itself).
"""
from __future__ import annotations

import pytest

from core.identity.provisioning import ReservedOwnerLogin


def _provisioning_raises(monkeypatch, exc: Exception) -> None:
    async def _raise():
        raise exc

    monkeypatch.setattr(
        "core.identity.provisioning.ensure_private_owner_identity", _raise)


async def test_a_reserved_owner_login_stops_startup(monkeypatch):
    from app.lifespan import _provision_private_owner

    _provisioning_raises(monkeypatch, ReservedOwnerLogin("reserved"))

    with pytest.raises(ReservedOwnerLogin):
        await _provision_private_owner()


async def test_any_other_provisioning_failure_is_logged_and_startup_continues(
        monkeypatch, caplog):
    from app.lifespan import _provision_private_owner

    _provisioning_raises(monkeypatch, RuntimeError("read-only replica"))

    await _provision_private_owner()

    assert any("read-only replica" in r.getMessage() for r in caplog.records)
