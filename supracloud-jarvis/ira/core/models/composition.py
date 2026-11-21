"""Where the gateway is actually built.

Phase 5 shipped `IRAModelGateway`. Phase 6 shipped the licence gate inside it.
Phase 7 shipped a training pipeline that feeds it. Each closure report then said
the same sentence, and it was true every time:

    nothing in the product constructs one

This module is the thing that constructs one. It is small on purpose — a
composition root that decides anything is a second policy engine — and almost
all of its content is about **which collaborators are attached in which
edition**, because that is where the honest failure modes are.

── There is no bypass, and that is the point ───────────────────────────────

The obvious way to ship this safely would be a flag: route through the gateway
when it is switched on, and keep the direct client otherwise. That is exactly
how the gap this phase closes was created. `registry_required` defaults to False
for a good reason — a deployment that has not registered its models must keep
working — but "off" there means *the registry is not consulted*, not *the
gateway is bypassed*. The route is single.

So what varies by deployment is how much governance is attached, not whether the
request goes through the chokepoint:

    private, no database   policy only. No quota, no metering, no registry.
    cloud, database        policy, quota, reservation, metering; registry when
                           the deployment has registered its models.

A private install with no tenant is not "ungoverned by accident" — it is a
single-user machine with nothing to meter and nobody to meter it to, and the
policy still runs.

── What this deliberately does NOT build ───────────────────────────────────

**No external provider.** `029_commercial.sql` has a catalogue that can describe
third-party providers, and there is no `ModelProvider` implementation for one.
IRA constructs the self-hosted backends it actually has — Ollama and vLLM — and
a catalogue row for a provider nobody implemented buys nothing. Recording this
matters because the alternative reading of "the gateway is now on the live path"
is "IRA can now route to external providers", and it cannot.

**No cost table.** Both providers this builds run on hardware the operator
already pays for, so per-token cost is zero and `estimate_cost` returns zero.
Inventing a rate would put a fabricated number in a tenant's usage table.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from core.interfaces.model_provider import Tier
from core.models.gateway import IRAModelGateway
from core.models.providers import OpenAICompatibleProvider, ProviderMisconfigured

logger = logging.getLogger("ira.models.composition")

#: Tier → the registry `model_key` a deployment would have registered.
#:
#: Server-side by construction: a request names a tier, never a model, so a
#: caller cannot choose which registry row approves it. The names match the
#: roles `MODEL_EVALUATION_MATRIX.md` and `067`'s seed already use.
TIER_MODEL_KEYS: dict[str, str] = {
    "fast": "local_fast",
    "deep": "local_main",
    "reasoning": "local_reasoning",
    "vision": "local_vision",
}

_lock = threading.Lock()
_gateway: Optional[IRAModelGateway] = None
_built_for: Any = None


def _client_factory(tier: Tier, base_url: str, api_key: str, timeout: Any):
    """Delegate to `utils/llm.py`'s existing per-tier client factories.

    Phase 8 moves the ROUTE, not the client construction. Those three functions
    already encode the tier→endpoint mapping, the Ollama/vLLM switch and the
    timeout, and reimplementing them here would mean two places to get the
    backend switch wrong.

    It also keeps a seam every existing test already uses: patching
    `utils.llm.get_fast_client` still substitutes the client the gateway ends up
    calling, so tests written before the gateway existed keep testing what they
    were written to test.
    """
    from utils import llm as _llm

    if tier is Tier.REASONING:
        return _llm.get_reasoning_client()
    if tier is Tier.DEEP:
        return _llm.get_deep_client()
    if tier is Tier.VISION:
        client, _model = _llm._vision_client_and_model()
        return client
    return _llm.get_fast_client()


def _cloud_collaborators(gateway: IRAModelGateway) -> None:
    """Attach the database-backed halves: quota, reservation, metering.

    Imported lazily. `core.models.store` reaches for a tenant connection, and a
    private install has no database — importing it at module scope would make
    the private edition depend on the cloud one to build a gateway that will
    never use it.
    """
    from core.models import store as _store

    gateway.quota_reader = _store.load_quota
    gateway.quota_reserver = _store.reserve_daily_request
    gateway.quota_releaser = _store.release_daily_request
    gateway.usage_writer = _store.record_usage


def build_gateway(cfg: Any, *, edition: str) -> IRAModelGateway:
    """Construct a gateway for this process. Never cached here — see `gateway()`."""
    gateway = IRAModelGateway(tier_model_keys=dict(TIER_MODEL_KEYS))

    try:
        provider = OpenAICompatibleProvider.from_settings(
            cfg, client_factory=_client_factory)
    except ProviderMisconfigured as exc:
        # A gateway with no provider is not broken: `refusal_for` distinguishes
        # "nothing is configured" from "everything was refused", and the first
        # is the message an operator needs. Raising here would turn a
        # misconfiguration into an import-time crash.
        logger.warning("no model provider could be built: %s", exc)
        return gateway

    gateway.register(provider)
    # Both providers this builds run on the operator's own hardware, so there is
    # no per-token bill to record. Zero is a fact; a made-up rate would not be.
    gateway.costs = {provider.provider_id: {}}

    if edition == "cloud":
        _cloud_collaborators(gateway)
        gateway.registry_required = bool(
            getattr(cfg, "ira_registry_required", False))

    return gateway


def gateway(cfg: Any = None, *, edition: Optional[str] = None) -> IRAModelGateway:
    """The process's gateway, built once.

    Cached because building it resolves models through the selection layer,
    which probes Ollama — doing that per request would put a network call in
    front of every completion. Rebuilt when the settings object changes
    identity, which is what `get_settings.cache_clear()` produces in tests.
    """
    global _gateway, _built_for

    if cfg is None:
        from config import get_settings
        cfg = get_settings()
    if edition is None:
        from core.editions import current_edition
        edition = current_edition()

    key = (id(cfg), edition)
    with _lock:
        if _gateway is None or _built_for != key:
            _gateway = build_gateway(cfg, edition=edition)
            _built_for = key
        return _gateway


def reset_gateway() -> None:
    """Drop the cached gateway. For tests, and for a settings reload."""
    global _gateway, _built_for
    with _lock:
        _gateway = None
        _built_for = None


__all__ = ["TIER_MODEL_KEYS", "build_gateway", "gateway", "reset_gateway"]
