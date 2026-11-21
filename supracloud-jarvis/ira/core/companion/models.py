"""Value types for the companion catalogue. No I/O, no database, no decisions.

Every type here is frozen. A catalogue entry that could be mutated after the
licence gate has passed it is a licence gate that proves nothing about what
actually gets rendered.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Literal, Optional

# ── Vocabularies ─────────────────────────────────────────────────────────────
#
# These mirror the CHECK constraints in postgres/024_companion_catalogue.sql.
# tests/companion/test_companion_migration.py asserts the two stay in step —
# a vocabulary that drifts from its constraint fails at INSERT time in
# production and nowhere earlier.

#: Rigs IRA's lip-sync pipeline can actually drive. An avatar rigged any other
#: way would load and then never move its mouth, which is worse than refusing it.
RIG_TYPES: Final[tuple[str, ...]] = ("ARKit52", "OculusVisemes", "VRM")

RENDERERS: Final[tuple[str, ...]] = ("glb", "portrait", "procedural")

AVATAR_STYLES: Final[tuple[str, ...]] = ("stylised", "illustrated", "abstract", "photoreal")

EDITION_AVAILABILITY: Final[tuple[str, ...]] = ("private", "cloud", "both")

VALIDATION_STATUSES: Final[tuple[str, ...]] = ("not-installed", "pending", "passed", "failed")

#: Where speaking with this voice sends the user's words.
PRIVACY_CLASSIFICATIONS: Final[tuple[str, ...]] = ("on_device", "local", "external")

GENDER_PRESENTATIONS: Final[tuple[str, ...]] = ("feminine", "masculine", "neutral", "unspecified")

#: How much the user has decided IRA may do on its own. Ordered from least to
#: most autonomous; the ladder is the brief's, and Phase 4's Permission Broker
#: reads it. Access and action permissions stay separate — this is the ceiling
#: for actions, not a grant of access to anything.
AUTONOMY_LEVELS: Final[tuple[str, ...]] = (
    "never",
    "observe",
    "suggest",
    "prepare",
    "confirm_before_action",
    "automatic_for_approved_routine",
)

#: 'ask' is the default rather than 'automatic': memory that accumulates without
#: the user choosing it is the behaviour the Privacy Centre exists to undo.
MEMORY_MODES: Final[tuple[str, ...]] = ("off", "ask", "automatic")

COMPANION_STATUSES: Final[tuple[str, ...]] = ("draft", "active", "retired")

EditionAvailability = Literal["private", "cloud", "both"]


# ── Performance policy ───────────────────────────────────────────────────────
#
# Separate desktop and mobile budgets, because "it loads on my machine" is not
# the same claim as "it loads on a phone". An avatar that exceeds the mobile
# budget is not refused outright — it is refused AS A MOBILE MODEL, and the
# client falls back. Refusing it everywhere would throw away a perfectly good
# desktop asset.


@dataclass(frozen=True, slots=True)
class PerformanceBudget:
    """Geometry and texture ceiling for one class of device."""

    name: str
    max_triangles: int
    max_texture_px: int


DESKTOP_BUDGET: Final[PerformanceBudget] = PerformanceBudget("desktop", 150_000, 2048)
MOBILE_BUDGET: Final[PerformanceBudget] = PerformanceBudget("mobile", 50_000, 1024)


# ── Catalogue entries ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AvatarEntry:
    """One avatar this deployment can offer.

    Licence fields are `Optional[bool]` rather than `bool` on purpose. `None`
    means UNKNOWN — nobody has checked — which is a different fact from `False`,
    and the licence gate must be able to tell them apart when explaining a
    refusal. Both refuse; only one of them is fixable by reading a licence file.
    """

    slug: str
    display_name: str
    style: str = "stylised"
    renderer: str = "glb"
    edition_availability: EditionAvailability = "private"

    thumbnail_url: Optional[str] = None
    desktop_model_url: Optional[str] = None
    mobile_model_url: Optional[str] = None
    rig_type: Optional[str] = None

    licence_reference: Optional[str] = None
    licence_name: Optional[str] = None
    licence_verified: bool = False
    commercial_use: Optional[bool] = None
    redistribution: Optional[bool] = None
    modification: Optional[bool] = None
    attribution: Optional[str] = None

    validation_status: str = "not-installed"
    supports_blink: bool = False
    supports_speech: bool = False
    triangles: Optional[int] = None
    texture_max_px: Optional[int] = None
    external_textures: bool = False

    fallback_slug: Optional[str] = None
    active: bool = False
    version: int = 1
    sort_order: int = 100

    def public_dict(self) -> dict[str, Any]:
        """The fields a client is allowed to see.

        An explicit allow-list, not `asdict()` minus a few keys: a field added
        later is then invisible to clients until somebody deliberately adds it,
        rather than being published by default.
        """
        return {
            "slug": self.slug,
            "display_name": self.display_name,
            "style": self.style,
            "renderer": self.renderer,
            "thumbnail_url": self.thumbnail_url,
            "desktop_model_url": self.desktop_model_url,
            "mobile_model_url": self.mobile_model_url,
            "rig_type": self.rig_type,
            "supports_blink": self.supports_blink,
            "supports_speech": self.supports_speech,
            "fallback_slug": self.fallback_slug,
            # Shown to the user, not just held internally: someone choosing a
            # face is entitled to know whose work it is.
            "licence_name": self.licence_name,
            "attribution": self.attribution,
        }


@dataclass(frozen=True, slots=True)
class VoiceEntry:
    """One voice this deployment can offer.

    `provider_voice_id` is the engine's own name for the voice (Kokoro's
    "af_bella", Supertonic's "F1"). It is an identifier, not a credential, and
    is safe to send to a client. Provider CREDENTIALS are never held on this
    type and never leave the backend — synthesis and preview are proxied through
    IRA's own API, so a browser never holds a key for anything.
    """

    slug: str
    provider: str
    provider_voice_id: str
    display_name: str
    language: str = "en"
    accent: str = ""
    gender_presentation: str = "unspecified"
    style: str = "warm"
    preview_url: Optional[str] = None
    privacy_classification: str = "local"
    edition_availability: EditionAvailability = "private"
    requires_runtime: Optional[str] = None
    active: bool = False
    sort_order: int = 100

    def public_dict(self) -> dict[str, Any]:
        """The fields a client is allowed to see — allow-list, as above."""
        return {
            "slug": self.slug,
            "display_name": self.display_name,
            "language": self.language,
            "accent": self.accent,
            "gender_presentation": self.gender_presentation,
            "style": self.style,
            "preview_url": self.preview_url,
            # The one fact a user needs before choosing a voice: does speaking
            # with it send my words to somebody else?
            "privacy_classification": self.privacy_classification,
            # The provider's NAME is disclosure, not a secret — a user is
            # entitled to know which engine speaks for them. The provider's
            # voice id is deliberately NOT published: it is an internal
            # selector, and publishing it invites a client to send its own.
            "provider": self.provider,
        }


@dataclass(frozen=True, slots=True)
class PersonalityPreset:
    """A bundle of STYLE dials.

    There is deliberately no free-text instruction field. A preset that carried
    arbitrary prompt text would be an injection surface curated by whoever can
    write the catalogue table, and it would let a "personality" quietly rewrite
    what IRA is willing to do. Style is numeric; permission lives elsewhere.
    """

    slug: str
    display_name: str
    description: str = ""
    warmth: int = 50
    formality: int = 50
    verbosity: int = 50
    proactivity: int = 50
    humour: int = 50
    active: bool = True
    sort_order: int = 100

    def public_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "display_name": self.display_name,
            "description": self.description,
            "warmth": self.warmth,
            "formality": self.formality,
            "verbosity": self.verbosity,
            "proactivity": self.proactivity,
            "humour": self.humour,
        }


@dataclass(frozen=True, slots=True)
class CompanionProfile:
    """A user's chosen companion identity.

    Selections are stored as catalogue SLUGS, matching the database. See the
    "why no UUID foreign keys" note in 024_companion_catalogue.sql: the slug is
    the record of what was chosen, and it survives the catalogue entry being
    withdrawn — which is exactly when you most want to know what someone had.
    """

    display_name: str = "IRA"
    avatar_slug: str = ""
    voice_slug: str = ""
    personality_slug: str = "supportive"
    language: str = "en-GB"
    form_of_address: str = ""
    memory_mode: str = "ask"
    autonomy_level: str = "confirm_before_action"
    status: str = "draft"
    version: int = 1

    def public_dict(self) -> dict[str, Any]:
        return {
            "display_name": self.display_name,
            "avatar_slug": self.avatar_slug,
            "voice_slug": self.voice_slug,
            "personality_slug": self.personality_slug,
            "language": self.language,
            "form_of_address": self.form_of_address,
            "memory_mode": self.memory_mode,
            "autonomy_level": self.autonomy_level,
            "status": self.status,
            "version": self.version,
        }

    def snapshot(self) -> dict[str, Any]:
        """The JSON written to `companion_versions.snapshot`."""
        return dict(self.public_dict())


@dataclass(frozen=True, slots=True)
class CompanionPreferences:
    """Settings that change often and independently of the companion's identity."""

    speech_rate: float = 1.0
    subtitles: bool = True
    reduced_motion: bool = False
    avatar_quality: str = "auto"
    greeting: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
