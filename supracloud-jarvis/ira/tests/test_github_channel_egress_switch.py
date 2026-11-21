"""The GitHub research channel asks the egress master switch before it contacts
api.github.com.

`channels/github.py` is the one research channel that calls a public third party
(api.github.com) with no configuration and no switch check. Its `check()` runs
on the fan-out behind the UNAUTHENTICATED `GET /health/detail`
(`api/routes/health.py` → `channels.doctor()`), so on `main` at `6b46efb` an
anonymous visitor made a local-first install phone out to GitHub even with
`IRA_ALLOW_EXTERNAL_API=false` — reproduced in
docs/testing/evidence/github-channel-egress-switch-2026-10-21/.

Each "off" test fails on `main` (its negative control); the "on" control passes
on both. No real network: httpx is replaced by a recorder.
"""
from __future__ import annotations

import asyncio
import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD"):
    os.environ.setdefault(_k, "test-placeholder")

import channels.github as ghmod


class _FakeResp:
    status_code = 200

    def json(self):
        return {"items": []}

    def raise_for_status(self):
        return None

    @property
    def text(self):
        return ""


class _FakeClient:
    attempts: list[str] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False

    async def get(self, url, **k):
        _FakeClient.attempts.append(url)
        return _FakeResp()


@pytest.fixture
def recorder(monkeypatch):
    _FakeClient.attempts = []
    monkeypatch.setattr(ghmod.httpx, "AsyncClient", _FakeClient)
    return _FakeClient.attempts


def _set_switch(monkeypatch, allowed: bool):
    import reasoning.api_consent as ac
    monkeypatch.setattr(ac, "external_api_allowed", lambda: allowed)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Switch OFF: no outbound, gated status (attack; fails on main) ─────────────

def test_check_makes_no_outbound_when_switch_off(monkeypatch, recorder):
    _set_switch(monkeypatch, False)
    status = _run(ghmod.GitHubChannel().check())
    assert recorder == [], f"github check phoned out with the switch off: {recorder}"
    assert status.ok is False
    assert "external APIs off" in status.detail


def test_read_and_search_make_no_outbound_when_switch_off(monkeypatch, recorder):
    _set_switch(monkeypatch, False)
    r = _run(ghmod.GitHubChannel().read("https://github.com/owner/repo"))
    s = _run(ghmod.GitHubChannel().search("anything"))
    assert recorder == [], f"github read/search phoned out with the switch off: {recorder}"
    assert "external APIs off" in r and "external APIs off" in s


def test_fail_closed_when_switch_unreadable(monkeypatch, recorder):
    """The control for the fail-closed path: if the switch cannot be read, treat
    it as off."""
    import reasoning.api_consent as ac

    def _boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(ac, "external_api_allowed", _boom)
    status = _run(ghmod.GitHubChannel().check())
    assert recorder == [], "github check phoned out when the switch could not be read"
    assert status.ok is False


# ── Switch ON: the probe runs (control; passes on main too) ───────────────────

def test_check_probes_when_switch_on(monkeypatch, recorder):
    _set_switch(monkeypatch, True)
    status = _run(ghmod.GitHubChannel().check())
    assert any("api.github.com/rate_limit" in u for u in recorder), \
        "with external APIs on, the health probe should contact GitHub"
    assert status.ok is True
