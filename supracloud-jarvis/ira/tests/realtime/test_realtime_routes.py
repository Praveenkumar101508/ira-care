"""Phase 6 — the realtime API's contract, with the disclosure as the subject.

Handlers are called directly, as the rest of this suite does, so the assertions
are about the route's own behaviour rather than about FastAPI's plumbing.

The properties that matter here are mostly negative:

  * no credential appears in any response that is not the one-shot join grant;
  * no response claims end-to-end encryption, stored audio or a transcript;
  * a deployment with no provider gets a truthful `available: false` rather
    than an error, and still gets a usable session;
  * the disclosure travels WITH the session rather than living behind a
    separate endpoint a client could neglect to call.

Persistence is patched out — the store is exercised against a real schema in
tests/tenancy/test_realtime_isolation_rls.py.
"""
from __future__ import annotations

import uuid

import pytest

from api.routes import realtime as route
from core.realtime.capability import FLOOR, CapabilityLevel

pytestmark = pytest.mark.asyncio

TENANT = uuid.UUID("f0f0f0f0-0000-4000-8000-00000000000a")
USER = uuid.UUID("f0f0f0f0-0000-4000-8000-0000000000aa")

SECRET_MARKERS = ("t0ken", "wss://", "API_SECRET", "eyJhbGciOi")


class FakePrincipal:
    tenant_id = TENANT
    user_id = USER


