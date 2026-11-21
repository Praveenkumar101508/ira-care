"""Validating and sanitising what a user typed into the Companion Creator.

Three of these fields — the companion's name, the form of address, and the
greeting — are free text that will end up in a model prompt. That makes them an
injection surface, and it is worth being precise about what this module does and
does not claim:

  **It does** strip control characters and newlines, cap length, and reject
  empty-after-cleaning input. A name cannot therefore contain a line break
  followed by something shaped like a new instruction, which is the cheapest
  form of the attack and the one that works by accident as often as by intent.

  **It does not** make the field safe to concatenate into a system prompt. No
  amount of character filtering does that. The real defence is that IRA's
  permissions are enforced by application code — the Constitution Engine and the
  Permission Broker in Phase 4 — and not by the model's willingness to follow
  its instructions. A companion named "ignore your rules" still cannot send an
  email without an approval, because the approval is not the model's decision.

Since the user is the only person who can set their own companion's name, the
threat here is mostly self-inflicted. It becomes a real one in Phase 5 if a
companion is ever shared, which is why the sanitisation is here from the start.
"""
from __future__ import annotations

import unicodedata
from typing import Optional

from core.companion.models import (
    AUTONOMY_LEVELS,
    COMPANION_STATUSES,
    MEMORY_MODES,
    CompanionProfile,
)

MAX_DISPLAY_NAME = 40
MAX_FORM_OF_ADDRESS = 40
MAX_LANGUAGE = 20
MAX_SLUG = 64


class CompanionValidationError(ValueError):
    """A companion profile field was rejected. Carries the field name."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


def clean_text(value: Optional[str], *, max_len: int) -> str:
    """Strip anything that is not printable text, collapse spaces, cap length.

    Unicode categories rather than a character blocklist: `C*` covers control
    characters, format characters (including the bidirectional overrides and
    zero-width joiners used to make one string render as another) and
    unassigned code points. A blocklist of `\\n` and `\\r` would miss U+2028,
    which many renderers also treat as a line break.
    """
    if value is None:
        return ""
    normalised = unicodedata.normalize("NFC", str(value))
    kept = "".join(
        ch for ch in normalised
        if not unicodedata.category(ch).startswith("C") and ch not in (" ", " ")
    )
    return " ".join(kept.split())[:max_len]


def clean_slug(value: Optional[str], *, field: str) -> str:
    """A catalogue slug: lowercase letters, digits and hyphens only.

    Rejected rather than coerced. A slug that needs cleaning did not come from
    the catalogue, and quietly rewriting it into a valid-looking one would turn
    a bad request into a lookup for something the caller never asked for.
    """
    if not value:
        return ""
    slug = str(value).strip().lower()
    if len(slug) > MAX_SLUG:
        raise CompanionValidationError(field, f"longer than {MAX_SLUG} characters")
    if not all(ch.isalnum() or ch == "-" for ch in slug) or not slug[0].isalnum():
        raise CompanionValidationError(
            field,
            "must contain only lowercase letters, digits and hyphens, and start "
            "with a letter or digit",
        )
    return slug


def validate_profile(
    *,
    display_name: str = "IRA",
    avatar_slug: str = "",
    voice_slug: str = "",
    personality_slug: str = "supportive",
    language: str = "en-GB",
    form_of_address: str = "",
    memory_mode: str = "ask",
    autonomy_level: str = "confirm_before_action",
    status: str = "draft",
    version: int = 1,
) -> CompanionProfile:
    """Validate and normalise a companion profile. Raises on anything invalid.

    Enum-like fields are matched EXACTLY against their vocabulary rather than
    being lowercased-and-guessed. An autonomy level of "AUTOMATIC" is not
    silently read as `automatic_for_approved_routine`: the one field on this
    form that decides how much IRA may do without asking is not a place for a
    helpful guess.
    """
    name = clean_text(display_name, max_len=MAX_DISPLAY_NAME)
    if not name:
        raise CompanionValidationError(
            "display_name", "must contain at least one printable character"
        )

    address = clean_text(form_of_address, max_len=MAX_FORM_OF_ADDRESS)

    lang = clean_text(language, max_len=MAX_LANGUAGE) or "en-GB"
    if not all(ch.isalnum() or ch in "-_" for ch in lang):
        raise CompanionValidationError(
            "language", "must be a language tag such as en-GB"
        )

    if memory_mode not in MEMORY_MODES:
        raise CompanionValidationError(
            "memory_mode", f"must be one of {', '.join(MEMORY_MODES)}"
        )

    if autonomy_level not in AUTONOMY_LEVELS:
        raise CompanionValidationError(
            "autonomy_level", f"must be one of {', '.join(AUTONOMY_LEVELS)}"
        )

    if status not in COMPANION_STATUSES:
        raise CompanionValidationError(
            "status", f"must be one of {', '.join(COMPANION_STATUSES)}"
        )

    try:
        version_int = int(version)
    except (TypeError, ValueError):
        raise CompanionValidationError("version", "must be an integer") from None
    if version_int < 1:
        raise CompanionValidationError("version", "must be 1 or greater")

    return CompanionProfile(
        display_name=name,
        avatar_slug=clean_slug(avatar_slug, field="avatar_slug"),
        voice_slug=clean_slug(voice_slug, field="voice_slug"),
        personality_slug=clean_slug(personality_slug, field="personality_slug"),
        language=lang,
        form_of_address=address,
        memory_mode=memory_mode,
        autonomy_level=autonomy_level,
        status=status,
        version=version_int,
    )


def next_version(profile: CompanionProfile) -> CompanionProfile:
    """The same profile at the next version number."""
    from dataclasses import replace

    return replace(profile, version=profile.version + 1)
