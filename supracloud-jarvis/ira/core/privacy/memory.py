"""Transparent memory — every stored memory, explicable and controllable.

The brief's requirement is that a user can see, for each memory: its content,
where it came from, why it was stored, how sensitive it is, how confident IRA
is, when it was created, when it expires, when it was last used, which apps may
use it, and whether it may go to an external provider — and can approve, edit,
expire, delete, disable by category, or switch memory off entirely.

The through-line: **a memory nobody can explain is one the user cannot
meaningfully judge.** So `storage_reason` is not decoration. A memory arriving
with no reason is a bug in whatever stored it, and `describe()` says so rather
than papering over it.

Pure value types and rules here; persistence is in `store.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Optional, Sequence
import uuid

#: Sensitivity bands. Mirrors the CHECK in 026_privacy.sql and feeds the
#: Constitution's `sensitivity_in` condition (Phase 4).
SENSITIVITIES: Final[tuple[str, ...]] = (
    "normal", "personal", "health", "financial", "family", "credential",
)

#: Bands that are never sent to an external model provider whatever the
#: per-memory policy says. A credential in a prompt is a credential disclosed,
#: and "the user allowed it" is not a good enough reason to do it by default.
NEVER_EXTERNAL: Final[frozenset[str]] = frozenset({"credential", "health"})

EXTERNAL_POLICIES: Final[tuple[str, ...]] = ("never", "anonymised", "allowed")
REVIEW_STATES: Final[tuple[str, ...]] = ("pending", "accepted", "rejected")
MEMORY_MODES: Final[tuple[str, ...]] = ("off", "ask", "automatic")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryRejected(ValueError):
    """A memory was refused by the user's own settings. Carries why."""


@dataclass(frozen=True, slots=True)
class RetentionSettings:
    """What a user has decided IRA may keep.

    Every default is the privacy-preserving one:

      * `memory_mode='ask'` — memory that accumulates without the user choosing
        it is the behaviour the Privacy Centre exists to undo.
      * `voice_retention_hours=0` — raw audio is discarded once transcribed.
      * retention days `None` — meaning "until you delete it", which is a
        visible choice rather than a silent forever.
    """

    memory_enabled: bool = True
    memory_mode: str = "ask"
    conversation_retention_days: Optional[int] = None
    memory_retention_days: Optional[int] = None
    voice_retention_hours: int = 0
    disabled_categories: frozenset[str] = field(default_factory=frozenset)

    def allows(self, sensitivity: str) -> tuple[bool, str]:
        """Whether a memory of this sensitivity may be stored at all."""
        if not self.memory_enabled:
            return False, "memory is switched off"
        if self.memory_mode == "off":
            return False, "memory mode is 'off'"
        if sensitivity in self.disabled_categories:
            return False, f"the {sensitivity!r} category is switched off"
        return True, ""

    def expiry_for(self, *, created: Optional[datetime] = None) -> Optional[datetime]:
        if self.memory_retention_days is None:
            return None
        return (created or _now()) + timedelta(days=self.memory_retention_days)

    @property
    def requires_approval(self) -> bool:
        """True when a new memory must be approved before it counts."""
        return self.memory_mode == "ask"


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """One stored memory, with everything needed to judge it."""

    content: str
    source_type: str = "conversation"
    source_reference: str = ""
    storage_reason: str = ""
    sensitivity: str = "normal"
    confidence: float = 0.5
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    allowed_connectors: tuple[str, ...] = ()
    external_provider_policy: str = "never"
    review_state: str = "accepted"
    deleted_at: Optional[datetime] = None
    memory_id: Optional[uuid.UUID] = None

    # ── State ───────────────────────────────────────────────────────────────

    def is_expired(self, *, now: Optional[datetime] = None) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= (now or _now())

    def is_live(self, *, now: Optional[datetime] = None) -> bool:
        """Usable right now: not deleted, not expired, not awaiting approval."""
        return (
            self.deleted_at is None
            and not self.is_expired(now=now)
            and self.review_state == "accepted"
        )

    # ── Sharing ─────────────────────────────────────────────────────────────

    def may_share_with(self, connector: str) -> bool:
        """Whether this memory may be given to a connector.

        Empty `allowed_connectors` means none — the fail-closed reading. A
        memory is not shared with an integration unless the user said so.
        """
        return self.is_live() and connector in self.allowed_connectors

    def may_send_to_external_provider(self) -> tuple[bool, str]:
        """Whether this memory may go to a third-party model.

        Two bands are refused regardless of the per-memory policy. A credential
        placed in a prompt has been disclosed, and health data sent to a
        provider is a disclosure the user is unlikely to have had in mind when
        they set a general preference.
        """
        if not self.is_live():
            return False, "memory is not live"
        if self.sensitivity in NEVER_EXTERNAL:
            return False, (
                f"{self.sensitivity} memories are never sent to an external "
                f"provider, whatever the per-memory policy says"
            )
        if self.external_provider_policy == "never":
            return False, "this memory is marked 'never' for external providers"
        return True, self.external_provider_policy

    # ── Explanation ─────────────────────────────────────────────────────────

    def describe(self) -> str:
        """A plain-language account of why IRA is holding this.

        Says so when the reason is missing rather than inventing one — an
        unexplained memory is a defect in whatever stored it, and hiding that
        would make the Privacy Centre complicit in it.
        """
        if not self.storage_reason:
            return (
                "IRA has not recorded why it kept this. That is a fault — you "
                "can delete it, and it should not have been stored without a "
                "reason."
            )
        bits = [self.storage_reason.rstrip(".") + "."]
        if self.source_reference:
            bits.append(f"From {self.source_type}: {self.source_reference}.")
        else:
            bits.append(f"From a {self.source_type}.")
        if self.expires_at:
            bits.append(f"Expires {self.expires_at.date().isoformat()}.")
        else:
            bits.append("No expiry set — it is kept until you delete it.")
        if self.last_used_at:
            bits.append(f"Last used {self.last_used_at.date().isoformat()}.")
        else:
            bits.append("Never used to answer anything yet.")
        return " ".join(bits)

    def public_dict(self) -> dict[str, Any]:
        """Everything the Privacy Centre shows. An allow-list, as elsewhere."""
        return {
            "id": str(self.memory_id) if self.memory_id else None,
            "content": self.content,
            "source_type": self.source_type,
            "source_reference": self.source_reference,
            "storage_reason": self.storage_reason,
            "sensitivity": self.sensitivity,
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "allowed_connectors": list(self.allowed_connectors),
            "external_provider_policy": self.external_provider_policy,
            "review_state": self.review_state,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            "explanation": self.describe(),
        }


