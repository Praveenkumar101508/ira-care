"""Phase 5 — every memory is explicable, and the defaults are the cautious ones.

The acceptance criterion is that a user can view and control every retained
memory. That only means something if:

  * a memory carries the facts needed to judge it (where from, why, how
    sensitive, when it expires, when last used);
  * the settings that limit what is kept are actually enforced at the point of
    storing, not just displayed;
  * sharing is off until the user says otherwise;
  * and "delete" reaches every read path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.privacy.memory import (
    NEVER_EXTERNAL,
    SENSITIVITIES,
    MemoryRecord,
    MemoryRejected,
    RetentionSettings,
    approve,
    live_only,
    prepare,
    reject,
    set_expiry,
    soft_delete,
    touch,
)

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


def settings(**kw) -> RetentionSettings:
    return RetentionSettings(**kw)


# ── Defaults are the cautious ones ───────────────────────────────────────────


def test_the_default_settings_ask_before_storing():
    """Matches Phase 3's companion default. Memory that accumulates without the
    user choosing it is the behaviour the Privacy Centre exists to undo."""
    default = RetentionSettings()
    assert default.memory_mode == "ask"
    assert default.requires_approval is True
    assert default.memory_enabled is True


def test_voice_audio_is_discarded_by_default():
    assert RetentionSettings().voice_retention_hours == 0


def test_a_new_memory_is_shared_with_nothing_by_default():
    record = prepare("likes strong coffee", settings=settings(),
                     storage_reason="mentioned twice this week", now=NOW)
    assert record.allowed_connectors == ()
    assert record.external_provider_policy == "never"
    assert record.may_share_with("gmail") is False


def test_a_new_memory_under_ask_starts_pending_not_accepted():
    record = prepare("likes strong coffee", settings=settings(),
                     storage_reason="mentioned twice", now=NOW)
    assert record.review_state == "pending"
    assert record.is_live(now=NOW) is False, "a pending memory must not be usable yet"


def test_automatic_mode_accepts_immediately():
    record = prepare("likes strong coffee", settings=settings(memory_mode="automatic"),
                     storage_reason="mentioned twice", now=NOW)
    assert record.review_state == "accepted"
    assert record.is_live(now=NOW) is True


# ── Settings are enforced at the point of storing ────────────────────────────


def test_memory_switched_off_refuses_to_store():
    with pytest.raises(MemoryRejected, match="switched off"):
        prepare("anything", settings=settings(memory_enabled=False),
                storage_reason="because", now=NOW)


def test_memory_mode_off_refuses_to_store():
    with pytest.raises(MemoryRejected, match="'off'"):
        prepare("anything", settings=settings(memory_mode="off"),
                storage_reason="because", now=NOW)


def test_a_disabled_category_refuses_to_store():
    """The brief's "never remember health conversations", as a setting rather
    than a rule."""
    cfg = settings(disabled_categories=frozenset({"health"}))
    with pytest.raises(MemoryRejected, match="health"):
        prepare("has a hospital appointment", settings=cfg,
                sensitivity="health", storage_reason="mentioned", now=NOW)
    # ...and other categories are unaffected.
    assert prepare("likes coffee", settings=cfg, storage_reason="mentioned", now=NOW)


def test_a_refused_memory_is_not_stored_in_a_muted_form():
    """"Stored but disabled" is the state that later turns out to have been
    readable all along. The refusal must be a refusal."""
    with pytest.raises(MemoryRejected):
        prepare("x", settings=settings(memory_enabled=False),
                storage_reason="because", now=NOW)


def test_retention_days_set_an_expiry():
    record = prepare("likes coffee", settings=settings(memory_retention_days=30),
                     storage_reason="mentioned", now=NOW)
    assert record.expires_at == NOW + timedelta(days=30)


def test_no_retention_days_means_no_expiry():
    record = prepare("likes coffee", settings=settings(), storage_reason="x", now=NOW)
    assert record.expires_at is None


# ── A memory must be explicable ──────────────────────────────────────────────


def test_a_memory_without_a_reason_is_refused_at_creation():
    """An unexplained memory cannot be judged by the person it is about."""
    with pytest.raises(MemoryRejected, match="why it was stored"):
        prepare("something", settings=settings(), storage_reason="  ", now=NOW)


def test_describe_explains_a_well_formed_memory():
    record = prepare(
        "prefers morning meetings", settings=settings(memory_retention_days=90),
        storage_reason="said so when rescheduling", source_type="conversation",
        source_reference="chat 2026-07-20", now=NOW,
    )
    text = record.describe()
    assert "said so when rescheduling" in text
    assert "chat 2026-07-20" in text
    assert "Expires" in text
    assert "Never used" in text


def test_describe_admits_when_a_legacy_memory_has_no_reason():
    """Old rows predate the requirement. Hiding that would make the Privacy
    Centre complicit in the gap."""
    legacy = MemoryRecord(content="something", storage_reason="")
    text = legacy.describe()
    assert "not recorded why" in text
    assert "fault" in text


def test_an_empty_memory_is_refused():
    with pytest.raises(MemoryRejected, match="needs content"):
        prepare("   ", settings=settings(), storage_reason="x", now=NOW)


def test_an_unknown_sensitivity_is_refused():
    with pytest.raises(MemoryRejected, match="unknown sensitivity"):
        prepare("x", settings=settings(), sensitivity="vibes",
                storage_reason="x", now=NOW)


@pytest.mark.parametrize("band", SENSITIVITIES)
def test_every_documented_sensitivity_is_accepted(band):
    assert prepare("x", settings=settings(), sensitivity=band,
                   storage_reason="because", now=NOW).sensitivity == band


def test_confidence_is_clamped_rather_than_rejected():
    assert prepare("x", settings=settings(), confidence=5.0,
                   storage_reason="y", now=NOW).confidence == 1.0
    assert prepare("x", settings=settings(), confidence=-3,
                   storage_reason="y", now=NOW).confidence == 0.0


# ── Lifecycle ────────────────────────────────────────────────────────────────


def test_approving_makes_a_pending_memory_usable():
    record = prepare("x", settings=settings(), storage_reason="y", now=NOW)
    assert approve(record).is_live(now=NOW) is True


def test_rejecting_marks_it_deleted_as_well_as_rejected():
    """So no read path has to remember to filter on both."""
    record = reject(prepare("x", settings=settings(), storage_reason="y", now=NOW), now=NOW)
    assert record.review_state == "rejected"
    assert record.deleted_at is not None
    assert record.is_live(now=NOW) is False


def test_a_deleted_memory_is_not_live():
    record = prepare("x", settings=settings(memory_mode="automatic"),
                     storage_reason="y", now=NOW)
    assert soft_delete(record, now=NOW).is_live(now=NOW) is False


def test_an_expired_memory_is_not_live():
    record = prepare("x", settings=settings(memory_mode="automatic",
                                            memory_retention_days=1),
                     storage_reason="y", now=NOW)
    assert record.is_live(now=NOW) is True
    assert record.is_live(now=NOW + timedelta(days=2)) is False


def test_a_user_can_set_and_clear_an_expiry():
    record = prepare("x", settings=settings(memory_mode="automatic"),
                     storage_reason="y", now=NOW)
    dated = set_expiry(record, NOW + timedelta(days=7))
    assert dated.expires_at == NOW + timedelta(days=7)
    assert set_expiry(dated, None).expires_at is None


def test_touch_records_that_a_memory_was_actually_used():
    """"Never used to answer anything" is what lets a user delete confidently."""
    record = prepare("x", settings=settings(memory_mode="automatic"),
                     storage_reason="y", now=NOW)
    assert record.last_used_at is None
    assert touch(record, now=NOW).last_used_at == NOW


def test_live_only_filters_every_non_live_state():
    base = settings(memory_mode="automatic")
    live = prepare("live", settings=base, storage_reason="y", now=NOW)
    deleted = soft_delete(prepare("gone", settings=base, storage_reason="y", now=NOW), now=NOW)
    pending = prepare("pending", settings=settings(), storage_reason="y", now=NOW)
    expired = prepare("old", settings=settings(memory_mode="automatic",
                                               memory_retention_days=1),
                      storage_reason="y", now=NOW - timedelta(days=10))

    kept = live_only([live, deleted, pending, expired], now=NOW)
    assert [r.content for r in kept] == ["live"]


# ── Sharing is fail-closed ───────────────────────────────────────────────────


def test_a_memory_is_shared_only_with_named_connectors():
    record = MemoryRecord(content="x", storage_reason="y",
                          allowed_connectors=("gmail",))
    assert record.may_share_with("gmail") is True
    assert record.may_share_with("drive") is False


def test_a_deleted_memory_is_shared_with_nothing():
    record = MemoryRecord(content="x", storage_reason="y",
                          allowed_connectors=("gmail",),
                          deleted_at=NOW)
    assert record.may_share_with("gmail") is False


@pytest.mark.parametrize("band", sorted(NEVER_EXTERNAL))
def test_some_bands_never_go_to_an_external_provider(band):
    """A credential in a prompt is a credential disclosed, and a general
    preference is unlikely to have had health data in mind."""
    record = MemoryRecord(content="x", storage_reason="y", sensitivity=band,
                          external_provider_policy="allowed")
    allowed, why = record.may_send_to_external_provider()
    assert allowed is False
    assert band in why


def test_the_default_external_policy_refuses():
    record = MemoryRecord(content="x", storage_reason="y")
    allowed, _ = record.may_send_to_external_provider()
    assert allowed is False


def test_an_explicitly_allowed_normal_memory_may_go_external():
    record = MemoryRecord(content="x", storage_reason="y",
                          external_provider_policy="allowed")
    allowed, policy = record.may_send_to_external_provider()
    assert allowed is True and policy == "allowed"


# ── The payload the Privacy Centre shows ─────────────────────────────────────


def test_public_dict_carries_every_field_the_brief_requires():
    record = prepare("x", settings=settings(memory_retention_days=10),
                     storage_reason="because", source_reference="chat-1", now=NOW)
    payload = record.public_dict()
    for key in (
        "content", "source_type", "source_reference", "storage_reason",
        "sensitivity", "confidence", "created_at", "expires_at", "last_used_at",
        "allowed_connectors", "external_provider_policy", "deleted_at",
    ):
        assert key in payload, f"the Privacy Centre cannot show {key}"
    assert payload["explanation"]
