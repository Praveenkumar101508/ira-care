"""The catalogue this deployment offers, and the rules for reading it.

── Where the catalogue lives ────────────────────────────────────────────────

The tables in `postgres/024_companion_catalogue.sql` are the durable record and
the thing an operator will eventually edit. This module holds the SHIPPED
catalogue as constants and serves reads from them.

That is a deliberate Phase 3 choice, not an oversight. The catalogue is global
static reference data — the same for every tenant, changing only when an
operator installs an asset — so a query per request would buy nothing, and a
half-built read path that silently falls back to constants when the database is
unreachable is exactly the kind of "which one am I looking at?" ambiguity that
makes a licence decision untrustworthy. Serving from one place means the licence
gate is deciding about the same rows the tests decided about.

The two must not drift, so they are not allowed to:
`tests/companion/test_catalogue_matches_migration.py` parses the migration's
seed INSERTs and asserts every slug and every licence-relevant field matches the
constants below. Reading the table instead becomes a one-function change in the
phase that adds an operator surface to write it.

── What gets offered ────────────────────────────────────────────────────────

Every read goes through the licence gate. `list_avatars` does not return
everything and let the caller filter — a caller that forgets is a caller that
publishes an unlicensed asset, so filtering is not the caller's job.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from core.companion.licence import (
    LicenceDecision,
    evaluate_avatar_licence,
    evaluate_voice_availability,
)
from core.companion.models import (
    AvatarEntry,
    PersonalityPreset,
    VoiceEntry,
)

logger = logging.getLogger("ira.companion.catalogue")


# ── Avatars ──────────────────────────────────────────────────────────────────
#
# Mirrors the seed block in 024_companion_catalogue.sql. Read the comments there
# for why each entry is set the way it is; the short version:
#
#   ira-signature      IRA's own procedural render. No third-party asset exists,
#                      so there is nothing to license. The only cloud-offerable
#                      avatar today, and the one that makes the Companion
#                      Creator usable without anybody inventing a licence.
#   ira-portrait       The owner's approved identity portrait. PRIVATE ONLY —
#                      licenceVerified is false in ira-avatar-license.json and it
#                      is a real person's likeness.
#   ira-digital-human  The rigged GLB slot. No model is installed and none is
#                      committed; this entry exists so the creator can show what
#                      an operator could add, not so it can be selected.

SEED_AVATARS: tuple[AvatarEntry, ...] = (
    AvatarEntry(
        slug="ira-signature",
        display_name="IRA Signature",
        style="abstract",
        renderer="procedural",
        edition_availability="both",
        licence_reference="in-repo: rendered by IRA's own components",
        licence_name="IRA original — no third-party asset",
        licence_verified=True,
        commercial_use=True,
        redistribution=True,
        modification=True,
        attribution="IRA",
        validation_status="passed",
        supports_blink=True,
        supports_speech=True,
        external_textures=False,
        fallback_slug=None,
        active=True,
        sort_order=10,
    ),
    AvatarEntry(
        slug="ira-portrait",
        display_name="IRA Portrait",
        style="photoreal",
        renderer="portrait",
        edition_availability="private",
        thumbnail_url="/avatar/ira-reference.png",
        licence_reference="/avatar/ira-avatar-license.json",
        licence_name="owner-declared",
        licence_verified=False,
        commercial_use=None,
        redistribution=None,
        modification=None,
        attribution="IRA — Praveen Kamineti",
        validation_status="passed",
        # A portrait is a still image: it does not blink and it does not
        # lip-sync. Saying otherwise here would put a claim in the catalogue
        # that the renderer cannot honour.
        supports_blink=False,
        supports_speech=False,
        external_textures=False,
        fallback_slug="ira-signature",
        active=True,
        sort_order=20,
    ),
    AvatarEntry(
        slug="ira-digital-human",
        display_name="IRA Digital Human",
        style="photoreal",
        renderer="glb",
        edition_availability="private",
        desktop_model_url="/avatar/ira-head.glb",
        mobile_model_url="/avatar/ira-head-mobile.glb",
        rig_type="ARKit52",
        licence_reference="/avatar/ira-avatar-license.json",
        licence_name=None,
        licence_verified=False,
        validation_status="not-installed",
        supports_blink=False,
        supports_speech=False,
        external_textures=False,
        fallback_slug="ira-portrait",
        active=False,
        sort_order=30,
    ),
)


# ── Voices ───────────────────────────────────────────────────────────────────
#
# Providers are the engines that actually exist in ira/voice/ (see
# tts_factory.py). All three run locally, hence privacy_classification 'local' —
# speaking with any of them sends nothing to a third party. Only Kokoro is
# active: it is the default engine and needs no extra install. The other two are
# real and OFF until an operator installs their runtime, which `requires_runtime`
# names rather than leaving the user to guess.

SEED_VOICES: tuple[VoiceEntry, ...] = (
    VoiceEntry(
        slug="kokoro-bella",
        provider="kokoro",
        provider_voice_id="af_bella",
        display_name="Bella — warm",
        accent="light Indian warmth",
        gender_presentation="feminine",
        style="warm",
        privacy_classification="local",
        edition_availability="both",
        active=True,
        sort_order=10,
    ),
    VoiceEntry(
        slug="kokoro-heart",
        provider="kokoro",
        provider_voice_id="af_heart",
        display_name="Heart — bright",
        accent="neutral",
        gender_presentation="feminine",
        style="bright",
        privacy_classification="local",
        edition_availability="both",
        active=True,
        sort_order=20,
    ),
    VoiceEntry(
        slug="supertonic-f1",
        provider="supertonic",
        provider_voice_id="F1",
        display_name="Supertonic F1",
        accent="neutral",
        gender_presentation="feminine",
        style="neutral",
        privacy_classification="local",
        edition_availability="private",
        requires_runtime="supertonic model files",
        active=False,
        sort_order=30,
    ),
    VoiceEntry(
        slug="supertonic-m1",
        provider="supertonic",
        provider_voice_id="M1",
        display_name="Supertonic M1",
        accent="neutral",
        gender_presentation="masculine",
        style="neutral",
        privacy_classification="local",
        edition_availability="private",
        requires_runtime="supertonic model files",
        active=False,
        sort_order=40,
    ),
    VoiceEntry(
        slug="omnivoice-design",
        provider="omnivoice",
        provider_voice_id="design",
        display_name="OmniVoice (designed)",
        accent="configurable",
        gender_presentation="unspecified",
        style="configurable",
        privacy_classification="local",
        edition_availability="private",
        requires_runtime="omnivoice sidecar venv",
        active=False,
        sort_order=50,
    ),
)


# ── Personalities ────────────────────────────────────────────────────────────

SEED_PERSONALITIES: tuple[PersonalityPreset, ...] = (
    PersonalityPreset(
        slug="supportive",
        display_name="Supportive",
        description="Warm and encouraging. Checks in, softens bad news, offers the next step.",
        warmth=85, formality=35, verbosity=55, proactivity=45, humour=45, sort_order=10,
    ),
    PersonalityPreset(
        slug="professional",
        display_name="Professional",
        description="Concise and precise. Minimal small talk, answers first.",
        warmth=40, formality=80, verbosity=35, proactivity=35, humour=15, sort_order=20,
    ),
    PersonalityPreset(
        slug="creative",
        display_name="Creative",
        description="Playful and associative. Offers alternatives and unexpected angles.",
        warmth=70, formality=30, verbosity=65, proactivity=60, humour=70, sort_order=30,
    ),
    PersonalityPreset(
        slug="analytical",
        display_name="Analytical",
        description="Structured and evidence-led. States assumptions and confidence.",
        warmth=35, formality=65, verbosity=60, proactivity=30, humour=15, sort_order=40,
    ),
)


# ── Reads ────────────────────────────────────────────────────────────────────


def _sorted(entries: Iterable) -> list:
    return sorted(entries, key=lambda e: (e.sort_order, e.slug))


def list_avatars(*, edition: str) -> list[AvatarEntry]:
    """Every avatar offerable in `edition`, in display order.

    Filtered, not annotated: an entry that fails the licence gate is absent, so
    a caller cannot render it by ignoring a flag.
    """
    return _sorted(
        e for e in SEED_AVATARS if evaluate_avatar_licence(e, edition=edition).allowed
    )


def list_voices(*, edition: str) -> list[VoiceEntry]:
    """Every voice offerable in `edition`, in display order."""
    return _sorted(
        v for v in SEED_VOICES if evaluate_voice_availability(v, edition=edition).allowed
    )


def list_personalities() -> list[PersonalityPreset]:
    """Every active personality preset, in display order.

    Not edition-filtered: a personality is numeric style with no asset and no
    provider behind it, so there is nothing about it that could be licensed
    differently or unavailable in one edition.
    """
    return _sorted(p for p in SEED_PERSONALITIES if p.active)


def get_avatar(slug: str) -> Optional[AvatarEntry]:
    """An avatar by slug, WITHOUT the licence gate.

    For diagnostics and for explaining a refusal. Never use it to decide what to
    render — use :func:`resolve_avatar`.
    """
    return next((e for e in SEED_AVATARS if e.slug == slug), None)


def get_voice(slug: str) -> Optional[VoiceEntry]:
    return next((v for v in SEED_VOICES if v.slug == slug), None)


def get_personality(slug: str) -> Optional[PersonalityPreset]:
    return next((p for p in SEED_PERSONALITIES if p.slug == slug), None)


def default_avatar(*, edition: str) -> Optional[AvatarEntry]:
    """The avatar to use when the user has not chosen one."""
    offerable = list_avatars(edition=edition)
    return offerable[0] if offerable else None


def default_voice(*, edition: str) -> Optional[VoiceEntry]:
    offerable = list_voices(edition=edition)
    return offerable[0] if offerable else None


def resolve_avatar(slug: str, *, edition: str) -> tuple[Optional[AvatarEntry], LicenceDecision]:
    """The avatar to actually render for `slug`, following the fallback chain.

    Returns the resolved entry (or None) and the decision for the ORIGINALLY
    requested slug, so a caller can log why it fell back.

    Two properties this has to hold:

      * **It terminates.** A fallback chain is operator-editable data and
        `a -> b -> a` is a plausible mistake; a visited-set makes that a
        refusal rather than a hung request.
      * **It never lands somewhere the gate refuses.** Falling back is not a
        way around the licence check — every hop is evaluated the same way, so
        a chain that ends at an unlicensed asset resolves to None.
    """
    original = get_avatar(slug)
    if original is None:
        return None, LicenceDecision(False, (f"no avatar with slug {slug!r}",))

    decision = evaluate_avatar_licence(original, edition=edition)
    if decision.allowed:
        return original, decision

    seen = {slug}
    current = original
    while current.fallback_slug and current.fallback_slug not in seen:
        seen.add(current.fallback_slug)
        nxt = get_avatar(current.fallback_slug)
        if nxt is None:
            break
        if evaluate_avatar_licence(nxt, edition=edition).allowed:
            logger.info(
                "avatar %s not offerable in %s edition (%s); falling back to %s",
                slug, edition, decision.reason, nxt.slug,
            )
            return nxt, decision
        current = nxt

    logger.warning(
        "avatar %s not offerable in %s edition (%s) and no offerable fallback",
        slug, edition, decision.reason,
    )
    return None, decision