def _walk(value):
    """Every scalar in a nested response, so a check cannot miss a nested field."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _walk(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk(v)
    else:
        yield str(value)


@pytest.fixture(autouse=True)
def no_identity_or_database(monkeypatch):
    """Neither an identity service nor a database is available in this suite.

    P9-03 turned the route modules' private `_principal(username)` helper into a
    FastAPI dependency, so there is nothing here to patch any more: these tests
    call the route functions directly and hand them the principal the dependency
    would have resolved. That is closer to what the route now promises — it
    RECEIVES a verified principal, it does not go and find one.
    """
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("core.realtime.store.create_session", _noop)
    monkeypatch.setattr("core.realtime.store.update_session", _noop)
    monkeypatch.setattr("core.realtime.store.record_events", _noop)


# ── Capabilities ─────────────────────────────────────────────────────────────


async def test_an_unconfigured_deployment_reports_unavailable_rather_than_failing(
    monkeypatch
):
    """The state of every private-edition install. It is not an error, and the
    UI must be able to present it as "voice runs on your device"."""
    monkeypatch.delenv("IRA_REALTIME_PROVIDER", raising=False)
    payload = await route.get_capabilities(_principal=FakePrincipal())

    assert payload["available"] is False
    assert payload["provider"] == "none"
    assert "nothing is sent anywhere" in payload["reason"]


async def test_the_text_rung_is_reachable_even_with_no_provider(monkeypatch):
    """The floor needs nothing. A client shown "no rung is reachable" would
    have no reason to offer the conversation at all."""
    monkeypatch.delenv("IRA_REALTIME_PROVIDER", raising=False)
    payload = await route.get_capabilities(_principal=FakePrincipal())

    by_level = {rung["level"]: rung for rung in payload["levels"]}
    assert by_level[FLOOR.value]["reachable"] is True
    assert by_level["full_avatar"]["reachable"] is False


async def test_the_full_ladder_is_published_with_its_budgets(monkeypatch):
    """Served rather than hard-coded in the client, so the UI cannot offer a
    rung the backend does not have — or quote a budget it does not measure."""
    monkeypatch.delenv("IRA_REALTIME_PROVIDER", raising=False)
    payload = await route.get_capabilities(_principal=FakePrincipal())

    assert len(payload["levels"]) == 4
    voice = next(r for r in payload["levels"] if r["level"] == "voice_only")
    assert voice["target_first_audio_ms"] == 900
    text = next(r for r in payload["levels"] if r["level"] == "text_only")
    assert text["target_first_audio_ms"] is None, (
        "the silent rung must not quote a budget — a number reads as a target "
        "it can never miss"
    )


async def test_the_reconnect_plan_is_finite_and_published(monkeypatch):
    """A client that cannot see the retry budget will invent its own."""
    monkeypatch.delenv("IRA_REALTIME_PROVIDER", raising=False)
    payload = await route.get_capabilities(_principal=FakePrincipal())

    plan = payload["reconnect"]
    assert plan["planned_delays_ms"], "no retry plan published"
    assert len(plan["planned_delays_ms"]) <= plan["max_attempts"]
    assert sum(plan["planned_delays_ms"]) <= plan["max_total_ms"]


async def test_capabilities_states_the_disclosure_before_anything_is_pressed(
    monkeypatch
):
    monkeypatch.delenv("IRA_REALTIME_PROVIDER", raising=False)
    disclosure = (await route.get_capabilities(_principal=FakePrincipal()))["disclosure"]

    assert disclosure["end_to_end_encrypted"] is False
    assert disclosure["audio_stored"] is False
    assert disclosure["transcript_stored"] is False
    assert "can observe it" in disclosure["note"]


# ── Opening a session ────────────────────────────────────────────────────────


async def test_opening_a_session_never_returns_an_error(monkeypatch):
    """The endpoint that must not fail. Denying the capability is correct;
    denying the conversation is not."""
    monkeypatch.delenv("IRA_REALTIME_PROVIDER", raising=False)
    payload = await route.open_session(
        route.OpenSessionBody(downlink_kbps=5000), principal=FakePrincipal()
    )
    assert payload["capability_level"] == FLOOR.value
    assert payload["is_realtime"] is False
    assert payload["reason"]
    assert "join" not in payload, "a join grant was issued with no provider"


async def test_the_disclosure_travels_with_the_session(monkeypatch):
    """Not behind a separate endpoint. A UI cannot render a live indicator
    without also having been handed the facts about where the audio goes."""
    monkeypatch.delenv("IRA_REALTIME_PROVIDER", raising=False)
    payload = await route.open_session(
        route.OpenSessionBody(downlink_kbps=5000), principal=FakePrincipal()
    )
    for field in ("media_leaves_device", "provider", "end_to_end_encrypted",
                  "audio_stored", "transcript_stored", "processing_location"):
        assert field in payload["disclosure"], f"{field} missing from the disclosure"


async def test_a_configured_deployment_returns_exactly_one_join_grant(monkeypatch):
    monkeypatch.setenv("IRA_REALTIME_PROVIDER", "livekit")
    monkeypatch.setenv("LIVEKIT_URL", "wss://example.invalid")
    monkeypatch.setenv("LIVEKIT_API_KEY", "APIkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s3cret-not-real")
    monkeypatch.setenv("IRA_REALTIME_REGION", "eu")

    payload = await route.open_session(
        route.OpenSessionBody(downlink_kbps=5000), principal=FakePrincipal()
    )
    assert payload["is_realtime"] is True
    assert payload["capability_level"] == CapabilityLevel.FULL_AVATAR.value
    assert payload["join"]["token"]
    assert payload["join"]["expires_at"]
    assert payload["disclosure"]["media_leaves_device"] is True
    assert payload["disclosure"]["end_to_end_encrypted"] is False


async def test_the_client_cannot_ask_for_a_rung_the_link_cannot_carry(monkeypatch):
    monkeypatch.delenv("IRA_REALTIME_PROVIDER", raising=False)
    payload = await route.open_session(
        route.OpenSessionBody(requested_level="full_avatar", downlink_kbps=50),
        principal=FakePrincipal(),
    )
    assert payload["capability_level"] == FLOOR.value


async def test_an_unrecognised_requested_rung_is_clamped_rather_than_honoured(
    monkeypatch
):
    """A client asking for a rung this build does not have must not be given
    the richest one on the assumption that is what it meant."""
    monkeypatch.delenv("IRA_REALTIME_PROVIDER", raising=False)
    payload = await route.open_session(
        route.OpenSessionBody(requested_level="cinematic_ultra", downlink_kbps=999999),
        principal=FakePrincipal(),
    )
    assert payload["capability_level"] == FLOOR.value


# ── No credential leaks ──────────────────────────────────────────────────────


async def test_no_response_but_the_join_grant_carries_a_credential(monkeypatch):
    """Walks the whole serialised payload rather than checking a few keys, so a
    field added later cannot smuggle one through."""
    monkeypatch.setenv("IRA_REALTIME_PROVIDER", "livekit")
    monkeypatch.setenv("LIVEKIT_URL", "wss://example.invalid")
    monkeypatch.setenv("LIVEKIT_API_KEY", "APIkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s3cret-not-real")

    payload = await route.open_session(
        route.OpenSessionBody(downlink_kbps=5000), principal=FakePrincipal()
    )
    join = payload.pop("join")
    assert join["token"], "sanity: no token was issued, so the check is vacuous"

    scalars = set(_walk(payload))
    assert join["token"] not in scalars
    assert "s3cret-not-real" not in scalars, "the API secret reached the client"


async def test_the_api_secret_never_appears_in_capabilities(monkeypatch):
    monkeypatch.setenv("IRA_REALTIME_PROVIDER", "livekit")
    monkeypatch.setenv("LIVEKIT_URL", "wss://example.invalid")
    monkeypatch.setenv("LIVEKIT_API_KEY", "APIkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s3cret-not-real")

    scalars = set(_walk(await route.get_capabilities(_principal=FakePrincipal())))
    assert "s3cret-not-real" not in scalars
    assert "wss://example.invalid" not in scalars, (
        "the provider URL is published to every caller, configured or not"
    )


# ── Unknown sessions ─────────────────────────────────────────────────────────


async def test_an_unknown_session_is_a_404_not_a_500(monkeypatch):
    from fastapi import HTTPException

    async def _none(*args, **kwargs):
        return []

    monkeypatch.setattr("core.realtime.store.recent_sessions", _none)
    with pytest.raises(HTTPException) as exc:
        await route.mark_connected(str(uuid.uuid4()), principal=FakePrincipal())
    assert exc.value.status_code == 404


async def test_a_malformed_session_id_is_a_404_not_a_crash(monkeypatch):
    """A UUID cast error would otherwise surface as a 500."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await route.mark_connected("not-a-uuid", principal=FakePrincipal())
    assert exc.value.status_code == 404


