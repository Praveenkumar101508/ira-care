"""Phase 3 — the companion API's contract.

Handlers are called directly, as the rest of this suite does, so the assertions
are about the route's own behaviour rather than about FastAPI's plumbing.

The property worth the most here is the negative one: **no provider credential,
and no provider-internal selector, appears anywhere in a catalogue response.**
That is asserted by walking the whole serialised payload rather than by checking
a few keys, so a field added later cannot smuggle one through.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from api.routes import companion as route
from core.companion.catalogue import SEED_VOICES
from core.companion.models import CompanionProfile


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


# ── Catalogue ────────────────────────────────────────────────────────────────


async def test_catalogue_returns_all_three_lists_and_both_vocabularies():
    payload = await route.get_catalogue(_user="owner")
    assert payload["avatars"], "no avatars offered"
    assert payload["voices"], "no voices offered"
    assert payload["personalities"], "no personalities offered"
    # Served rather than hard-coded in the client, so the UI cannot offer a
    # level the backend rejects.
    assert "confirm_before_action" in payload["autonomy_levels"]
    assert "ask" in payload["memory_modes"]
    assert payload["edition"] in ("private", "cloud")


async def test_no_provider_voice_id_appears_anywhere_in_the_catalogue_response():
    """The provider's internal selector is not published. Publishing it invites
    a client to send its own."""
    payload = await route.get_catalogue(_user="owner")
    scalars = set(_walk(payload))
    assert scalars, "sanity: the walker found nothing, so the check is vacuous"
    for voice in SEED_VOICES:
        assert voice.provider_voice_id not in scalars, (
            f"{voice.slug}'s provider_voice_id {voice.provider_voice_id!r} "
            f"was published to the client"
        )


async def test_the_payload_walker_would_find_a_leaked_value():
    """Proves the check above is capable of failing: the walker must reach
    nested values, not just top-level keys."""
    found = set(_walk({"a": [{"b": {"c": "leaked-value"}}]}))
    assert "leaked-value" in found


async def test_no_secret_shaped_value_appears_in_the_catalogue_response():
    """A blunt but effective backstop: if a credential ever reaches this
    payload, it will almost certainly carry one of these markers."""
    payload = await route.get_catalogue(_user="owner")
    blob = json.dumps(payload).lower()
    for marker in ("api_key", "apikey", "secret", "token", "password", "bearer", "authorization"):
        assert marker not in blob, f"{marker!r} appears in a catalogue response"


async def test_catalogue_avatars_never_carry_licence_internals():
    payload = await route.get_avatars(_user="owner")
    for avatar in payload["avatars"]:
        assert "licence_verified" not in avatar
        assert "commercial_use" not in avatar
        assert "edition_availability" not in avatar


async def test_voices_carry_their_privacy_classification():
    payload = await route.get_voices(_user="owner")
    assert payload["voices"]
    for voice in payload["voices"]:
        assert voice["privacy_classification"] in ("on_device", "local", "external")


async def test_personalities_carry_only_numeric_dials():
    payload = await route.get_personalities(_user="owner")
    for preset in payload["personalities"]:
        for key in ("warmth", "formality", "verbosity", "proactivity", "humour"):
            assert isinstance(preset[key], int)
        assert not {"prompt", "instructions", "system_prompt"} & set(preset)


# ── Profile ──────────────────────────────────────────────────────────────────


class _FakePrincipal:
    tenant_id = "11111111-1111-1111-1111-111111111111"
    user_id = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def no_db(monkeypatch):
    """Stub the persistence layer. These tests are about the route's contract,
    not about SQL — the SQL is exercised against a real PostgreSQL in
    tests/tenancy/."""
    saved: dict = {}

    async def fake_load(_principal):
        return saved.get("profile")

    async def fake_save(_principal, profile, *, reason=""):
        saved["profile"] = profile
        saved["reason"] = reason
        return profile

    monkeypatch.setattr(route, "load_companion", fake_load)
    monkeypatch.setattr(route, "save_companion", fake_save)
    return saved


async def test_a_user_with_no_companion_gets_a_default_draft_not_a_404(no_db):
    """Making the client special-case "no profile yet" is how the two paths
    drift apart."""
    payload = await route.get_profile(principal=_FakePrincipal())
    assert payload["exists"] is False
    assert payload["profile"]["display_name"] == "IRA"
    assert payload["profile"]["status"] == "draft"


async def test_saving_a_draft_does_not_activate_it(no_db):
    payload = await route.put_profile(route.CompanionBody(display_name="Aria"), principal=_FakePrincipal())
    assert payload["profile"]["status"] == "draft"
    assert payload["profile"]["display_name"] == "Aria"


async def test_activating_sets_the_status_to_active(no_db):
    payload = await route.activate_profile(
        route.CompanionBody(display_name="Aria"), principal=_FakePrincipal()
    )
    assert payload["profile"]["status"] == "active"


async def test_omitted_fields_keep_their_current_value(no_db):
    await route.put_profile(
        route.CompanionBody(display_name="Aria", form_of_address="Praveen"), principal=_FakePrincipal()
    )
    payload = await route.put_profile(
        route.CompanionBody(display_name="Nova"), principal=_FakePrincipal()
    )
    assert payload["profile"]["display_name"] == "Nova"
    assert payload["profile"]["form_of_address"] == "Praveen", (
        "an omitted field was overwritten — a wizard that saves one step at a "
        "time would erase every other answer"
    )


async def test_a_refused_selection_returns_422_naming_the_field(no_db):
    """So the wizard can mark the right step rather than showing a generic
    error on the last one."""
    with pytest.raises(HTTPException) as exc:
        await route.put_profile(
            route.CompanionBody(avatar_slug="does-not-exist"), principal=_FakePrincipal()
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["field"] == "avatar_slug"


async def test_an_invalid_autonomy_level_is_refused_by_the_route(no_db):
    with pytest.raises(HTTPException) as exc:
        await route.put_profile(
            route.CompanionBody(autonomy_level="do-whatever"), principal=_FakePrincipal()
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["field"] == "autonomy_level"


async def test_the_profile_response_reports_a_fallback_rather_than_substituting_silently(
    monkeypatch, no_db
):
    """An avatar can be withdrawn after a user selected it. Saying so is the
    difference between a fallback and a silent substitution."""
    async def fake_load(_principal):
        return CompanionProfile(
            display_name="Aria",
            avatar_slug="ira-portrait",
            voice_slug="kokoro-bella",
            status="active",
        )

    monkeypatch.setattr(route, "load_companion", fake_load)
    monkeypatch.setattr(route, "current_edition", lambda: "cloud")

    payload = await route.get_profile(principal=_FakePrincipal())
    assert payload["profile"]["avatar_slug"] == "ira-portrait"
    assert payload["resolved_avatar"]["slug"] == "ira-signature"
    assert payload["fallback_reason"], "the reason for the substitution was not reported"


# ── Identity resolution ──────────────────────────────────────────────────────


async def test_cloud_identity_unavailability_surfaces_as_503_not_500(monkeypatch):
    """Phase 2 left cloud principal resolution deliberately refusing rather than
    minting a private-shaped principal in a multi-tenant process. That is a
    deployment state, not a malformed request.

    The property is unchanged; only its address moved. P9-03 deleted this
    module's private copy of the resolve-or-503 helper in favour of the shared
    `principal_from_username`, so the assertion follows it there rather than
    being dropped — and `tests/tenancy/test_p9_tenant_header.py` pins the two
    statuses that copy could not express (400 ambiguous, 403 not a member).
    """
    from api.middleware import workspace as ws
    from core.identity.resolver import CloudIdentityNotAvailable

    async def boom(_username, **_kwargs):
        raise CloudIdentityNotAvailable("cloud identity is not available")

    monkeypatch.setattr(ws, "resolve_principal", boom)

    with pytest.raises(HTTPException) as exc:
        await ws.principal_from_username("owner")
    assert exc.value.status_code == 503
