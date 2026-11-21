"""Connector gateway interface — the only thing that may hold a credential.

The rule this module encodes, from the architecture brief:

    The model must never directly access OAuth credentials, database
    credentials, email tokens, calendar tokens, user secrets, payment
    credentials or cloud provider secrets.

So connectors are not called by agents. An agent describes what it wants; the
gateway checks the Permission Broker, executes with credentials the agent never
sees, and returns a result plus the evidence needed for an Action Receipt.

Phase 1 defines the seam. Phase 7 implements Gmail, Calendar and Drive behind it.
IRA Private's existing local executors keep working unchanged — they are wired
in later, deliberately, rather than being rerouted mid-phase.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from core.identity.principal import Principal
from core.interfaces.permission import PermissionDecision


class AccessLevel(str, Enum):
    """The autonomy ladder. Access and action permissions stay separate: being
    allowed to SEARCH email is not being allowed to SEND it."""

    NEVER = "never"
    OBSERVE = "observe"
    SUGGEST = "suggest"
    PREPARE = "prepare"
    CONFIRM_BEFORE_ACTION = "confirm_before_action"
    AUTOMATIC_FOR_APPROVED_ROUTINE = "automatic_for_approved_routine"


@dataclass(frozen=True, slots=True)
class ConnectorCall:
    """A request to do something through a connector."""

    principal: Principal
    connector: str      # "gmail", "google_calendar", "google_drive", ...
    operation: str      # "search", "read_selected", "prepare_draft", "send"
    purpose: str
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.purpose:
            raise ValueError("ConnectorCall requires a purpose for the Action Receipt")


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    """Evidence of what actually happened.

    Produced by the gateway, not by the agent that asked — an actor describing
    its own behaviour is not evidence.
    """

    connector: str
    operation: str
    purpose: str
    performed_at: str
    #: What was read or touched, in user-readable terms.
    data_accessed: Sequence[str] = ()
    #: What left the machine, and to whom. Empty means nothing did.
    sent_externally: Sequence[str] = ()
    model_provider: str = ""
    approved_by_user: bool = False
    undo_available: bool = False
    audit_event_id: str = ""


@dataclass(frozen=True, slots=True)
class ConnectorResult:
    ok: bool
    #: Payload for the caller. MUST NOT contain credentials.
    data: Any = None
    receipt: Optional[ActionReceipt] = None
    decision: Optional[PermissionDecision] = None
    error: str = ""


@runtime_checkable
class Connector(Protocol):
    """One integration. Implementations hold credentials; callers never do."""

    @property
    def name(self) -> str:
        ...

    def supported_operations(self) -> Mapping[str, AccessLevel]:
        """Operation → the minimum access level it requires."""
        ...

    async def execute(self, call: ConnectorCall) -> ConnectorResult:
        ...

    async def disconnect(self, principal: Principal) -> None:
        """Revoke and delete stored credentials. Must be immediate."""
        ...


@runtime_checkable
class ConnectorGateway(Protocol):
    """The single entry point for every connector call.

    Implementations MUST, in order: resolve the principal, consult the
    Constitution Engine, consult the Permission Broker, classify risk, pass
    Action Guard where there are side effects, execute, then emit an audit event
    and a receipt. A gateway that executes before deciding is not a gateway.
    """

    async def call(self, call: ConnectorCall) -> ConnectorResult:
        ...

    def registered(self) -> Sequence[str]:
        ...


class UnavailableConnectorGateway:
    """Phase 1 default: refuses every call.

    As with DenyAllBroker, the safe placeholder denies. Wiring this in early
    cannot accidentally grant access to anything.
    """

    async def call(self, call: ConnectorCall) -> ConnectorResult:
        return ConnectorResult(
            ok=False,
            error=(
                f"No connector gateway is implemented yet (Phase 7). "
                f"Refusing {call.connector}.{call.operation}."
            ),
        )

    def registered(self) -> Sequence[str]:
        return ()


__all__ = [
    "AccessLevel",
    "ConnectorCall",
    "ActionReceipt",
    "ConnectorResult",
    "Connector",
    "ConnectorGateway",
    "UnavailableConnectorGateway",
]
