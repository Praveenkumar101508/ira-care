"""Fail-soft seams for existing chat, voice and vision perception sources."""
from __future__ import annotations

import logging

from cognition.models import CognitiveEvent, EventKind
from cognition.runtime import get_kernel

logger = logging.getLogger("ira.cognition.ingest")


async def feed_cognitive_percept(
    owner_id: str,
    source: str,
    summary: str,
    *,
    confidence: float = 0.8,
    payload: dict | None = None,
) -> bool:
    """Deliver a trusted IRA subsystem percept when the kernel is enabled.

    This never makes the calling chat/voice/vision route depend on the optional
    kernel. Payloads should be metadata only; raw binary media is never copied.

    **Refuses in the cloud edition.** `get_kernel()` returns one process-global
    `NeuroKernel` — one event bus, one global workspace, one self-model, one
    store — with no tenant dimension in it. Every caller here passes
    `get_settings().ira_admin_username` as `owner_id`, so in a multi-tenant
    deployment this would pour every tenant's chat and voice content into one
    shared object under one identity, and `/cognition/status` reads it back out.

    This is the choke point rather than the call sites, deliberately: there are
    three today and the next one to be added would not know to check. See 3C-05.
    """
    from core.editions import EDITION_CLOUD, current_edition

    if current_edition() == EDITION_CLOUD:
        return False

    kernel = get_kernel()
    if not kernel.settings.enabled or not summary.strip():
        return False
    try:
        await kernel.perceive(CognitiveEvent(
            user_id=owner_id,
            kind=EventKind.PERCEPT,
            source=source[:80],
            summary=summary[:1000],
            payload=dict(payload or {}),
            trust=0.9,
            confidence=confidence,
            salience=0.6,
            novelty=0.6,
        ))
        return True
    except Exception as exc:
        logger.debug("cognitive percept feed unavailable: %s", exc)
        return False


__all__ = ["feed_cognitive_percept"]
