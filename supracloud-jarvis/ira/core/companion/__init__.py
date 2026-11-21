"""Companion Creator — the avatar/voice/personality catalogue and the profile.

Phase 3 generalises IRA's single fixed avatar and single fixed voice into
curated catalogues, and gives each user a companion profile that selects from
them.

The package is deliberately split so the parts that make security decisions have
no database in them and can be tested exhaustively:

  * :mod:`models`     — the value types. No I/O.
  * :mod:`licence`    — "may this avatar be offered, to whom?" Fails closed.
  * :mod:`validation` — the rig/geometry/texture report an avatar must pass.
  * :mod:`profile`    — companion field validation and sanitisation.
  * :mod:`catalogue`  — the seed catalogue and the DB-backed reads.
  * :mod:`service`    — assembling a companion from validated selections.

Two rules the package exists to hold:

  1. **Nothing here fabricates a licence.** An asset whose rights cannot be
     positively confirmed is not offered. Unknown is treated as no.
  2. **A personality is style, never permission.** Presets carry numeric dials
     only. Choosing a proactive personality cannot grant IRA the right to act
     without approval — that lives in autonomy level and, from Phase 4, in the
     Constitution and the Permission Broker.
"""
from __future__ import annotations

from core.companion.licence import (
    LicenceDecision,
    evaluate_avatar_licence,
    is_offerable,
)
from core.companion.models import (
    AUTONOMY_LEVELS,
    MEMORY_MODES,
    RIG_TYPES,
    AvatarEntry,
    CompanionProfile,
    PersonalityPreset,
    VoiceEntry,
)
from core.companion.profile import (
    CompanionValidationError,
    validate_profile,
)

__all__ = [
    "AUTONOMY_LEVELS",
    "MEMORY_MODES",
    "RIG_TYPES",
    "AvatarEntry",
    "CompanionProfile",
    "CompanionValidationError",
    "LicenceDecision",
    "PersonalityPreset",
    "VoiceEntry",
    "evaluate_avatar_licence",
    "is_offerable",
    "validate_profile",
]
