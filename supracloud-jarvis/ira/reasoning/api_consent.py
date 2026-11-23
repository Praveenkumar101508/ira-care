"""
ira/reasoning/api_consent.py — the external-API consent, privacy and audit layer.

This module owns everything to do with *whether* IRA may leave the machine:

  * the privacy / consent environment switches and their safe defaults;
  * the user-facing Deep Intelligence Mode consent copy;
  * :func:`apply_consent` — turning a user's Approve / Local-only answer into a
    final :class:`~reasoning.model_router.ModelRouteDecision`;
  * the runtime execution gate (:func:`run_decision` + executor registry) that
    makes external use impossible unless it was explicitly enabled and wired;
  * a small **structured consent audit hook** (:func:`record_consent_event`)
    that records *safe metadata only* — never prompts, never secrets — every
    time Deep Intelligence Mode is offered, approved, declined, blocked, or
    found unavailable.

It deliberately does **not** import :class:`ModelRouteDecision` at runtime
(``apply_consent`` only needs :func:`dataclasses.replace`, ``run_decision`` only
reads attributes), so ``model_router`` can import and re-export from here without
a circular import. All public names are re-exported from ``reasoning.model_router``
for backward compatibility — existing callers keep importing from there unchanged.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Mapping, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime import cycle
    from reasoning.model_router import ModelRouteDecision

logger = logging.getLogger("ira.reasoning.consent_audit")

# ── Consent / privacy environment switches ──────────────────────────────────────
ALLOW_EXTERNAL_ENV = "IRA_ALLOW_EXTERNAL_API"      # master off-switch (default false)
REQUIRE_CONSENT_ENV = "IRA_REQUIRE_API_CONSENT"    # ask before any external (default true)
PRIVACY_MODE_ENV = "IRA_PRIVACY_MODE"              # local_only | local_first | external_ok
# Kept as names so older imports resolve; neither setting does anything any more.
# IRA CARE (round 3, phase 4, D-056): there is no hosted model API in this product.
EXTERNAL_PROVIDER_ENV = "IRA_EXTERNAL_API_PROVIDER"
EXTERNAL_MODEL_ENV = "IRA_EXTERNAL_API_MODEL"

DEFAULT_PRIVACY_MODE = "local_first"

#: Shown to the user when a very hard task is routed: IRA answers on the machine,
#: with its own open-weight models; there is no external model to approve.
CONSENT_MESSAGE = (
    "IRA answers this on your own machine, with its own open models. "
    "There is no external model service in IRA CARE and no paid tokens: your words stay here.\n\n"
    "Reply: 'Local only' (the only mode) to continue."
)


# ── Config readers ──────────────────────────────────────────────────────────────

def _bool_env(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def privacy_mode(env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    val = (env.get(PRIVACY_MODE_ENV) or "").strip().lower()
    return val or DEFAULT_PRIVACY_MODE


def external_api_allowed(env: Optional[Mapping[str, str]] = None) -> bool:
    """May a MODEL call ever leave the machine? Never. IRA CARE has no hosted model
    API and no paid model service (D-056); `IRA_ALLOW_EXTERNAL_API` no longer
    turns one on. (The same variable still governs ordinary web fetches in the
    research and browser tools, which are not model calls; see utils/external_gate.)"""
    return False


def consent_required(env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return _bool_env(env, REQUIRE_CONSENT_ENV, True)


def consent_message() -> str:
    return CONSENT_MESSAGE




# ── Structured consent audit hook ───────────────────────────────────────────────
# A tiny, dependency-free audit seam. It records only *safe metadata* about each
# Deep Intelligence Mode consent decision so external-use choices are independently
# queryable. It NEVER receives prompt content, context, or secrets. By default it
# writes one structured INFO log line; a host app can call
# ``register_consent_audit_sink()`` to forward events into the full audit/event
# system (e.g. ``utils.security_events.emit_event``) later.

#: The machine-readable outcomes an audit event can record.
CONSENT_OFFERED = "offered"        # IRA offered Deep Intelligence Mode (consent pending)
CONSENT_APPROVED = "approved"      # user approved AND external is allowed -> going external
CONSENT_DECLINED = "declined"      # user chose Local Mode only
CONSENT_BLOCKED = "blocked"        # user approved but config/policy forbids external -> stayed local
CONSENT_UNAVAILABLE = "unavailable"  # external requested at run time but not executable


@dataclass(frozen=True)
class ConsentAuditEvent:
    """Safe, structured record of one consent decision.

    Contains **no** prompt text, context, or secret — only routing metadata that
    is safe to persist and query.
    """

    timestamp: str                      # ISO-8601 UTC, second precision
    reason_code: str                    # one of the CONSENT_* codes above
    privacy_mode: str                   # active IRA_PRIVACY_MODE
    selected_mode: str                  # resolved ModelMode (e.g. "local_reasoning")
    selected_model: str                 # concrete model name (local or external target)
    consent_required: bool              # was explicit user consent required?
    consent_approved: Optional[bool]    # True/False once answered; None when not applicable
    provider: Optional[str]             # external provider name — only when approved+external
    estimated_cost_level: str           # "none" | "low" | "medium" | "high"


#: Sink signature: ``fn(event: ConsentAuditEvent) -> None``.
AuditSink = Callable[["ConsentAuditEvent"], None]

_AUDIT_SINK: Optional[AuditSink] = None  # None -> the default structured-log sink


def _default_log_sink(event: ConsentAuditEvent) -> None:
    """Default sink: emit one structured INFO line (no prompt, no secret)."""
    logger.info("consent_audit %s", asdict(event))


def register_consent_audit_sink(fn: Optional[AuditSink]) -> None:
    """Register the callable that receives every :class:`ConsentAuditEvent`.

    Pass ``None`` to fall back to the default structured-log sink. This is the
    seam a host app uses to forward consent decisions into the full audit system.
    The sink must accept a single :class:`ConsentAuditEvent`; it is called
    fail-soft (any exception is logged and swallowed, never propagated to routing).
    """
    global _AUDIT_SINK
    _AUDIT_SINK = fn


def reset_consent_audit_sink() -> None:
    """Restore the default structured-log sink (tests / lockdown)."""
    global _AUDIT_SINK
    _AUDIT_SINK = None


def record_consent_event(
    *,
    reason_code: str,
    privacy_mode: str,
    selected_mode: str,
    selected_model: str,
    consent_required: bool,
    consent_approved: Optional[bool] = None,
    provider: Optional[str] = None,
    estimated_cost_level: str = "none",
) -> ConsentAuditEvent:
    """Build and dispatch a :class:`ConsentAuditEvent`, returning it.

    Dispatch is fail-soft: a broken sink never disturbs routing. Only safe
    metadata is accepted — there is deliberately no parameter for prompt text.
    """
    event = ConsentAuditEvent(
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        reason_code=reason_code,
        privacy_mode=privacy_mode,
        selected_mode=str(selected_mode),
        selected_model=selected_model,
        consent_required=consent_required,
        consent_approved=consent_approved,
        provider=provider,
        estimated_cost_level=estimated_cost_level,
    )
    sink = _AUDIT_SINK or _default_log_sink
    try:
        sink(event)
    except Exception as exc:  # noqa: BLE001 - audit must never break routing
        logger.warning("consent audit sink failed (fail-soft): %s", exc)
    return event


def _audit_from_decision(
    decision: "ModelRouteDecision",
    *,
    reason_code: str,
    consent_approved: Optional[bool] = None,
    provider: Optional[str] = None,
) -> ConsentAuditEvent:
    """Record an audit event from a decision's safe metadata fields."""
    return record_consent_event(
        reason_code=reason_code,
        privacy_mode=decision.privacy_level,
        selected_mode=str(decision.selected_mode),
        selected_model=decision.selected_model,
        consent_required=decision.requires_api_consent,
        consent_approved=consent_approved,
        provider=provider,
        estimated_cost_level=decision.estimated_cost_level,
    )


