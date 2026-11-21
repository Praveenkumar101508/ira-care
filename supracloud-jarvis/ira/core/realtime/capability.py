"""The capability ladder — what a live session degrades to, and in what order.

Mirrors the seed block in `postgres/027_realtime.sql`. The table is the durable
record; these constants are what the process actually reads, and
`tests/realtime/test_ladder_matches_migration.py` asserts the two agree field by
field so they cannot drift into a state where which answer is true depends on
the code path. Same arrangement as Phase 3's catalogues and Phase 4's tool
policies.

── Why a ladder and not a boolean ───────────────────────────────────────────

"Does realtime work?" has more than two answers. A user on a train has enough
bandwidth to hear a voice but not to receive a rendered avatar; a user on a
five-year-old phone can receive the video but cannot decode it at frame rate.
Both of those are still a working conversation, and dropping them to "sorry,
try later" throws away a usable session.

So descent is one rung at a time, always downward, and always announced. The
UI shows the current rung, because an avatar that silently stops appearing is
indistinguishable from a bug.

── The floor is text, and the floor always works ────────────────────────────

`TEXT_ONLY` needs no external provider, no microphone and no bandwidth budget.
That matters for the fail-closed rule: when the permission service is
unavailable, or no provider is configured, or the transport refuses, the answer
is not an error page — it is the existing text chat, which has always worked
and is untouched by this phase.

Ascent is deliberately NOT implemented here. Climbing back up mid-sentence
means renegotiating the transport while audio is playing, and a half-second of
silence in the middle of a reply is a worse experience than staying one rung
down. `descend` is the only mover; a better rung is chosen at the next session.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, Optional


class CapabilityLevel(str, Enum):
    """A rung. `str` mixin so it serialises to the same token the schema uses."""

    FULL_AVATAR = "full_avatar"
    LOW_RESOURCE_AVATAR = "low_resource_avatar"
    VOICE_ONLY = "voice_only"
    TEXT_ONLY = "text_only"


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    """One rung's requirements and its stated latency budget."""

    level: CapabilityLevel
    #: 1 is the richest. Descent is always to rung + 1 — no rung is skipped, so
    #: a session's history reads as a monotonic ladder.
    rung: int
    renders_avatar: bool
    produces_audio: bool
    #: The budget time-to-first-audio is MEASURED AGAINST. Not a measured
    #: result and not a guarantee. None where the rung produces no audio at
    #: all — `None` reads as "not applicable"; a `0` would read as "instant".
    target_first_audio_ms: Optional[int]
    min_downlink_kbps: int


#: Richest first. Mirrors the INSERT in 027_realtime.sql.
LADDER: Final[tuple[CapabilityProfile, ...]] = (
    CapabilityProfile(CapabilityLevel.FULL_AVATAR, 1, True, True, 1200, 1500),
    CapabilityProfile(CapabilityLevel.LOW_RESOURCE_AVATAR, 2, True, True, 1200, 600),
    CapabilityProfile(CapabilityLevel.VOICE_ONLY, 3, False, True, 900, 100),
    CapabilityProfile(CapabilityLevel.TEXT_ONLY, 4, False, False, None, 0),
)

#: The rung that always works. Absorbing: descending from it stays on it.
FLOOR: Final[CapabilityLevel] = CapabilityLevel.TEXT_ONLY

_BY_LEVEL: Final[dict[CapabilityLevel, CapabilityProfile]] = {
    profile.level: profile for profile in LADDER
}
_BY_RUNG: Final[dict[int, CapabilityProfile]] = {p.rung: p for p in LADDER}


def profile_for(level: CapabilityLevel | str) -> Optional[CapabilityProfile]:
    """The profile for a rung, or None if the name is not a rung.

    None rather than a default: an unrecognised capability level means the
    caller believes in a rung this build does not have, and guessing which one
    they meant is how a session ends up on a richer rung than anybody chose.
    """
    try:
        return _BY_LEVEL.get(CapabilityLevel(level))
    except ValueError:
        return None


def descend(level: CapabilityLevel | str) -> CapabilityLevel:
    """The next rung down. The floor absorbs; an unknown rung drops to the floor.

    Dropping an unknown rung straight to the floor is the fail-closed reading:
    if we cannot tell how capable a session is, the safe answer is the one that
    needs nothing.
    """
    profile = profile_for(level)
    if profile is None:
        return FLOOR
    return _BY_RUNG.get(profile.rung + 1, _BY_LEVEL[FLOOR]).level


def is_floor(level: CapabilityLevel | str) -> bool:
    return profile_for(level) is _BY_LEVEL[FLOOR]


def best_supported(
    *,
    downlink_kbps: Optional[int],
    avatar_available: bool,
    audio_available: bool,
    requested: CapabilityLevel | str = CapabilityLevel.FULL_AVATAR,
) -> CapabilityLevel:
    """The richest rung this client can actually sustain, never above `requested`.

    `downlink_kbps=None` means the client did not report its bandwidth. That is
    treated as "unknown", not as "unlimited": an unmeasured link starts at
    voice_only and can be nothing richer. A client that wants the avatar has to
    say what it can carry.

    An unrecognised `requested` is clamped to the floor rather than ignored,
    for the same reason `descend` does.
    """
    ceiling = profile_for(requested)
    if ceiling is None:
        return FLOOR
    if not audio_available:
        return FLOOR

    for profile in LADDER:
        if profile.rung < ceiling.rung:
            continue                       # richer than the caller asked for
        if profile.renders_avatar and not avatar_available:
            continue
        if profile.produces_audio and not audio_available:
            continue
        if downlink_kbps is None:
            # Unknown bandwidth: refuse any rung that states a requirement.
            if profile.min_downlink_kbps > 100:
                continue
        elif downlink_kbps < profile.min_downlink_kbps:
            continue
        return profile.level
    return FLOOR


def budget_ms(level: CapabilityLevel | str) -> Optional[int]:
    """The stated time-to-first-audio budget for a rung, or None if it is silent."""
    profile = profile_for(level)
    return profile.target_first_audio_ms if profile else None


__all__ = [
    "FLOOR",
    "LADDER",
    "CapabilityLevel",
    "CapabilityProfile",
    "best_supported",
    "budget_ms",
    "descend",
    "is_floor",
    "profile_for",
]