def prepare(
    content: str,
    *,
    settings: RetentionSettings,
    storage_reason: str,
    sensitivity: str = "normal",
    source_type: str = "conversation",
    source_reference: str = "",
    confidence: float = 0.5,
    now: Optional[datetime] = None,
) -> MemoryRecord:
    """Build a memory that respects the user's settings, or refuse.

    Raises :class:`MemoryRejected` when the settings forbid it — rather than
    storing it in some muted form. "Stored but disabled" is the state that later
    turns out to have been readable all along.
    """
    if not str(content).strip():
        raise MemoryRejected("a memory needs content")
    if sensitivity not in SENSITIVITIES:
        raise MemoryRejected(f"unknown sensitivity {sensitivity!r}")
    if not str(storage_reason).strip():
        # Enforced at the point of creation so `describe()` never has to
        # apologise for a missing reason on a NEW memory.
        raise MemoryRejected(
            "a memory must record why it was stored — an unexplained memory "
            "cannot be judged by the person it is about"
        )

    allowed, why = settings.allows(sensitivity)
    if not allowed:
        raise MemoryRejected(f"not stored: {why}")

    created = now or _now()
    return MemoryRecord(
        content=str(content).strip(),
        source_type=source_type,
        source_reference=source_reference,
        storage_reason=str(storage_reason).strip(),
        sensitivity=sensitivity,
        confidence=max(0.0, min(1.0, float(confidence))),
        created_at=created,
        expires_at=settings.expiry_for(created=created),
        # Awaiting approval when the user chose 'ask' — Phase 3's default.
        review_state="pending" if settings.requires_approval else "accepted",
        external_provider_policy="never",
        allowed_connectors=(),
    )


def approve(record: MemoryRecord) -> MemoryRecord:
    return replace(record, review_state="accepted")


def reject(record: MemoryRecord, *, now: Optional[datetime] = None) -> MemoryRecord:
    """Reject a proposed memory. Marks it deleted as well as rejected, so no
    read path has to remember to filter on both."""
    return replace(record, review_state="rejected", deleted_at=now or _now())


def set_expiry(record: MemoryRecord, when: Optional[datetime]) -> MemoryRecord:
    return replace(record, expires_at=when)


def soft_delete(record: MemoryRecord, *, now: Optional[datetime] = None) -> MemoryRecord:
    return replace(record, deleted_at=now or _now())


def touch(record: MemoryRecord, *, now: Optional[datetime] = None) -> MemoryRecord:
    """Record that this memory was actually used to answer something."""
    return replace(record, last_used_at=now or _now())


def live_only(
    records: Sequence[MemoryRecord], *, now: Optional[datetime] = None
) -> list[MemoryRecord]:
    return [r for r in records if r.is_live(now=now)]


__all__ = [
    "EXTERNAL_POLICIES",
    "MEMORY_MODES",
    "NEVER_EXTERNAL",
    "REVIEW_STATES",
    "SENSITIVITIES",
    "MemoryRecord",
    "MemoryRejected",
    "RetentionSettings",
    "approve",
    "live_only",
    "prepare",
    "reject",
    "set_expiry",
    "soft_delete",
    "touch",
]
