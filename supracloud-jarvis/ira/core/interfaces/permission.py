"""Permission-decision interface.

The seam every tool call must pass through before it touches anything. Phase 4
implements the Constitution Engine and Permission Broker behind it; Phase 1
defines the shape so that code written now can depend on the interface rather
than on whichever guard happens to exist today.

Design rules encoded here:

  * **Deterministic, not prompted.** A decision is made by application code from
    a Principal, an action and a purpose. The model is never asked to enforce a
    rule about itself — it is the thing being constrained.
  * **Fail closed.** :data:`DENY_UNKNOWN` is what an unimplemented or
    unavailable broker returns. An unknown action, an expired approval or an
    unreachable permission service denies; it never falls through to allow.
  * **Purpose is mandatory.** "Why" is part of the request, not an afterthought,
    because the Action Receipt (Phase 7) has to state it and a purpose invented
    after the fact is not a reason.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from core.identity.principal import Principal


class Effect(str, Enum):
    """What may happen to a requested action."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_CONFIRMATION = "require_confirmation"
    ALLOW_FOR_SESSION = "allow_for_session"
    ALLOW_FOR_ROUTINE = "allow_for_routine"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """A proposed action, before anything has been done."""

    principal: Principal
    domain: str          # "email", "calendar", "files", "shell", ...
    action: str          # "send", "read", "delete", ...
    #: Why this is being attempted, in words a user could read on a receipt.
    purpose: str
    resource: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)
    #: True when the action was proposed by a model rather than asked for
    #: directly by the user. Untrusted-origin actions may be judged differently.
    model_proposed: bool = True

    def __post_init__(self) -> None:
        if not self.domain or not self.action:
            raise ValueError("ActionRequest requires both a domain and an action")
        if not self.purpose:
            raise ValueError(
                "ActionRequest requires a purpose — an action whose reason cannot "
                "be stated cannot be shown honestly on an Action Receipt"
            )


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """The answer, and the reason for it."""

    effect: Effect
    reason: str
    risk: RiskLevel = RiskLevel.MEDIUM
    #: Identifier of the rule or grant that decided this, for the audit trail.
    source: str = ""
    #: Set when effect is REQUIRE_CONFIRMATION.
    approval_request_id: Optional[str] = None

    @property
    def is_allowed(self) -> bool:
        """True only for effects that permit execution WITHOUT further approval.

        REQUIRE_CONFIRMATION is deliberately not allowed here: a caller that
        treats "ask the user" as "go ahead" is the bug this property prevents.
        """
        return self.effect in (
            Effect.ALLOW,
            Effect.ALLOW_FOR_SESSION,
            Effect.ALLOW_FOR_ROUTINE,
        )

    @property
    def needs_confirmation(self) -> bool:
        return self.effect is Effect.REQUIRE_CONFIRMATION


#: The decision returned when nothing authoritative could be established.
DENY_UNKNOWN = PermissionDecision(
    effect=Effect.DENY,
    reason="No permission decision could be established — denying (fail closed).",
    risk=RiskLevel.HIGH,
    source="fail-closed-default",
)


@runtime_checkable
class PermissionBroker(Protocol):
    """Decides whether a proposed action may proceed.

    Implementations MUST return DENY (not raise, not allow) for anything they do
    not understand, and MUST NOT execute the action themselves.
    """

    async def decide(self, request: ActionRequest) -> PermissionDecision:
        ...


class DenyAllBroker:
    """The Phase 1 default implementation.

    Denies everything with an honest reason. It exists so that code written
    against the interface has something real to depend on, and so that wiring a
    broker in before Phase 4 implements the rules cannot accidentally permit
    anything. It is not a placeholder that allows — the safe placeholder denies.
    """

    async def decide(self, request: ActionRequest) -> PermissionDecision:
        return PermissionDecision(
            effect=Effect.DENY,
            reason=(
                f"No Permission Broker is implemented yet (Phase 4). "
                f"Refusing {request.domain}.{request.action}."
            ),
            risk=RiskLevel.HIGH,
            source="DenyAllBroker",
        )


__all__ = [
    "Effect",
    "RiskLevel",
    "ActionRequest",
    "PermissionDecision",
    "PermissionBroker",
    "DenyAllBroker",
    "DENY_UNKNOWN",
]
