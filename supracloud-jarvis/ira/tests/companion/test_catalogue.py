"""Phase 3 — catalogue reads must filter, and fallbacks must not launder.

Two failure modes these guard:

  * A read that returns everything and leaves filtering to the caller. The
    caller that forgets is the one that publishes an unlicensed asset.
  * A fallback chain used as a way around the licence gate — "the avatar you
    asked for is refused, so here is another one" is only safe if the other one
    was evaluated too.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from core.companion import catalogue as cat
from core.companion.licence import is_offerable
from core.companion.models import AvatarEntry


# ── Listing ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("edition", ["private", "cloud"])
def test_every_listed_avatar_passes_the_gate(edition):
    for entry in cat.list_avatars(edition=edition):
        assert is_offerable(entry, edition=edition), f"{entry.slug} listed but refused"


@pytest.mark.parametrize("edition", ["private", "cloud"])
def test_every_listed_voice_passes_the_gate(edition):
    for entry in cat.list_voices(edition=edition):
        assert is_offerable(entry, edition=edition), f"{entry.slug} listed but refused"


def test_cloud_sees_strictly_fewer_avatars_than_private():
    """Not an arbitrary count — it is the assertion that the owner's private
    assets do not leak into the multi-tenant catalogue."""
    private = {a.slug for a in cat.list_avatars(edition="private")}
    cloud = {a.slug for a in cat.list_avatars(edition="cloud")}
    assert cloud < private, "cloud must be a strict subset of private"
    assert "ira-portrait" in private and "ira-portrait" not in cloud


def test_cloud_has_at_least_one_avatar_and_one_voice():
    """The acceptance criterion for Phase 3 depends on this: a cloud user must
    be able to preview a valid avatar and voice. If this ever fails, the
    Companion Creator has nothing to offer and the phase is not done."""
    assert cat.list_avatars(edition="cloud"), "no cloud-offerable avatar"
    assert cat.list_voices(edition="cloud"), "no cloud-offerable voice"


def test_listings_are_sorted_deterministically():
    slugs = [a.slug for a in cat.list_avatars(edition="private")]
    assert slugs == [a.slug for a in cat.list_avatars(edition="private")]
    orders = [a.sort_order for a in cat.list_avatars(edition="private")]
    assert orders == sorted(orders)


def test_personalities_are_active_only():
    assert all(p.active for p in cat.list_personalities())
    assert {p.slug for p in cat.list_personalities()} >= {
        "supportive", "professional", "creative", "analytical"
    }


# ── Defaults ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("edition", ["private", "cloud"])
def test_defaults_are_offerable(edition):
    avatar = cat.default_avatar(edition=edition)
    voice = cat.default_voice(edition=edition)
    assert avatar is not None and is_offerable(avatar, edition=edition)
    assert voice is not None and is_offerable(voice, edition=edition)


def test_the_cloud_default_avatar_is_the_licence_free_one():
    """A default that needed a licence would make the very first screen of the
    product depend on an asset nobody has bought."""
    default = cat.default_avatar(edition="cloud")
    assert default is not None
    assert default.renderer == "procedural"
    assert not default.desktop_model_url and not default.mobile_model_url


# ── Fallback resolution ──────────────────────────────────────────────────────


def test_an_offerable_avatar_resolves_to_itself():
    resolved, decision = cat.resolve_avatar("ira-signature", edition="cloud")
    assert resolved is not None and resolved.slug == "ira-signature"
    assert decision.allowed


def test_a_refused_avatar_falls_back_and_reports_why():
    resolved, decision = cat.resolve_avatar("ira-portrait", edition="cloud")
    assert resolved is not None and resolved.slug == "ira-signature"
    assert not decision.allowed, "the decision returned must be the ORIGINAL one"
    assert decision.reasons


def test_an_unknown_slug_resolves_to_nothing():
    resolved, decision = cat.resolve_avatar("does-not-exist", edition="cloud")
    assert resolved is None
    assert not decision.allowed


def test_a_fallback_chain_that_ends_nowhere_offerable_resolves_to_none(monkeypatch):
    """Falling back is not a way around the gate."""
    a = AvatarEntry(slug="a", display_name="A", active=False, fallback_slug="b")
    b = AvatarEntry(slug="b", display_name="B", active=False, fallback_slug=None)
    monkeypatch.setattr(cat, "SEED_AVATARS", (a, b))

    resolved, decision = cat.resolve_avatar("a", edition="private")
    assert resolved is None
    assert not decision.allowed


def test_a_circular_fallback_chain_terminates(monkeypatch):
    """`a -> b -> a` is a plausible operator mistake in editable data. It must
    be a refusal, not a hung request."""
    a = AvatarEntry(slug="a", display_name="A", active=False, fallback_slug="b")
    b = AvatarEntry(slug="b", display_name="B", active=False, fallback_slug="a")
    monkeypatch.setattr(cat, "SEED_AVATARS", (a, b))

    resolved, _ = cat.resolve_avatar("a", edition="private")
    assert resolved is None


def test_a_self_referential_fallback_terminates(monkeypatch):
    a = AvatarEntry(slug="a", display_name="A", active=False, fallback_slug="a")
    monkeypatch.setattr(cat, "SEED_AVATARS", (a,))
    resolved, _ = cat.resolve_avatar("a", edition="private")
    assert resolved is None


def test_a_long_chain_resolves_to_the_first_offerable_hop(monkeypatch):
    a = AvatarEntry(slug="a", display_name="A", active=False, fallback_slug="b")
    b = AvatarEntry(slug="b", display_name="B", active=False, fallback_slug="c")
    c = AvatarEntry(
        slug="c", display_name="C", renderer="procedural",
        edition_availability="both", validation_status="passed", active=True,
    )
    monkeypatch.setattr(cat, "SEED_AVATARS", (a, b, c))
    resolved, _ = cat.resolve_avatar("a", edition="private")
    assert resolved is not None and resolved.slug == "c"


# ── The client payload ───────────────────────────────────────────────────────


def test_public_dict_never_carries_licence_verification_internals():
    """A client has no use for these and publishing them invites a client to
    reason about licensing, which is the server's job."""
    forbidden = {
        "licence_verified", "commercial_use", "redistribution", "modification",
        "edition_availability", "active", "validation_status",
    }
    for entry in cat.SEED_AVATARS:
        assert not (forbidden & set(entry.public_dict())), entry.slug


