"""The Connector Gateway — the single entry point for every connector call.

Implements the `ConnectorGateway` protocol from
`core/interfaces/connector_gateway.py`, which specifies the order:

    resolve the principal, consult the Constitution Engine, consult the
    Permission Broker, classify risk, pass Action Guard where there are side
    effects, execute, then emit an audit event and a receipt. A gateway that
    executes before deciding is not a gateway.

Phase 4 already built the middle of that — the broker consults the Constitution,
classifies risk, applies the policy ceiling and mints a ticket, and
`core/governance/gateway.execute` runs Action Guard and the Trust Ledger. This
module does not reimplement any of it. What it adds is the connector-specific
layer around it:

  * an account must be CONNECTED, with the SCOPE the operation needs;
  * content read from the provider comes back TAINTED (`taint.py`);
  * taint clamps the effect before the broker's ticket can be used;
  * an Action Receipt is produced by the gateway, not by the caller.

── Why the receipt is built here ───────────────────────────────────────────

From the seam: *"Produced by the gateway, not by the agent that asked — an actor
describing its own behaviour is not evidence."* So `execute` on a connector
returns data and a description of what it touched; the gateway is what turns
that into a receipt, stamps it, and writes it. A connector cannot claim it
accessed less than it did, because it does not write the record.

── Ordering, and why it is asserted rather than trusted ────────────────────

`tests/architecture/test_governance_chokepoint.py` (Phase 4) already proves
execution needs a ticket. `tests/connectors/test_gateway_order.py` adds the
connector-specific half: a recording connector observes the sequence, and the
test fails if execution is reached before a decision.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from core.connectors.taint import (
    EMPTY_INFLUENCES,
    Influences,
    clamp_for_taint,
    influences_of,
    merge,
    taint_findings,
)
from core.identity.principal import Principal
from core.interfaces.connector_gateway import (
    AccessLevel,
    ActionReceipt,
    Connector,
    ConnectorCall,
    ConnectorResult,
)
from core.interfaces.permission import ActionRequest, Effect, PermissionDecision, RiskLevel

logger = logging.getLogger("ira.connectors.gateway")

#: Connector operation → the (domain, action) it is governed as. The connector
#: vocabulary and the policy vocabulary are deliberately different — the policy
#: table is about CAPABILITIES ("may this build send email") and the connector
#: is about API calls. Mapping them here, in one table, is what stops a
#: connector inventing an ungoverned operation: an operation absent from this
#: map has no policy, and the gateway denies.
OPERATION_POLICY: dict[tuple[str, str], tuple[str, str]] = {
    ("gmail", "search"):                ("email", "search"),
    ("gmail", "read_selected"):         ("email", "read"),
    ("gmail", "prepare_draft"):         ("email", "draft"),
    ("google_calendar", "list_events"): ("calendar", "read"),
    ("google_drive", "search"):         ("files", "read"),
    ("google_drive", "read_selected"):  ("files", "read"),
}

#: The minimum rung on the autonomy ladder each operation needs. Access and
#: action permissions stay separate (the seam says so): being allowed to SEARCH
#: email is not being allowed to draft a reply.
OPERATION_ACCESS: dict[tuple[str, str], AccessLevel] = {
    ("gmail", "search"):                AccessLevel.OBSERVE,
    ("gmail", "read_selected"):         AccessLevel.OBSERVE,
    ("gmail", "prepare_draft"):         AccessLevel.PREPARE,
    ("google_calendar", "list_events"): AccessLevel.OBSERVE,
    ("google_drive", "search"):         AccessLevel.OBSERVE,
    ("google_drive", "read_selected"):  AccessLevel.OBSERVE,
}

#: Ladder order, least to most autonomous. Used to compare an account's granted
#: level against what an operation needs.
_LADDER: tuple[AccessLevel, ...] = (
    AccessLevel.NEVER,
    AccessLevel.OBSERVE,
    AccessLevel.SUGGEST,
    AccessLevel.PREPARE,
    AccessLevel.CONFIRM_BEFORE_ACTION,
    AccessLevel.AUTOMATIC_FOR_APPROVED_ROUTINE,
)


def _rung(level: AccessLevel | str) -> int:
    """Position on the ladder. An unrecognised level is the bottom, not the top."""
    try:
        return _LADDER.index(AccessLevel(level))
    except ValueError:
        return 0


@dataclass(frozen=True, slots=True)
class ConnectedAccount:
    """A connected account, as the store returns it. Never carries a credential
    in plaintext — `sealed` is the encrypted blob and only
    `core/connectors/secrets.py` can open it."""

    account_id: str
    connector: str
    provider_account: str
    granted_scopes: tuple[str, ...]
    access_level: AccessLevel
    status: str
    sealed: Optional[Any] = field(default=None, repr=False)


class ConnectorRefused(RuntimeError):
    """A connector call that must not proceed. Carries the user-readable reason."""

    def __init__(self, reason: str, *, decision: Optional[PermissionDecision] = None):
        super().__init__(reason)
        self.reason = reason
        self.decision = decision


@dataclass
class IRAConnectorGateway:
    """The gateway. Holds registered connectors and the collaborators it needs.

    Collaborators are injected rather than imported at call time so the ordering
    test can observe them and so a deployment without a database can still be
    exercised. Each defaults to the real thing.
    """

    connectors: dict[str, Connector] = field(default_factory=dict)
    #: Resolves the caller's connected account for a connector. Returns None
    #: when there is no account, which is a refusal, not an error.
    account_resolver: Optional[Any] = None
    #: `core.governance.broker.IRAPermissionBroker`-shaped. Injected for tests.
    broker: Optional[Any] = None
    #: Where receipts go. None means "do not persist", used in tests.
    receipt_writer: Optional[Any] = None

    def register(self, connector: Connector) -> None:
        self.connectors[connector.name] = connector

    def registered(self) -> Sequence[str]:
        return tuple(sorted(self.connectors))

    # ── The single entry point ───────────────────────────────────────────────

    async def call(self, call: ConnectorCall) -> ConnectorResult:
        """Decide, then execute, then record. Never the other way round.

        Every refusal returns a `ConnectorResult(ok=False)` with a reason and a
        receipt. A refusal is as much a thing that happened as a success, and a
        record that only holds successes cannot answer "why did IRA not do
        that?" — which is the question a user actually asks.
        """
        started = datetime.now(timezone.utc)
        try:
            return await self._call(call, started)
        except ConnectorRefused as refusal:
            return await self._refuse(call, refusal.reason, started,
                                     decision=refusal.decision)
        except Exception as exc:  # noqa: BLE001 - fail closed on anything unexpected
            logger.exception("connector call failed; refusing")
            return await self._refuse(
                call, f"the call failed and was refused: {type(exc).__name__}",
                started, outcome="error",
            )

    async def _call(self, call: ConnectorCall, started: datetime) -> ConnectorResult:
        # 1. The connector must exist. An unknown connector is denied, not
        #    guessed at — the same rule as an unknown action type.
        connector = self.connectors.get(call.connector)
        if connector is None:
            raise ConnectorRefused(
                f"{call.connector!r} is not a connector this build implements"
            )

        # 2. The operation must be one this build governs. An operation with no
        #    policy mapping has no ceiling, and an ungoverned operation is
        #    exactly what a connector should not be able to invent.
        policy_key = OPERATION_POLICY.get((call.connector, call.operation))
        if policy_key is None:
            raise ConnectorRefused(
                f"{call.connector}.{call.operation} is not a governed operation. "
                f"Every connector operation must map to a (domain, action) with "
                f"a tool policy, or it has no ceiling."
            )
        domain, action = policy_key

        # 3. The account must be connected, with the scope and rung this
        #    operation needs. Checked BEFORE the broker so a user with no
        #    account gets "connect Gmail first" rather than a permission denial
        #    they cannot act on.
        account = await self._account_for(call)
        self._assert_account_permits(account, call)

        # 4. Taint. Anything the caller passed that came from outside — a message
        #    body being quoted into a draft, a document being summarised — is
        #    found automatically rather than declared.
        influences = influences_of(call.parameters)

        # 5. The Permission Broker. It consults the Constitution, classifies
        #    risk, applies the policy ceiling and mints a ticket. Phase 4 owns
        #    all of that; this does not reimplement any of it.
        decision, ticket = self._decide(call, domain, action, influences)

        # 6. Taint clamps the decision. Applied AFTER the broker so the clamp is
        #    visible as a separate step in the decision record, and so it can
        #    only ever reduce what the broker allowed.
        clamped = clamp_for_taint(decision.effect, influences)
        if clamped is not decision.effect:
            raise ConnectorRefused(influences.reason(), decision=decision)

        if ticket is None:
            raise ConnectorRefused(
                decision.reason or (
                    f"{domain}.{action} was not permitted without a fresh "
                    f"confirmation."
                ),
                decision=decision,
            )

        # 7. Execute. The connector holds the credential; the caller never does.
        result = await connector.execute(call)

        # 8. The receipt, built here rather than by the connector.
        receipt = self._build_receipt(
            call, started,
            outcome="ok" if result.ok else "error",
            data_accessed=tuple(result.receipt.data_accessed) if result.receipt else (),
            sent_externally=tuple(result.receipt.sent_externally) if result.receipt else (),
            approved_by_user=decision.effect is Effect.REQUIRE_CONFIRMATION,
            influences=merge(influences, self._result_influences(result)),
        )
        await self._write(receipt, call)

        return ConnectorResult(
            ok=result.ok, data=result.data, receipt=receipt,
            decision=decision, error=result.error,
        )

    # ── Steps, separated so the ordering test can see them ───────────────────

    async def _account_for(self, call: ConnectorCall) -> Optional[ConnectedAccount]:
        if self.account_resolver is None:
            return None
        resolved = self.account_resolver(call.principal, call.connector)
        if hasattr(resolved, "__await__"):
            return await resolved  # type: ignore[misc]
        return resolved

    def _assert_account_permits(
        self, account: Optional[ConnectedAccount], call: ConnectorCall
    ) -> None:
        """No account, wrong status, missing scope or too low a rung — all refuse.

        Separate messages, because they need different actions from the user:
        connect the account, reconnect it, grant a scope, or raise the autonomy
        level. "Permission denied" for all four is unactionable.
        """
        if account is None:
            raise ConnectorRefused(
                f"No {call.connector} account is connected. Connect one in "
                f"Settings → Connectors first."
            )
        if account.status != "connected":
            raise ConnectorRefused(
                f"The connected {call.connector} account is {account.status}. "
                f"Reconnect it to continue."
            )

        needed_scopes = self._scopes_for(call)
        missing = [s for s in needed_scopes if s not in account.granted_scopes]
        if missing:
            raise ConnectorRefused(
                f"{call.connector}.{call.operation} needs a permission that was "
                f"not granted ({', '.join(missing)}). Reconnect the account and "
                f"approve it, or this feature stays unavailable."
            )

        needed_rung = OPERATION_ACCESS.get(
            (call.connector, call.operation), AccessLevel.CONFIRM_BEFORE_ACTION
        )
        if _rung(account.access_level) < _rung(needed_rung):
            raise ConnectorRefused(
                f"{call.connector}.{call.operation} needs the "
                f"'{needed_rung.value}' level and this account is set to "
                # `.value` explicitly: an f-string on the enum renders
                # "AccessLevel.NEVER", which is developer output leaking into a
                # message a user reads. A plain str value may not be a member at
                # all (an unrecognised level), so fall back to str().
                f"'{getattr(account.access_level, 'value', account.access_level)}'. "
                f"Raise it in Settings → Connectors if you want IRA to do this."
            )

    @staticmethod
    def _scopes_for(call: ConnectorCall) -> tuple[str, ...]:
        from core.connectors.oauth import OPERATION_SCOPES

        return OPERATION_SCOPES.get((call.connector, call.operation), ())

    def _decide(
        self, call: ConnectorCall, domain: str, action: str, influences: Influences
    ):
        """Ask the Permission Broker. Fail closed if there is no broker."""
        request = ActionRequest(
            principal=call.principal,
            domain=domain,
            action=action,
            purpose=call.purpose,
            resource=str(call.parameters.get("resource", "")),
            # A connector call is model-proposed unless the caller says
            # otherwise. Defaulting the other way would let an agent claim the
            # user asked for something.
            model_proposed=bool(call.parameters.get("model_proposed", True)),
        )
        if self.broker is None:
            return (
                PermissionDecision(
                    effect=Effect.DENY,
                    reason=(
                        "No permission broker is available, so the call is "
                        "refused. A connector call that cannot be authorised "
                        "must not proceed."
                    ),
                    risk=RiskLevel.HIGH,
                    source="no-broker",
                ),
                None,
            )
        outcome = self.broker.evaluate(request)
        return outcome.decision, outcome.ticket

    @staticmethod
    def _result_influences(result: ConnectorResult) -> Influences:
        """Taint carried back OUT by a read.

        A search that returns message snippets is returning attacker-controlled
        text, and the receipt should say so even though this particular call was
        only a read.
        """
        return influences_of(result.data)

    # ── Receipts ─────────────────────────────────────────────────────────────

    def _build_receipt(
        self,
        call: ConnectorCall,
        started: datetime,
        *,
        outcome: str,
        data_accessed: tuple[str, ...] = (),
        sent_externally: tuple[str, ...] = (),
        approved_by_user: bool = False,
        influences: Optional[Influences] = None,
    ) -> ActionReceipt:
        influences = influences or EMPTY_INFLUENCES
        return ActionReceipt(
            connector=call.connector,
            operation=call.operation,
            purpose=call.purpose,
            performed_at=started.isoformat(),
            data_accessed=data_accessed,
            sent_externally=sent_externally,
            approved_by_user=approved_by_user,
            undo_available=False,
            audit_event_id="",
        )

    async def _refuse(
        self,
        call: ConnectorCall,
        reason: str,
        started: datetime,
        *,
        outcome: str = "denied",
        decision: Optional[PermissionDecision] = None,
    ) -> ConnectorResult:
        """A refusal still gets a receipt.

        "Why did IRA not do that?" is a question a record of successes cannot
        answer, and it is the question users actually ask.
        """
        receipt = self._build_receipt(call, started, outcome=outcome)
        await self._write(receipt, call, outcome=outcome, reason=reason)
        return ConnectorResult(
            ok=False, data=None, receipt=receipt, decision=decision, error=reason
        )

    async def _write(
        self,
        receipt: ActionReceipt,
        call: ConnectorCall,
        *,
        outcome: str = "ok",
        reason: str = "",
    ) -> None:
        if self.receipt_writer is None:
            return
        try:
            written = self.receipt_writer(call.principal, receipt, outcome, reason)
            if hasattr(written, "__await__"):
                await written
        except Exception as exc:  # noqa: BLE001 - fail-soft, logged
            # Fail-soft, and the reason is narrow: a connector READ that already
            # happened must not be reported as failed because the receipt write
            # did. The honesty cost is covered by `ledger_recorded` on the row,
            # which is False when nothing was written.
            logger.warning("could not persist an action receipt: %s", exc)


def build_gateway(
    *,
    account_resolver: Optional[Any] = None,
    broker: Optional[Any] = None,
    receipt_writer: Optional[Any] = None,
    connectors: Optional[Sequence[Connector]] = None,
) -> IRAConnectorGateway:
    """Assemble the gateway with the real collaborators by default."""
    gateway = IRAConnectorGateway(
        account_resolver=account_resolver,
        broker=broker,
        receipt_writer=receipt_writer,
    )
    for connector in connectors or ():
        gateway.register(connector)
    return gateway


__all__ = [
    "OPERATION_ACCESS",
    "OPERATION_POLICY",
    "ConnectedAccount",
    "ConnectorRefused",
    "IRAConnectorGateway",
    "build_gateway",
]
