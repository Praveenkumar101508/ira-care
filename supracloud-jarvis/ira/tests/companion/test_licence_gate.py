"""Phase 3 — the avatar licence gate must fail closed.

The property under test is the one the transformation brief states as a
non-negotiable: *"Invalid avatar licence metadata → do not publish the avatar."*

These tests are written adversarially rather than as a happy-path check. Each
one takes an entry that IS offerable and breaks exactly one thing about it, then
requires a refusal — because the failure mode that matters is not "the gate
rejects garbage", it is "the gate accepts something that is 95% right".
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from core.companion.catalogue import SEED_AVATARS, get_avatar
from core.companion.licence import (
    evaluate_avatar_licence,
    evaluate_voice_availability,
    is_offerable,
)
from core.companion.models import AvatarEntry, VoiceEntry


def _cloud_ok() -> AvatarEntry:
    """A minimal entry that IS offerable in cloud. The baseline to break."""
    return AvatarEntry(
        slug="test-ok",
        display_name="Test",
        renderer="glb",
        rig_type="ARKit52",
        desktop_model_url="/avatar/test.glb",
        edition_availability="both",
        licence_reference="/avatar/test-license.json",
        licence_name="CC0",
        licence_verified=True,
        commercial_use=True,
        redistribution=True,
        validation_status="passed",
        supports_blink=True,
        supports_speech=True,
        active=True,
    )


def test_the_baseline_entry_is_offerable_in_both_editions():
    """Without this, every test below could pass by refusing everything."""
    entry = _cloud_ok()
    assert evaluate_avatar_licence(entry, edition="cloud").allowed
    assert evaluate_avatar_licence(entry, edition="private").allowed


# ── One broken field at a time ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "field,value,expect_private_refused",
    [
        # Licence terms only gate REDISTRIBUTION, so these refuse cloud while
        # leaving private use alone — that asymmetry is the whole design.
        ("licence_verified", False, False),
        ("commercial_use", False, False),
        ("commercial_use", None, False),
        ("redistribution", False, False),
        ("redistribution", None, False),
        # These are refusals in every edition.
        ("active", False, True),
        ("validation_status", "not-installed", True),
        ("validation_status", "failed", True),
        ("validation_status", "pending", True),
        ("licence_reference", None, True),
        ("licence_reference", "", True),
        ("edition_availability", "private", False),
    ],
)
def test_breaking_one_field_refuses_cloud(field, value, expect_private_refused):
    entry = replace(_cloud_ok(), **{field: value})

    decision = evaluate_avatar_licence(entry, edition="cloud")
    assert not decision.allowed, f"{field}={value!r} was accepted for cloud"
    assert decision.reasons, "a refusal must carry a reason"

    private = evaluate_avatar_licence(entry, edition="private")
    assert private.allowed is not expect_private_refused, (
        f"{field}={value!r}: private edition decision was {private.allowed}, "
        f"expected {not expect_private_refused} ({private.reason})"
    )


def test_unknown_is_refused_the_same_as_no_but_says_so():
    """`None` and `False` both refuse — but an operator must be able to tell
    'nobody checked' from 'the licence says no', because only one of those is
    fixable by reading the licence file."""
    unknown = replace(_cloud_ok(), commercial_use=None)
    explicit_no = replace(_cloud_ok(), commercial_use=False)

    d_unknown = evaluate_avatar_licence(unknown, edition="cloud")
    d_no = evaluate_avatar_licence(explicit_no, edition="cloud")

    assert not d_unknown.allowed and not d_no.allowed
    assert "unknown" in d_unknown.reason
    assert "unknown" not in d_no.reason


def test_an_unknown_edition_is_refused_rather_than_guessed():
    """A misconfigured edition name must not fall through to the laxer rule."""
    decision = evaluate_avatar_licence(_cloud_ok(), edition="staging")
    assert not decision.allowed
    assert "unknown edition" in decision.reason


def test_unknown_edition_availability_value_is_refused():
    entry = replace(_cloud_ok(), edition_availability="everyone")  # type: ignore[arg-type]
    assert not evaluate_avatar_licence(entry, edition="cloud").allowed
    assert not evaluate_avatar_licence(entry, edition="private").allowed


def test_all_reasons_are_collected_not_just_the_first():
    """An operator fixing an entry wants the whole list, not one per round trip."""
    entry = replace(
        _cloud_ok(),
        licence_verified=False,
        commercial_use=None,
        redistribution=None,
    )
    decision = evaluate_avatar_licence(entry, edition="cloud")
    assert len(decision.reasons) >= 3, decision.reasons


# ── The procedural exemption ─────────────────────────────────────────────────


def test_a_procedural_avatar_needs_no_licence_record():
    """There is no asset file, so there is nothing to license. Requiring a
    licence record for maths would be theatre."""
    entry = AvatarEntry(
        slug="proc",
        display_name="Procedural",
        renderer="procedural",
        edition_availability="both",
        validation_status="passed",
        licence_reference=None,
        licence_verified=False,
        commercial_use=None,
        redistribution=None,
        active=True,
    )
    assert evaluate_avatar_licence(entry, edition="cloud").allowed


def test_the_procedural_exemption_cannot_be_claimed_by_an_entry_that_ships_a_model():
    """Setting renderer='procedural' on an entry with a GLB must not launder an
    unlicensed binary through the exemption."""
    entry = AvatarEntry(
        slug="sneaky",
        display_name="Sneaky",
        renderer="procedural",
        desktop_model_url="/avatar/downloaded-from-somewhere.glb",
        edition_availability="both",
        validation_status="passed",
        licence_reference=None,
        licence_verified=False,
        active=True,
    )
    decision = evaluate_avatar_licence(entry, edition="cloud")
    assert not decision.allowed, "an unlicensed GLB passed as 'procedural'"
    assert "licence" in decision.reason.lower()


# ── The shipped catalogue ────────────────────────────────────────────────────


def test_the_owner_portrait_is_never_offerable_in_cloud():
    """The single most important assertion in this file.

    `ira-portrait` is a likeness of a real person whose licence is recorded as
    unverified. It may be used by the owner on their own machine. It must never
    be redistributed to cloud users.
    """
    portrait = get_avatar("ira-portrait")
    assert portrait is not None
    assert evaluate_avatar_licence(portrait, edition="private").allowed
    assert not evaluate_avatar_licence(portrait, edition="cloud").allowed


def test_no_seeded_avatar_claims_an_unverified_licence_while_being_cloud_offerable():
    """Nothing in the shipped catalogue may be cloud-offerable unless its rights
    are positively confirmed — or it has no asset at all."""
    for entry in SEED_AVATARS:
        if not is_offerable(entry, edition="cloud"):
            continue
        procedural = entry.renderer == "procedural" and not entry.desktop_model_url
        assert procedural or (
            entry.licence_verified
            and entry.commercial_use is True
            and entry.redistribution is True
        ), f"{entry.slug} is cloud-offerable without confirmed redistribution rights"


def test_the_uninstalled_model_slot_is_not_offerable_anywhere():
    """`ira-digital-human` names a GLB that is deliberately not committed."""
    entry = get_avatar("ira-digital-human")
    assert entry is not None
    assert not is_offerable(entry, edition="private")
    assert not is_offerable(entry, edition="cloud")


# ── Voices ───────────────────────────────────────────────────────────────────


def test_an_inactive_voice_is_not_offered_and_says_what_is_missing():
    voice = VoiceEntry(
        slug="v",
        provider="supertonic",
        provider_voice_id="F1",
        display_name="V",
        edition_availability="both",
        requires_runtime="supertonic model files",
        active=False,
    )
    decision = evaluate_voice_availability(voice, edition="private")
    assert not decision.allowed
    assert "supertonic model files" in decision.reason


def test_a_private_only_voice_is_refused_in_cloud():
    voice = VoiceEntry(
        slug="v",
        provider="omnivoice",
        provider_voice_id="design",
        display_name="V",
        edition_availability="private",
        active=True,
    )
    assert evaluate_voice_availability(voice, edition="private").allowed
    assert not evaluate_voice_availability(voice, edition="cloud").allowed