# ── Consent gate ────────────────────────────────────────────────────────────────

def apply_consent(
    decision: "ModelRouteDecision",
    approved: bool,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> "ModelRouteDecision":
    """Resolve a consent prompt into a final decision.

    * ``approved=False`` → stay local (continue with the locally-selected model);
      ``requires_api_consent`` is cleared.
    * ``approved=True`` but the ``IRA_ALLOW_EXTERNAL_API`` master switch is off →
      stay local (config forbids external use). External is NEVER used silently.
    * ``approved=True`` and allowed → switch to the external provider/model.

    Every outcome emits a structured consent audit event (safe metadata only).
    """
    env = os.environ if env is None else env

    if not approved:
        _audit_from_decision(decision, reason_code=CONSENT_DECLINED, consent_approved=False)
        return replace(
            decision,
            requires_api_consent=False,
            reason=decision.reason + "; user chose Local Mode only",
            provider="local",
        )

    # Approved or not, there is nothing external to switch to: the answer stays
    # on the machine, and the approval is recorded as blocked by design.
    _audit_from_decision(decision, reason_code=CONSENT_BLOCKED, consent_approved=True)
    return replace(
        decision,
        requires_api_consent=False,
        reason=decision.reason + "; external API disabled by config (no hosted model exists in IRA CARE) — staying local",
        provider="local",
    )


# ── Execution gate (defence in depth) ───────────────────────────────────────────
# A second, runtime guard so that an external decision can NEVER reach the network
# unless: (1) the decision is provider=="external" (only apply_consent(approved)
# produces it), (2) the IRA_ALLOW_EXTERNAL_API master switch is on, and (3) an
# external executor has been explicitly registered by the host application. No
# external executor ships by default, so Deep Intelligence Mode is decision-only
# out of the box — it cannot call out even if every other check were bypassed.

class ExternalExecutorNotConfigured(RuntimeError):
    """Raised when an external decision is run without a registered, allowed executor."""


def register_external_executor(fn) -> None:
    """Refused. No external model executor can be registered in IRA CARE (D-056):
    the only way words leave the machine would be here, and here is closed."""
    raise ExternalExecutorNotConfigured(
        "IRA CARE has no hosted model API: an external executor cannot be registered.")


def clear_external_executor() -> None:
    """Kept for older callers; there is never anything to clear."""
    return None


def run_decision(
    decision: "ModelRouteDecision",
    *,
    local_runner,
    system: str = "",
    prompt: str = "",
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Execute a decision. Local decisions call ``local_runner(decision)``.

    External decisions are gated again here: they require the master switch on AND
    a registered executor, else :class:`ExternalExecutorNotConfigured` is raised.
    External use is therefore never silent — there is no default code path to it.
    A blocked external run is recorded as an ``unavailable`` consent audit event.
    """
    env = os.environ if env is None else env
    if decision.provider == "local":
        return local_runner(decision)

    # provider == "external" can only come from a hand-built decision: refused.
    _audit_from_decision(
        decision, reason_code=CONSENT_UNAVAILABLE, consent_approved=True
    )
    raise ExternalExecutorNotConfigured(
        "External execution blocked: IRA CARE has no hosted model API and no external executor."
    )


__all__ = [
    # Consent / privacy config.
    "ALLOW_EXTERNAL_ENV",
    "REQUIRE_CONSENT_ENV",
    "PRIVACY_MODE_ENV",
    "EXTERNAL_PROVIDER_ENV",
    "EXTERNAL_MODEL_ENV",
    "DEFAULT_PRIVACY_MODE",
    "CONSENT_MESSAGE",
    "consent_message",
    "privacy_mode",
    "external_api_allowed",
    "consent_required",
    # Consent gate.
    "apply_consent",
    # Structured consent audit hook.
    "ConsentAuditEvent",
    "AuditSink",
    "CONSENT_OFFERED",
    "CONSENT_APPROVED",
    "CONSENT_DECLINED",
    "CONSENT_BLOCKED",
    "CONSENT_UNAVAILABLE",
    "record_consent_event",
    "register_consent_audit_sink",
    "reset_consent_audit_sink",
    # Execution gate.
    "ExternalExecutorNotConfigured",
    "register_external_executor",
    "clear_external_executor",
    "run_decision",
]
