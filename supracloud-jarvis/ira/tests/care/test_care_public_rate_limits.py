"""The care routes anyone can call without signing in are rationed per client address.

`/care/devices/pair` exchanges a code for a device token: the code is the only credential,
so guesses must be limited. `/care/signup` creates rows. Both answer 429 past their budget,
and a refused guess never reaches the database."""
from __future__ import annotations

import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

pytest.importorskip("slowapi")

from fastapi.testclient import TestClient  # noqa: E402

from care import routes  # noqa: E402


def _client(monkeypatch):
    monkeypatch.setenv("IRA_CARE_ALLOWED_ORIGINS", "")
    from care.server import create_care_app
    return TestClient(create_care_app())   # no `with`: the lifespan (database, jobs) is not started


def _budget(limit: str) -> int:
    return int(limit.split("/")[0])


def test_pairing_guesses_are_rationed_per_address(monkeypatch):
    looked_up = []

    async def no_such_code(code, app_version=None):
        looked_up.append(code)
        return None

    monkeypatch.setattr(routes.repo, "pair_device", no_such_code)
    client = _client(monkeypatch)
    budget = _budget(routes.PAIR_LIMIT)
    codes = [client.post("/api/v1/care/devices/pair", json={"pairing_code": f"cp1.x.GUESS{i:03d}"}).status_code
             for i in range(budget + 5)]
    assert codes[:budget] == [404] * budget, "within the budget a wrong code is simply not recognised"
    assert set(codes[budget:]) == {429}, "past it, every further guess is refused"
    assert len(looked_up) == budget, "a refused guess never reaches the database"


def test_signup_is_rationed_per_address(monkeypatch):
    calls = []

    async def fake_signup(email, password):
        calls.append(email)
        return {"status": "check your email"}

    monkeypatch.setattr(routes.onboarding, "signup", fake_signup)
    client = _client(monkeypatch)
    budget = _budget(routes.SIGNUP_LIMIT)
    codes = [client.post("/api/v1/care/signup", json={"email": f"founder{i}@example.invalid", "password": "a long enough passphrase 1"}).status_code
             for i in range(budget + 2)]
    assert codes[:budget] == [202] * budget
    assert set(codes[budget:]) == {429}
    assert len(calls) == budget
