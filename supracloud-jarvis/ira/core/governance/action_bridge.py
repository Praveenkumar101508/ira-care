"""The bridge from a product `ActionSpec` to the governed path (P9-07).

── What this closes ────────────────────────────────────────────────────────

Phase 9's plan recorded, in finding F9-6:

    "What is NOT done here: routing the shipped surfaces through
     broker → ticket → gateway.execute. That is the wiring Phase 9's exit
     criteria imply and it is larger than a batch."

It was never done, and Phase 9 was reported closed anyway. The governance
machinery existed and nothing in the product reached it:

    product action → stage_action/confirm_action → _refuse_in_cloud → STOP

    Constitution / broker / ticket / gateway.execute      ← existed, uncalled

That is a *safe refusal* architecture, not a governed-agent one. Cloud was
safe because governed execution did not work, which is a different property
from governed execution working.

This module is the missing segment, and only that. It invents no second
decision path: it converts an `ActionSpec` the product already builds into the
`ActionRequest` the broker already consumes, asks the tenant's own Constitution
and grants, and hands the ticket to `gateway.execute` — which already carries
Phase 10's guarantees (intent recorded before the side effect, refusal when it
cannot be, `governed=False` when the outcome record is lost).

── Why the caller names the governed action ────────────────────────────────

`ActionSpec.kind` is a Shadow World kind (`email_send`, `calendar_change`), and
`ToolPolicy` carries the same value in `shadow_kind`. It is tempting to map one
to the other and save the caller a parameter. It does not work and must not be
faked: `calendar_change` is the shadow kind of BOTH `calendar.create` and
`calendar.delete`, whose policies differ in risk and in maximum effect. A
mapping from kind alone would silently grade a delete as a create.

So the caller names `(domain, action)` — it always knows, at every one of the
five sites — and this module CHECKS that pair against the spec it was handed.
A caller naming `memory.read` while carrying an `email_send` spec is refused,
not reconciled. That check is the point of passing both.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional, Union

from core.governance import gateway
from core.governance.broker import BrokerDecision
from core.governance.gateway import ExecutionOutcome
from core.governance.policies import policy_for
from core.identity.principal import Principal
from core.interfaces.permission import ActionRequest, Effect, RiskLevel

logger = logging.getLogger("ira.governance.bridge")

Executor = Callable[[], Union[Any, Awaitable[Any]]]


class UngovernableAction(RuntimeError):
    """The spec cannot be turned into a governed request.

    Raised rather than returned, and never caught into a refusal by this
    module: every case is a defect in the CALLER — a governed pair that does
    not exist in this build, or one that contradicts the spec beside it. A
    refusal would let a mis-wired call site look like a denied action, which is
    the failure mode Phase 9 spent a batch on.
    """


def request_for_spec(
    spec: Any,
    *,
    principal: Principal,
    domain: str,
    action: str,
    purpose: Optional[str] = None,
) -> ActionRequest:
    """The `ActionRequest` for a product `ActionSpec`, or raise.

    `purpose` defaults to the spec's `intended_action`, which is already the
    human-readable sentence the product shows on a receipt. `ActionRequest`
    refuses an empty purpose, so a spec with nothing to say about itself cannot
    become a governed request at all.
    """
    policy = policy_for(domain, action)
    if policy is None:
        raise UngovernableAction(
            f"{domain}.{action} has no tool policy in this build. The broker "
            f"would deny it as unclassified, but the caller naming a pair that "
            f"does not exist is a wiring defect and is reported as one."
        )

    # The pair must describe the spec beside it. Checked only when the policy
    # HAS a shadow kind: read-only policies legitimately have none, and
    # requiring one would make `email.read` ungovernable.
    spec_kind = getattr(spec, "kind", None)
    if policy.shadow_kind and spec_kind and policy.shadow_kind != spec_kind:
        raise UngovernableAction(
            f"the caller asked to govern {domain}.{action} (shadow kind "
            f"{policy.shadow_kind!r}) while carrying an ActionSpec of kind "
            f"{spec_kind!r}. One of the two is wrong, and guessing which would "
            f"grade the action that was not requested."
        )

    return ActionRequest(
        principal=principal,
        domain=domain,
        action=action,
        purpose=purpose or getattr(spec, "intended_action", "") or f"{domain}.{action}",
        resource=(getattr(spec, "objects_affected", None) or [""])[0],
        parameters={
            "action_id": getattr(spec, "action_id", ""),
            "objects_affected": list(getattr(spec, "objects_affected", []) or []),
            "data_leaving_device": list(
                getattr(spec, "data_leaving_device", []) or []),
        },
        # These specs are built by a product route from a request the user
        # made, not proposed by a model. `model_proposed=True` would raise the
        # risk grade of every one of them and is simply untrue here; a surface
        # that IS model-driven passes the flag itself.
        model_proposed=False,
    )


async def decide(
    spec: Any,
    *,
    principal: Principal,
    domain: str,
    action: str,
    purpose: Optional[str] = None,
    confirmation: Optional[str] = None,
) -> tuple[ActionRequest, BrokerDecision]:
    """Ask THIS TENANT's Constitution and grants. Records the decision.

    The broker is loaded through `store.load_broker`, which reads rules and
    grants over `tenant_connection` — so the rules applied are the ones
    belonging to the principal's tenant, enforced by RLS rather than by a
    predicate this module remembers to write.
    """
    from core.governance.store import load_broker, record_decision

    request = request_for_spec(
        spec, principal=principal, domain=domain, action=action, purpose=purpose)
    broker = await load_broker(principal)
    outcome = broker.evaluate(request, confirmation=confirmation)

    # Fail-soft, and deliberately so: `governance_decisions` is a product
    # record of what was decided, NOT the audit evidence for what was done —
    # that is the Trust Ledger's, and `gateway.execute` refuses when it cannot
    # write it. Losing this row must not deny an action the Constitution
    # allowed; losing the ledger record must.
    try:
        await record_decision(principal, request, outcome)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "governance decision not recorded for %s.%s: %s", domain, action, exc)

    return request, outcome


async def execute_spec(
    spec: Any,
    *,
    principal: Principal,
    domain: str,
    action: str,
    executor: Executor,
    purpose: Optional[str] = None,
    confirmation: Optional[str] = None,
    simulate: bool = True,
) -> ExecutionOutcome:
    """The whole governed path for one product action.

        ActionSpec → ActionRequest → Constitution/grants → ticket
                   → gateway.execute → Trust Ledger → result

    Returns an `ExecutionOutcome` in every case, including refusal, so a caller
    cannot distinguish "denied" from "failed" by whether an exception escaped
    and cannot accidentally treat one as the other.
    """
    request, outcome = await decide(
        spec, principal=principal, domain=domain, action=action, purpose=purpose,
        confirmation=confirmation)

    if not outcome.may_execute:
        # No ticket, so `gateway.execute` is not merely skipped — it CANNOT be
        # called. Nothing runs, and the reason is the tenant's own rule.
        logger.info(
            "governed action refused for %s.%s: %s (%s)",
            domain, action, outcome.decision.reason, outcome.decision.effect.value)
        return ExecutionOutcome(
            executed=False,
            reason=outcome.decision.reason,
            stage="broker",
            risk=outcome.decision.risk,
            governed=False,
            metadata={
                "effect": outcome.decision.effect.value,
                "source": outcome.decision.source,
                "action_id": getattr(spec, "action_id", ""),
            },
        )

    return await gateway.execute(
        request, ticket=outcome.ticket, executor=executor, simulate=simulate)


def outcome_as_response(outcome: ExecutionOutcome, spec: Any) -> dict[str, Any]:
    """The `gated_execution` response shape, from a governed outcome.

    The five call sites already branch on `status`, so the governed path speaks
    their vocabulary rather than making every route learn a second one. The one
    field that is new is `governed` — and it is reported even on success,
    because P10-05 made "executed" and "governed" different questions and a
    response that answers only the first is the receipt Phase 9 found.
    """
    action_id = getattr(spec, "action_id", "")
    if outcome.executed:
        return {
            "status": "executed",
            "action_id": action_id,
            "result": outcome.result,
            "ledger_recorded": outcome.ledger_recorded,
            "governed": outcome.governed,
            "ticket_id": outcome.ticket_id,
            "risk": outcome.risk.value if isinstance(outcome.risk, RiskLevel)
            else str(outcome.risk),
            "proof_card": outcome.proof_card,
            **({"detail": outcome.reason} if not outcome.governed else {}),
        }
    return {
        "status": "blocked",
        "action_id": action_id,
        "reason": outcome.reason,
        "stage": outcome.stage,
        "governed": False,
        "risk": outcome.risk.value if isinstance(outcome.risk, RiskLevel)
        else str(outcome.risk),
    }


__all__ = [
    "Effect",
    "UngovernableAction",
    "decide",
    "execute_spec",
    "outcome_as_response",
    "request_for_spec",
]
