"""
utils/external_gate.py — the master switch, enforced at the point of egress.

``IRA_ALLOW_EXTERNAL_API`` is IRA's promise that nothing leaves the box unless
the owner turned external use on. The reasoning path has always honoured it
(``reasoning/api_consent.py``), but the media generation paths — image, video,
audio, transcription — reached Replicate whenever a token happened to be set,
without ever consulting it.

This module closes that gap in ONE place: the helpers that actually make the
outbound call. Gating there rather than in each route means every caller —
routes, the chat feature dispatcher, the assist surfaces — is covered by
construction, and a new caller cannot forget.

It adds no new policy. ``external_api_allowed()`` in ``reasoning/api_consent.py``
remains the single source of truth for whether external use is permitted; this
just refuses, clearly, when it says no, and records the refusal as a consent
audit event (safe metadata only — never prompt text).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("ira.external_gate")


def _detail(feature: str, provider: str) -> str:
    return (
        f"{feature} would use an external provider ({provider}), and external APIs "
        f"are turned off (IRA_ALLOW_EXTERNAL_API=false). Nothing was sent off the "
        f"device. Enable the master switch to allow it, or configure a local "
        f"backend instead."
    )


class EgressBlocked(RuntimeError):
    """Raised inside non-HTTP layers when the master switch forbids leaving the box."""


BROWSING_BLOCKED_DETAIL = (
    "Live browsing is blocked: external APIs are off (IRA_ALLOW_EXTERNAL_API=false). "
    "Fetching a public page sends your query and your IP off the device, so the "
    "master switch governs it. Nothing was fetched."
)


def external_provider_allowed() -> bool:
    """True when the master switch permits an outbound call to a provider.

    Fail-CLOSED: if the setting cannot be read for any reason, the answer is no.
    """
    try:
        from reasoning.api_consent import external_api_allowed

        return bool(external_api_allowed())
    except Exception as exc:  # noqa: BLE001 — unknown means not allowed
        logger.warning("external gate: could not read the master switch (%s) — refusing", exc)
        return False


def require_external_provider(feature: str, *, provider: str = "Replicate") -> None:
    """Refuse with HTTP 403 unless external providers are allowed.

    Called at the top of every helper that performs an outbound generation call.
    The refusal is recorded as a consent audit event so a blocked attempt is
    visible in the same place approved external use is.
    """
    if external_provider_allowed():
        return

    try:
        from reasoning.api_consent import CONSENT_BLOCKED, privacy_mode, record_consent_event

        record_consent_event(
            reason_code=CONSENT_BLOCKED,
            privacy_mode=privacy_mode(),
            selected_mode=f"media:{feature}",
            selected_model=provider,
            consent_required=True,
            consent_approved=False,
            provider=provider,
            estimated_cost_level="none",
        )
    except Exception as exc:  # noqa: BLE001 — auditing must never mask the refusal
        logger.debug("external gate: audit event failed (%s)", exc)

    from fastapi import HTTPException

    raise HTTPException(status_code=403, detail=_detail(feature, provider))


def browsing_allowed() -> bool:
    """True when live browsing may fetch a public page.

    Requires BOTH switches: ``BROWSER_AGENT_ENABLED`` is the capability toggle,
    ``IRA_ALLOW_EXTERNAL_API`` is the egress ceiling. Self-hosted services are
    unaffected — the SSRF guard already refuses loopback/private addresses, so
    nothing local ever travels this path.
    """
    if not external_provider_allowed():
        return False
    try:
        from config import get_settings

        return bool(get_settings().browser_agent_enabled)
    except Exception:  # noqa: BLE001 — unknown means off
        return False


def require_browsing() -> None:
    """Raise :class:`EgressBlocked` unless browsing may leave the box.

    Called at the single navigation chokepoint, so every browsing path — the
    browser agent, flight search, /assist/browse — is covered by construction.
    """
    if browsing_allowed():
        return

    try:
        from reasoning.api_consent import CONSENT_BLOCKED, privacy_mode, record_consent_event

        record_consent_event(
            reason_code=CONSENT_BLOCKED,
            privacy_mode=privacy_mode(),
            selected_mode="browsing",
            selected_model="web",
            consent_required=True,
            consent_approved=False,
            provider="web",
            estimated_cost_level="none",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("external gate: browsing audit event failed (%s)", exc)

    raise EgressBlocked(BROWSING_BLOCKED_DETAIL)


__all__ = [
    "EgressBlocked",
    "BROWSING_BLOCKED_DETAIL",
    "external_provider_allowed",
    "require_external_provider",
    "browsing_allowed",
    "require_browsing",
]
