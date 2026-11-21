"""The owner gates in the graph and the chat route honour DEV_MODE only where the edition does.

P2-02 routed the four authenticating reads of `Settings.dev_mode` through
`dev_mode_active()`, which answers False for any edition but `private`. Two reads
were left on the raw flag and listed in the tree guard's NON_SECURITY allow-list
as "model routing" and "already owner-gated":

    agents/graph.py::biometric_gate     if cfg.dev_mode: return {... "is_owner": True, "clearance_level": "admin"}
    api/routes/chat.py                  if not cfg.dev_mode and is_restricted_domain(...) and not owner:

Both decide authority — the first one literally grants owner clearance. Today
they are not reachable in cloud only because `config.py::_resolve_edition`
refuses `DEV_MODE=true` on `IRA_EDITION=cloud` at settings load, which is one
validator standing between a stranger and the owner's clearance. These tests
make the gates themselves edition-aware, and take the two files off the
allow-list so the tree guard sees them again.

Negative controls (planted and watched fail): put `if cfg.dev_mode:` back in
`biometric_gate` → `test_cloud_dev_mode_does_not_make_a_stranger_the_owner`
fails and `test_dev_mode_edition_gate.py::test_no_new_module_decides_authority_from_the_raw_dev_mode_flag`
names `agents/graph.py`; put `cfg.dev_mode` back in the chat route → the shape
test below and the tree guard both fail.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import re
import types
from typing import Any, cast

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from core.editions import EDITION_CLOUD, EDITION_PRIVATE  # noqa: E402

_IRA = pathlib.Path(__file__).resolve().parents[1]

#: A query the owner gate classifies as restricted (`security/owner_gate.py`, "lock down").
RESTRICTED_QUERY = "lock down the house and scan the network for intrusions"


def _settings(dev_mode: bool):
    return types.SimpleNamespace(dev_mode=dev_mode, owner_name="The Owner")


@pytest.fixture
def dev_mode_on(monkeypatch):
    """`Settings.dev_mode` is true everywhere it is read."""
    import agents.graph as graph
    import api.middleware.auth as auth

    monkeypatch.setattr(graph, "get_settings", lambda: _settings(True))
    monkeypatch.setattr(auth, "get_settings", lambda: _settings(True))


def _gate(state: dict[str, Any]) -> Any:
    """Run the gate on a partial state, as the graph's first node sees one."""
    from agents.graph import biometric_gate
    return asyncio.run(biometric_gate(cast(Any, state)))


def test_cloud_dev_mode_does_not_make_a_stranger_the_owner(monkeypatch, dev_mode_on):
    monkeypatch.setenv("IRA_EDITION", EDITION_CLOUD)
    out = _gate({"user_query": RESTRICTED_QUERY, "is_owner": False})
    assert out["active_agent"] == "access_denied", (
        "in the cloud edition DEV_MODE handed a non-owner the owner's clearance")
    assert out.get("clearance_level") != "admin"


def test_private_dev_mode_still_bypasses_the_gate(monkeypatch, dev_mode_on):
    """The convenience on one person's machine is unchanged."""
    monkeypatch.setenv("IRA_EDITION", EDITION_PRIVATE)
    out = _gate({"user_query": RESTRICTED_QUERY, "is_owner": False})
    assert out["is_owner"] is True
    assert out["clearance_level"] == "admin"
    assert "active_agent" not in out


def test_the_gate_itself_is_unchanged_with_dev_mode_off(monkeypatch):
    import agents.graph as graph
    import api.middleware.auth as auth

    monkeypatch.setattr(graph, "get_settings", lambda: _settings(False))
    monkeypatch.setattr(auth, "get_settings", lambda: _settings(False))
    monkeypatch.setenv("IRA_EDITION", EDITION_PRIVATE)

    denied = _gate({"user_query": RESTRICTED_QUERY, "is_owner": False})
    assert denied["active_agent"] == "access_denied"
    assert "The Owner" in denied["final_response"]

    allowed = _gate({"user_query": RESTRICTED_QUERY, "is_owner": True})
    assert "active_agent" not in allowed

    harmless = _gate({"user_query": "what is the weather like today", "is_owner": False})
    assert "active_agent" not in harmless


def test_the_chat_route_reads_dev_mode_through_the_edition_gate():
    """`chat.py` builds a streaming response around the check, so the gate is
    asserted on its shape, as `test_dev_mode_edition_gate.py` does for `voice.py`."""
    source = (_IRA / "api" / "routes" / "chat.py").read_text(encoding="utf-8")
    assert "dev_mode_active()" in source
    assert not re.search(r"(?<!\w)cfg\.dev_mode", source), (
        "the chat route's restricted-domain gate reads the raw dev-mode flag again")


def test_the_two_gates_are_no_longer_excused_from_the_tree_guard():
    from tests.test_dev_mode_edition_gate import NON_SECURITY

    assert "agents/graph.py" not in NON_SECURITY, "graph.py's read grants owner clearance; it is not 'model routing'"
    assert "api/routes/chat.py" not in NON_SECURITY, "chat.py's read is a security gate; it is not excused"