async def test_another_tenants_session_is_indistinguishable_from_a_missing_one(
    monkeypatch
):
    """RLS scopes the read, so "belongs to someone else" and "does not exist"
    return the same answer — which is the answer that leaks nothing."""
    from fastapi import HTTPException

    async def _only_mine(*args, **kwargs):
        return [{"session_id": str(uuid.uuid4())}]

    monkeypatch.setattr("core.realtime.store.recent_sessions", _only_mine)
    with pytest.raises(HTTPException) as exc:
        await route.mark_connected(str(uuid.uuid4()), principal=FakePrincipal())
    assert exc.value.status_code == 404
    assert "no such realtime session" in exc.value.detail


# ── There is no way to turn recording on ─────────────────────────────────────


async def test_no_endpoint_offers_recording():
    """Not an endpoint that is permission-gated — one that does not exist.

    The schema cannot store audio or transcripts, so an endpoint offering it
    would have nowhere to write. This asserts nobody adds one without also
    having to change the schema, which is the review moment such a change
    deserves.
    """
    paths = [r.path for r in route.router.routes]
    for path in paths:
        lowered = path.lower()
        for forbidden in ("record", "transcript", "capture", "archive"):
            assert forbidden not in lowered, (
                f"{path} looks like a recording endpoint. Phase 6 stores no "
                f"audio and no transcripts; adding one means changing "
                f"027_realtime.sql first."
            )


async def test_the_route_module_never_writes_content_to_telemetry():
    """A crude but load-bearing scan: the handlers must not build a metrics
    dict with a string in it, because the CHECK constraint would reject it at
    the moment a problem is being recorded."""
    import inspect

    source = inspect.getsource(route)
    assert "transcript_stored" in source, "sanity: the scan is looking at the wrong file"
    # The only mentions of transcripts are the disclosure fields asserting False.
    for line in source.splitlines():
        if "transcript" in line.lower() and "=" in line and "False" not in line:
            assert line.strip().startswith(("#", '"', "'", "*")), (
                f"a transcript field is being assigned a non-False value: {line.strip()}"
            )