def test_a_voice_never_publishes_its_provider_voice_id():
    """The provider's internal selector is not published: publishing it invites
    a client to send its own, and the id is of no use to a UI that selects by
    slug."""
    for voice in cat.SEED_VOICES:
        payload = voice.public_dict()
        assert "provider_voice_id" not in payload
        assert voice.provider_voice_id not in payload.values(), voice.slug


def test_a_voice_publishes_its_privacy_classification():
    """It is the one fact a user needs before choosing a voice, so it must not
    be something the UI has to infer."""
    for voice in cat.SEED_VOICES:
        assert voice.public_dict()["privacy_classification"] in (
            "on_device", "local", "external"
        )


def test_no_seeded_voice_sends_words_to_a_third_party():
    """Every engine in ira/voice/ runs locally. If an external provider is ever
    added, this test must be updated deliberately rather than silently."""
    for voice in cat.SEED_VOICES:
        assert voice.privacy_classification != "external", (
            f"{voice.slug} claims external processing — was that intended? "
            "The Privacy Centre's disclosures depend on this being accurate."
        )


def test_a_personality_preset_carries_no_free_text_instructions():
    """A preset that could carry prompt text would be an injection surface
    curated by whoever can write the catalogue."""
    for preset in cat.SEED_PERSONALITIES:
        payload = preset.public_dict()
        instruction_like = {
            k for k in payload
            if k in ("prompt", "system_prompt", "instructions", "template", "rules")
        }
        assert not instruction_like, f"{preset.slug} carries {instruction_like}"
        for key in ("warmth", "formality", "verbosity", "proactivity", "humour"):
            assert isinstance(payload[key], int)
            assert 0 <= payload[key] <= 100
