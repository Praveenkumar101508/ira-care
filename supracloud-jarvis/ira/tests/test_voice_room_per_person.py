"""Each person's LiveKit voice session is their own room.

`GET /api/v1/voice/token` gave every caller a token for one room,
`livekit_room_name` ("ira-voice"), with the right to listen and to speak. A
household account is allowed that route (`SCOPE_VOICE`), so it could join the
room while the owner was talking and hear both the owner's microphone and IRA's
spoken answers. Speaking is gated by the voice biometrics on the agent;
listening is not gated at all.

The voice agent (`voice/agent.py`) is dispatched per room and reads the room
from its job, and the frontend uses only the token and the URL, so a room per
person needs nothing else to change.

The route's docstring also promised a one-hour token. Without `with_ttl` the
library's default applies, which is six hours.

Each attack test fails on `main` at 310e8c6; that run is its negative control.

`livekit.api` is replaced by a recorder: the tests read the grant the route
asks for, not a signed token, so they need neither the package nor a secret.
"""
from __future__ import annotations

import datetime
import os
import sys
import types

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")


@pytest.fixture
def livekit(monkeypatch):
    """A stand-in `livekit.api` that records each token the route builds."""
    issued: list[dict] = []

    class VideoGrants:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class AccessToken:
        def __init__(self, key, secret):
            self.claims = {"key": key, "ttl": None}
            issued.append(self.claims)

        def with_identity(self, identity):
            self.claims["identity"] = identity
            return self

        def with_name(self, name):
            self.claims["name"] = name
            return self

        def with_grants(self, grants):
            self.claims["grants"] = grants
            return self

        def with_ttl(self, ttl):
            self.claims["ttl"] = ttl
            return self

        def to_jwt(self):
            return "stand-in.jwt"

    api = types.ModuleType("livekit.api")
    api.AccessToken, api.VideoGrants = AccessToken, VideoGrants
    monkeypatch.setitem(sys.modules, "livekit", types.ModuleType("livekit"))
    monkeypatch.setitem(sys.modules, "livekit.api", api)

    from config import get_settings

    monkeypatch.setattr(get_settings(), "livekit_api_key", "stand-in-key", raising=False)
    monkeypatch.setattr(get_settings(), "livekit_api_secret", "stand-in-value", raising=False)
    return issued


async def _token_for(user: str):
    from api.routes.voice import get_livekit_token

    return await get_livekit_token(_user=user)


async def test_a_person_gets_a_voice_token(livekit):
    """The control: the route works, and the grant joins a room."""
    response = await _token_for("admin")
    grants = livekit[-1]["grants"]
    assert response.token == "stand-in.jwt"
    assert grants.room_join and grants.can_subscribe and grants.can_publish
    assert response.room == grants.room


async def test_the_same_person_gets_the_same_room(livekit):
    """The control for reconnecting: one person's room does not move."""
    first = (await _token_for("admin")).room
    second = (await _token_for("admin")).room
    assert first == second


async def test_two_people_never_share_a_room(livekit):
    """Checked on the grant, which is what LiveKit enforces; the response's
    `room` field is only informational."""
    await _token_for("admin")
    await _token_for("pda-granted")
    owner, household = livekit[-2]["grants"].room, livekit[-1]["grants"].room
    assert owner != household, (
        f"the owner and a household account were both granted {owner!r}, "
        "where each can hear the other")


async def test_the_room_name_does_not_spell_out_the_account(livekit):
    """Room names are visible to anything that lists rooms."""
    room = (await _token_for("pda-granted")).room
    assert "pda-granted" not in room


async def test_the_token_lasts_at_most_an_hour(livekit):
    await _token_for("admin")
    ttl = livekit[-1]["ttl"]
    assert ttl is not None, "no lifetime was set, so the library's six-hour default applies"
    assert ttl <= datetime.timedelta(hours=1)
