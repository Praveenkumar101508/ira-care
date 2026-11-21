"""The Tool Gateway — the only way a governed action reaches an executor.

    execute(request, ticket=..., executor=...)
        ↓  consume the ticket        forged / expired / replayed / mismatched → refuse
        ↓  Shadow World + Action Guard   (existing — simulation, snapshots, gates)
        ↓  the executor
        ↓  Trust Ledger + Action Receipt (existing)

── What this module is NOT ──────────────────────────────────────────────────

It is not a second safety system. IRA already has simulation (Shadow World),
blast-radius grading and the protected-path denylist (Action Guard), one-shot
owner-scoped approval tokens (`utils/approval.py`), a hash-chained audit log
(Trust Ledger) and Proof Cards. All of that keeps working exactly as it did and
does all the work it did before.

What was missing was a point where "has anyone actually decided this is
allowed?" is answered before any of it starts. That is the whole of this file.
The `ticket` parameter is required and unforgeable, so the question cannot be
skipped — not by forgetting, which is the realistic failure.

── Why the executor is passed in ────────────────────────────────────────────

The gateway never imports a connector. It takes a callable. Two reasons:

  1. It cannot become a hub that knows about Gmail, Drive and the filesystem —
     that hub would be the thing every future connector has to be wired into,
     and the wiring is what gets skipped under time pressure.
  2. The architectural test can then state a simple, checkable rule: connector
     modules are called from here and from nowhere else.
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Union

from core.governance.ticket import ExecutionTicket, TicketError
from core.interfaces.permission import ActionRequest, Effect, RiskLevel

logger = logging.getLogger("ira.governance.gateway")

Executor = Callable[[], Union[Any, Awaitable[Any]]]


class ExecutionRefused(RuntimeError):
    """The gateway refused to execute. Carries why, for the audit record."""

    def __init__(self, reason: str, *, stage: str = "gateway") -> None:
        super().__init__(reason)
        self.reason = reason
        self.stage = stage


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """What happened, in the shape an Action Receipt needs.

    Deliberately reports `ledger_recorded` honestly rather than assuming. A
    receipt that claims an audit entry exists when the ledger was unreachable is
    worse than one that admits the gap — the same call `actions/gated_execution.py`
    already makes.
    """

    executed: bool
    result: Any = None
    reason: str = ""
    stage: str = ""
    risk: RiskLevel = RiskLevel.MEDIUM
    ticket_id: Optional[str] = None
    simulated: bool = False
    ledger_recorded: bool = False
    #: Whether this action can be described as GOVERNED — i.e. whether the
    #: required Trust Ledger evidence for it exists durably (P10-05).
    #:
    #: Separate from `executed` because they answer different questions and the
    #: honest answer is sometimes "yes and no": the intent record is written
    #: before the side effect and the result record after it, so a failure of
    #: the second leaves an action that really did run and whose outcome was
    #: never recorded. That is not undoable, and calling it ungoverned is the
    #: only true thing left to say about it.
    #:
    #: Defaults False. A refusal is not governed either — nothing was done, so
    #: there is nothing to have governed — and defaulting True would make every
    #: future code path that forgets to set it claim the strongest property this
    #: dataclass has.
    governed: bool = False
    proof_card: Optional[dict] = None
    metadata: dict = field(default_factory=dict)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _record_ledger(
    kind: str, *, user_id: str, action_id: str, record: dict, principal=None
) -> bool:
    """Append to the existing Trust Ledger. Fail-soft, reported honestly.

    ── `principal` is not optional in spirit (P9-07) ───────────────────────

    It was missing until the product was actually wired to this module, and the
    omission was invisible for exactly as long as nothing called it. Without
    it, `append_record` falls back to the request ContextVar and then, in the
    private edition, to `PRIVATE_TENANT_ID`. So a governed action taken for
    tenant A wrote its audit evidence wherever the ambient context happened to
    point — in the private edition, to the private tenant; in cloud, nowhere at
    all, because `LedgerNotBound` is raised and the action is refused.

    The caller here always holds a verified principal: `execute` receives it on
    the `ActionRequest` and it is the same one whose Constitution authorised the
    action. Threading it makes the evidence land in the chain of the tenant that
    acted, which is the whole point of a per-tenant chain — and it is the
    difference between the fail-closed refusal being a safety property and being
    the reason the feature never worked.

    ── Fail-soft, as before ────────────────────────────────────────────────

    A momentarily-unreachable database must not lose a legitimate action, but
    the caller is told the entry is missing so the receipt does not claim it.

    `kind` MUST be one of `trust.ledger.LEDGER_KINDS` — `append_record` raises
    `ValueError` on anything else. Because this function is fail-soft, an
    invalid kind would be swallowed and reported as "ledger unavailable"
    forever, so the ledger would look intermittently broken rather than
    misused. `test_gateway_uses_real_ledger_kinds` pins the kinds this module
    passes against that set.
    """
    try:
        from trust.ledger import append_record

        await append_record(
            kind, user_id=user_id, action_id=action_id, task_id=None,
            record=record, principal=principal,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - fail-soft by design
        from trust.ledger import LedgerNotBound, LedgerTenantMismatch

        if isinstance(exc, (LedgerNotBound, LedgerTenantMismatch)):
            # Not an outage — no tenant context to write under, or two sources
            # disagreeing about which tenant this is. Neither clears by itself,
            # and `ledger_recorded: False` on a receipt is not enough on its own:
            # nothing downstream treats False as an error, which is precisely how
            # P9-02's defect stayed invisible for a phase.
            logger.error(
                "TRUST LEDGER NOT WRITTEN — this is a defect, not an outage "
                "(%s for %s): %s", kind, action_id, exc)
        else:
            logger.warning(
                "trust ledger unavailable (%s for %s): %s", kind, action_id, exc)
        return False


def _build_spec(request: ActionRequest, ticket: ExecutionTicket, shadow_kind: str):
    """The ActionSpec the existing Shadow World and Proof Card both consume.

    Built once and reused, so the proof card describes the spec that was
    actually simulated — `simulate()` attaches its report to `spec.simulation`,
    and `build_proof_card` reads it from there.
    """
    from actions.shadow_world import ActionSpec

    return ActionSpec(
        kind=shadow_kind,
        intended_action=(
            f"{request.domain}.{request.action} {request.resource}".strip()
        ),
        requested_by=request.principal.username or str(request.principal.user_id),
        risk=ticket.risk.value,
        objects_affected=[str(request.resource)] if request.resource else [],
        # Populated by the connector layer in Phase 7; empty here means "local
        # only", which is true of every executor this build wires up.
        data_leaving_device=[],
        tools_involved=[f"{request.domain}.{request.action}"],
    )


async def execute(
    request: ActionRequest,
    *,
    ticket: ExecutionTicket,
    executor: Executor,
    simulate: bool = True,
) -> ExecutionOutcome:
    """Run `executor`, but only if `ticket` authorises exactly `request`.

    `ticket` is keyword-only and has no default. There is no overload, no
    `ticket=None` path and no environment variable that skips it — the only way
    to call this function is with a ticket a permission decision minted.
    """
    # ── 1. The ticket ───────────────────────────────────────────────────────
    try:
        ticket.consume(request)
    except TicketError as exc:
        logger.warning(
            "execution refused for %s.%s: %s", request.domain, request.action, exc
        )
        return ExecutionOutcome(
            executed=False,
            reason=str(exc),
            stage="ticket",
            risk=ticket.risk if isinstance(ticket, ExecutionTicket) else RiskLevel.HIGH,
        )

    action_id = str(ticket.ticket_id)
    user_id = str(request.principal.user_id)

    # ── 2. The existing safety stack ────────────────────────────────────────
    #
    # Shadow World simulation and the Action Guard's grading run here, unchanged.
    # A simulation that fails refuses the action BEFORE the executor is called —
    # the existing contract, which this module preserves rather than reimplements.
    simulated = False
    spec = None
    from core.governance.policies import policy_for

    policy = policy_for(request.domain, request.action)
    if simulate and policy is not None and policy.side_effecting and policy.shadow_kind:
        spec = _build_spec(request, ticket, policy.shadow_kind)
        ok, why = await _simulate(spec)
        simulated = True
        if not ok:
            await _record_ledger(
                "security_event",
                user_id=user_id,
                action_id=action_id,
            principal=request.principal,
                record={
                    "event": "simulation_failed",
                    "domain": request.domain,
                    "action": request.action,
                    "reason": why,
                },
            )
            return ExecutionOutcome(
                executed=False,
                reason=f"Shadow World simulation refused this action: {why}",
                stage="simulation",
                risk=ticket.risk,
                ticket_id=action_id,
                simulated=True,
            )

    # ── 3. Intent goes into the ledger BEFORE the side effect ───────────────
    #
    # Ordering matters: an action that succeeds and then fails to be recorded
    # must still leave a trace of having been attempted. `action` is the
    # ledger's kind for a recorded intent (see trust.ledger.LEDGER_KINDS).
    intent_recorded = await _record_ledger(
        "action",
        user_id=user_id,
        action_id=action_id,
            principal=request.principal,
        record={
            "domain": request.domain,
            "action": request.action,
            "resource": request.resource,
            "purpose": request.purpose,
            "effect": ticket.effect.value,
            "risk": ticket.risk.value,
            "source": ticket.source,
            "model_proposed": request.model_proposed,
            "approval_request_id": ticket.approval_request_id,
        },
    )

    # ── 3a. No durable intent record, no side effect (P10-05) ───────────────
    #
    # The invariant this enforces:
    #
    #     GOVERNED SUCCESS  implies  REQUIRED TRUST LEDGER EVIDENCE EXISTS
    #
    # The intent write is the LAST point at which refusing costs nothing —
    # nothing has happened yet, so a refusal here leaves no unrecorded action
    # behind. One statement later the side effect has run and the choice is gone.
    #
    # This module used to continue and report `ledger_recorded: False`. That was
    # honest and it was not enough: nothing treats False as an error, so a
    # governed action executed with no audit entry and the receipt said so in a
    # field nobody read. Phase 9 found exactly that state — `ledger_recorded`
    # was False for EVERY cloud action, for a whole phase.
    #
    # The cost is stated rather than hidden: while the ledger is unreachable, no
    # governed action runs. That is the correct direction for a governance
    # control — an audit trail that yields under load is not an audit trail —
    # and it is the owner's instruction: if the evidence cannot be made durable,
    # refuse explicitly.
    if not intent_recorded:
        logger.error(
            "REFUSING %s.%s: the Trust Ledger did not accept the intent record, "
            "so this action cannot be governed",
            request.domain, request.action)
        return ExecutionOutcome(
            executed=False,
            reason=(
                "Refusing to act: the Trust Ledger did not record this action's "
                "intent, and an action that cannot be recorded cannot be "
                "governed. Nothing was executed."
            ),
            stage="ledger",
            risk=ticket.risk,
            ticket_id=action_id,
            simulated=simulated,
            ledger_recorded=False,
            governed=False,
        )

    # ── 4. Execute ──────────────────────────────────────────────────────────
    try:
        result = await _maybe_await(executor())
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        await _record_ledger(
            "execution",
            user_id=user_id,
            action_id=action_id,
            principal=request.principal,
            record={"status": "error", "error": type(exc).__name__},
        )
        logger.exception("executor failed for %s.%s", request.domain, request.action)
        return ExecutionOutcome(
            executed=False,
            reason=f"The action failed: {type(exc).__name__}",
            stage="executor",
            risk=ticket.risk,
            ticket_id=action_id,
            simulated=simulated,
            ledger_recorded=intent_recorded,
        )

    # ── 5. Result + Proof Card ──────────────────────────────────────────────
    result_recorded = await _record_ledger(
        "execution",
        user_id=user_id,
        action_id=action_id,
            principal=request.principal,
        record={"status": "ok", "summary": _summarise(result)},
    )

    if not result_recorded:
        # The side effect HAS happened and cannot be undone, so `executed` stays
        # True — reporting otherwise would be a second lie to fix the first. What
        # changes is the claim: this action is not governed, because half its
        # evidence is missing, and the reason says which half.
        logger.error(
            "%s.%s EXECUTED BUT NOT GOVERNED: the intent was recorded and the "
            "outcome was not. The action cannot be undone; the gap is real.",
            request.domain, request.action)

    return ExecutionOutcome(
        executed=True,
        result=result,
        reason=(
            ticket.reason if result_recorded else
            "Executed, but the Trust Ledger did not record the outcome. The "
            "intent is on record and the result is not, so this action is NOT "
            "governed and its receipt must not claim to be."
        ),
        stage="complete" if result_recorded else "evidence_incomplete",
        risk=ticket.risk,
        ticket_id=action_id,
        simulated=simulated,
        ledger_recorded=intent_recorded and result_recorded,
        governed=bool(intent_recorded and result_recorded),
        proof_card=_proof_card(request, ticket, spec),
    )


async def _simulate(spec) -> tuple[bool, str]:
    """Run the existing Shadow World simulation. Never executes anything.

    `shadow_world.simulate` reports `{"ok": bool, "detail": str}` and attaches
    the report to `spec.simulation`. Its own contract is already fail-closed:
    with no registered simulator for a kind it returns `ok=False` with an honest
    "not simulated" detail, which blocks the action rather than letting it
    through unsimulated. This wrapper preserves that — it does not reinterpret
    a missing simulator as success.
    """
    try:
        from actions.shadow_world import simulate as shadow_simulate

        report = await _maybe_await(shadow_simulate(spec))
        if isinstance(report, dict) and report.get("ok"):
            return True, ""
        detail = (
            str(report.get("detail", "simulation did not succeed"))
            if isinstance(report, dict)
            else "simulator returned an unexpected shape"
        )
        return False, detail
    except Exception as exc:  # noqa: BLE001
        # A simulator that cannot run is not permission to skip simulation. The
        # action is refused — fail closed, per the brief's §3.5.
        logger.warning("shadow simulation unavailable: %s", exc)
        return False, f"simulation unavailable ({type(exc).__name__}); refusing"


def _summarise(result: Any) -> str:
    """A short, safe summary. Never the raw payload.

    `tool_calls.result_summary` is explicitly documented in 023 as "Result
    summary only. Never credentials, never raw connector payloads." A connector
    response can contain message bodies and tokens; truncating a repr of it is
    not redaction, so only the shape is recorded.
    """
    if result is None:
        return "no result"
    if isinstance(result, bool):
        return str(result)
    if isinstance(result, (int, float)):
        return str(result)
    if isinstance(result, (list, tuple, set)):
        return f"{len(result)} item(s)"
    if isinstance(result, dict):
        return f"object with {len(result)} field(s)"
    return type(result).__name__


def _proof_card(
    request: ActionRequest, ticket: ExecutionTicket, spec=None
) -> dict:
    """Build a Proof Card using the EXISTING builder.

    `build_proof_card` takes an `ActionSpec`, so when the action was simulated
    the same spec is reused — the card then reports the simulation that actually
    ran, rather than a second description of it.

    The governance facts the card cannot know (which rule decided this, whether
    an approval was involved) are merged on top under `governance`. If the
    builder is unavailable the governance payload is returned alone: a card is
    evidence, and evidence that fails to render should degrade to less
    information, never to no action.
    """
    governance = {
        "action": f"{request.domain}.{request.action}",
        "resource": request.resource,
        "purpose": request.purpose,
        "effect": ticket.effect.value,
        "risk": ticket.risk.value,
        "decided_by": ticket.source,
        "reason": ticket.reason,
        "approval_request_id": ticket.approval_request_id,
        "ticket_id": str(ticket.ticket_id),
        "model_proposed": request.model_proposed,
    }
    if spec is None:
        return governance
    try:
        from trust.proof_card import build_proof_card

        card = build_proof_card(spec) or {}
        card["governance"] = governance
        return card
    except Exception:  # noqa: BLE001 - the card is evidence, not a dependency
        logger.warning("proof card builder unavailable; returning governance facts only")
        return governance


__all__ = ["ExecutionOutcome", "ExecutionRefused", "Executor", "execute"]
