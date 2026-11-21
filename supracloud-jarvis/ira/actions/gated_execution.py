"""
actions/gated_execution.py — the integrated action gate (PR 73 wiring).

Composes the pieces built in PR 73 into ONE flow that real executors call:

    Shadow World simulate → owner gate + approval token → execute_gate →
    run the side effect → Trust Ledger records → Proof Card

This is the bridge that makes the safety layers actually protect live
actions. It REUSES utils/approval.py (the protected chokepoint) unchanged —
the guardrail still stores the pending action and enforces one-shot,
owner-scoped confirmation; this module interposes simulation before the
draft and the execute-gate + ledger recording around the confirm.

Behaviour contract:
  * A non-owner never reaches simulation or execution.
  * While lockdown refuses an action (trust/lockdown_enforcement.py), it is
    refused at stage AND at confirm, before simulation, ledger or token.
  * Critical actions and failed simulations are refused at STAGE time
    (no token is even issued).
  * At confirm time execute_gate() must pass or the action is refused and a
    security_event is recorded — no medium+ action runs unsimulated/unapproved.
  * The intent and approval records are written BEFORE the side effect, and
    since P1-09 (1 November 2026, owner-approved) an action whose intent or
    approval the Trust Ledger cannot take is REFUSED: nothing runs and the
    token is not spent. Every action confirmed here is Class A in
    `trust/journal.py`'s terms. The `execution` record, written after the side
    effect, falls back to the journal and is reported honestly (D4).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Union

from actions.shadow_world import ActionSpec, execute_gate, simulate
from trust.proof_card import build_proof_card
from utils.approval import ApprovalGuardrail, guardrail

logger = logging.getLogger("ira.actions.gated")

ExecuteFn = Callable[[], Union[Any, Awaitable[Any]]]


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _append(kind: str, *, user_id: str, action_id: str,
                  task_id: Optional[str], record: dict) -> int:
    """Append a ledger record and return its seq. Logs a failure, then raises it.

    Two failures, two log levels, on purpose. A `LedgerNotBound` is NOT an
    outage: it means this process had no tenant context to write the record
    under, which is a wiring defect in us and will not clear on its own. Logging
    it as "unavailable" at WARNING is how P9-02's defect survived — the audit
    trail was empty for a whole phase and the only trace was a line that reads
    like a flaky database. See trust/ledger.py:LedgerNotBound."""
    try:
        from trust.ledger import append_record
        row = await append_record(kind, user_id=user_id, action_id=action_id,
                                   task_id=task_id, record=record)
        return int(row["seq"])
    except Exception as exc:
        from trust.ledger import LedgerNotBound, LedgerTenantMismatch
        if isinstance(exc, (LedgerNotBound, LedgerTenantMismatch)):
            logger.error(
                "TRUST LEDGER NOT WRITTEN — this is a defect, not an outage "
                "(%s record for %s): %s", kind, action_id, exc)
        else:
            logger.warning("trust ledger unavailable (%s record for %s): %s",
                           kind, action_id, exc)
        raise


async def _record(kind: str, *, user_id: str, action_id: str,
                  task_id: Optional[str], record: dict) -> Optional[int]:
    """Append a ledger record, fail-soft. Returns the seq or None if the
    ledger was unreachable (DB down) — the caller reports this honestly.

    Only for records whose loss refuses nothing: the `security_event` of a
    refusal, which ran nothing. A record written before a side effect goes
    through `_record_durably`, which a failure stops (P1-09)."""
    try:
        return await _append(kind, user_id=user_id, action_id=action_id,
                             task_id=task_id, record=record)
    except Exception:  # noqa: BLE001 — logged by _append
        return None


@dataclass(frozen=True)
class DurableRecord:
    """Where one record landed: the ledger, the fallback journal, or nowhere."""

    store: Optional[str]                 # "ledger" | "journal" | None
    seq: Optional[int] = None            # the ledger seq, when store == "ledger"
    entry_id: Optional[str] = None       # the journal entry, when store == "journal"
    journal_error: Optional[str] = None  # why the journal refused, when it was tried

    @property
    def durable(self) -> bool:
        return self.store is not None


async def _record_durably(kind: str, *, user_id: str, action_id: str,
                          task_id: Optional[str], record: dict,
                          journal: bool, skip_ledger: bool = False) -> DurableRecord:
    """Write one record to the Trust Ledger, or with `journal=True` to the
    fallback journal when the ledger refuses it (P1-09).

    Never raises. `store=None` means neither took it, and the caller must
    treat that as "not recorded": refuse when the side effect has not run yet,
    report it when it has. A journal entry counts only once it is fsync'd, and
    any journal failure, expected or not, is `store=None`: a journal write that
    failed never becomes permission to execute.

    `skip_ledger` sends the record straight to the journal. It is used once an
    earlier record of the same action went there, so an action's records stay
    together and in order rather than straddling the two stores.
    """
    from trust import journal as _journal

    if not skip_ledger:
        try:
            seq = await _append(kind, user_id=user_id, action_id=action_id,
                                task_id=task_id, record=record)
            return DurableRecord(store="ledger", seq=seq)
        except Exception as exc:  # noqa: BLE001 — logged by _append
            _journal.note_ledger_failure(kind, action_id, exc)
    if not journal:
        return DurableRecord(store=None)
    try:
        entry = await _journal.journal_record(
            kind, user_id=user_id, action_id=action_id, task_id=task_id, record=record,
            reason="earlier_record_journalled" if skip_ledger else "ledger_unavailable")
    except Exception as exc:  # noqa: BLE001 — JournalUnavailable, or anything else
        return DurableRecord(store=None, journal_error=str(exc) or type(exc).__name__)
    return DurableRecord(store="journal", entry_id=entry.entry_id)


def _execution_not_recorded(action_id: str, rec: DurableRecord) -> None:
    """The side effect ran and neither store took its `execution` record."""
    from trust import journal as _journal

    _journal.note_unrecorded_execution(action_id, rec.journal_error or "journal not tried")


#: The status of an action refused because the Trust Ledger could not record it.
LEDGER_REFUSED = "ledger_unavailable"


def is_ledger_refusal(outcome: Any) -> bool:
    return isinstance(outcome, dict) and outcome.get("status") == LEDGER_REFUSED


async def ledger_refusal(action: str, *, action_id: Optional[str], recovery: bool,
                         journal_error: Optional[str] = None) -> dict[str, Any]:
    """The outcome of an action refused for want of a durable record (P1-09).

    Nothing ran and the token was not spent. `break_glass` is non-empty when
    neither store can take a record, so nothing that needs one can run until
    the owner acts outside IRA. Routes answer it with 503
    (`api/routes/_ledger.py`).

    The refusal text for a Class A action comes from the journal's probe (a
    flock wait of up to `LOCK_TIMEOUT_SECONDS`, a file read and `statvfs`),
    which runs in a worker thread (#233): during a ledger outage every refusal
    used to hold the event loop for up to that long.
    """
    from trust import journal as _journal

    # The audit line comes first, before any await: a request cancelled while
    # the probe runs still leaves its trace (#250).
    logger.error("REFUSED %s (%s): the Trust Ledger did not record it; nothing ran",
                 action, action_id)
    if recovery:
        # No probe on this branch: refusal_detail only formats text.
        detail, names_break_glass = _journal.refusal_detail(
            recovery=True, journal_error=journal_error)
    else:
        detail, names_break_glass = await asyncio.to_thread(
            _journal.refusal_detail, recovery=False, journal_error=journal_error)
    if names_break_glass:
        logger.error("REFUSED %s (%s): neither did the journal; the break-glass applies",
                     action, action_id)
    return {"status": LEDGER_REFUSED, "action": action, "action_id": action_id,
            "detail": detail, "executed": False, "confirmation_spent": False,
            "ledger_recorded": False,
            "break_glass": list(_journal.BREAK_GLASS) if names_break_glass else []}


#: What the cloud edition answers when something asks this module to act
#: WITHOUT naming a principal and a governed action.
#:
#: Until P9-07 this was the answer in cloud unconditionally, and the cloud
#: edition therefore had no working action path at all — only a safe refusal.
#: It is now the answer to exactly one thing: a call site that has not been
#: wired to the governed path. See `_governed_or_refuse`.
UNGOVERNED_IN_CLOUD = (
    "This action was requested without a verified principal and a named "
    "governed action, so the tenant's Constitution and permission grants — "
    "which decide whose rules apply — cannot be consulted. The cloud edition "
    "refuses rather than falling back to the owner gate. The governed path is "
    "core/governance: broker.evaluate mints a ticket and gateway.execute "
    "consumes it; core/governance/action_bridge.py is the way in."
)


def _as_principal(principal: Any) -> Any:
    """A real `Principal`, or None — never anything in between.

    ── Why this exists ────────────────────────────────────────────────────

    The three route call sites declare `_principal=Depends(governed_principal)`.
    FastAPI replaces that when it serves a request; a test — and a route calling
    another route — invokes the function directly, and the parameter then holds
    the `fastapi.params.Depends` OBJECT rather than a principal. That object is
    truthy, so `if principal is not None` treats it as authority and the first
    thing to touch it fails deep inside the database layer with a message about
    a missing tenant.

    P9-03 hit the same class of defect from the other side: an alias that looked
    like a dependency and was never resolved, so a header was silently never
    read. The lesson is the same one — a dependency default is not a value.

    So this narrows to the type that IS authority. Anything else becomes None,
    which in cloud means `_refuse_in_cloud` refuses, and in the private edition
    means the owner gate answers, which is that edition's real authority.
    It is logged at ERROR because a route reaching here with an unresolved
    dependency is a wiring defect that will not clear on its own — the same
    reasoning `trust/ledger.py` gives for `LedgerNotBound`.
    """
    if principal is None:
        return None
    from core.identity.principal import Principal

    if isinstance(principal, Principal):
        return principal
    logger.error(
        "UNRESOLVED PRINCIPAL — this is a wiring defect, not a missing user: "
        "governed action received %s instead of a Principal. Treating it as "
        "absent, which refuses in cloud.", type(principal).__name__)
    return None


def _refuse_in_cloud(spec: ActionSpec, *, principal: Any,
                     governed: Optional[tuple[str, str]]) -> Optional[dict[str, Any]]:
    """The cloud refusal for an UNWIRED caller, or None.

    ── What this used to be, and why it changed (P9-07) ────────────────────

    P9-04 put an unconditional cloud refusal here, for a good reason: nothing
    in this module consults `core.governance`, so a tenant's rules constrained
    `/governance/preview` and nothing that executed. Refusing by construction
    was better than relying on five call sites each remembering to pass
    `is_owner=False`.

    But refusing by construction closed the hole by removing the feature. The
    reconciliation of Phase 9 named it exactly: cloud execution was *safe*
    because governed execution did not work, and "governed agents" was reported
    complete on that basis.

    So the refusal narrows to what it should always have been — a fail-closed
    answer for a caller that cannot be governed, because it named no verified
    principal or no governed action. A caller that names both goes through
    `action_bridge`, which is the path this module's own error message has been
    pointing at since P9-04.

    Fail-closed remains the default: `principal=None` refuses. There is no
    inferred principal, no "current user" lookup and no fallback to the owner
    gate, because each of those turns a missing context into somebody's
    authority.
    """
    from core.editions import is_cloud

    if not is_cloud():
        return None
    if principal is not None and governed is not None:
        return None
    logger.warning(
        "ungoverned action path refused in cloud: kind=%s action=%s "
        "principal=%s governed=%s",
        spec.kind, spec.action_id,
        "present" if principal is not None else "MISSING",
        governed or "MISSING")
    return {"status": "forbidden", "action_id": spec.action_id,
            "detail": UNGOVERNED_IN_CLOUD}


async def stage_action(spec: ActionSpec, *, is_owner: bool, execute: ExecuteFn,
                       simulator: Optional[Callable[[ActionSpec], Awaitable[dict]]] = None,
                       guard: Optional[ApprovalGuardrail] = None,
                       principal: Any = None,
                       governed: Optional[tuple[str, str]] = None) -> dict[str, Any]:
    """Simulate and stage an action. Returns one of:
      {"status": "forbidden"}                       — unwired caller, or not the owner
      {"status": "refused", "refused_by": "lockdown", "lockdown_state", "message"}
                                                    — lockdown refuses it (no token)
      {"status": "blocked", "reason", "proof_card"} — critical / sim failed / DENIED
      {"status": "confirmation_required", "token", "approval_level",
       "proof_card"}                                — staged, awaiting confirm

    `simulator`, when given, is used directly (side-effect free by contract)
    instead of the global registry — so callers need not mutate shared state.

    `principal` and `governed` are what make this reachable in the cloud
    edition (P9-07). `governed` is the `(domain, action)` pair the tenant's
    Constitution is asked about; the pair is validated against `spec.kind`, so
    naming a different action than the one being staged is refused rather than
    reconciled.

    **Who decides differs by edition, and that is the design.** In the private
    edition the owner gate is the authority — there is one user and no
    Constitution to consult. In cloud the tenant's Constitution and grants are
    the authority, and `is_owner` is structurally False there (3C-04), so
    consulting it would refuse every action forever. That is precisely what it
    did until this batch.
    """
    g = guard or guardrail
    principal = _as_principal(principal)
    refused = _refuse_in_cloud(spec, principal=principal, governed=governed)
    if refused is not None:
        return refused

    # The owner gate answers before lockdown, so a person who may not act at
    # all hears "not yours" rather than learning the owner's security state.
    # It used to sit after the Constitution check; the two never both apply
    # (the gate needs `principal is None`, the Constitution needs a principal),
    # so moving it changes no outcome.
    if principal is None and not is_owner:
        return {"status": "forbidden", "action_id": spec.action_id,
                "detail": f"'{spec.intended_action}' is restricted to the owner."}

    locked = await _lockdown_refuses(spec)
    if locked is not None:
        return locked

    denied = await _constitution_denies(spec, principal=principal, governed=governed)
    if denied is not None:
        return denied

    if simulator is not None:
        try:
            result = await simulator(spec)
            result.setdefault("ok", False)
        except Exception as exc:
            result = {"ok": False, "detail": f"simulator raised: {exc}"}
        spec.simulation = result
    else:
        await simulate(spec)   # attaches spec.simulation (side-effect-free)
    problems = spec.problems()
    # Anything fatal regardless of approval (critical, failed sim) blocks now.
    fatal = [p for p in problems if "approval level" not in p]
    if fatal:
        return {"status": "blocked", "action_id": spec.action_id,
                "reason": "; ".join(fatal),
                "proof_card": build_proof_card(spec)}

    draft = g.draft(owner=spec.requested_by, action=spec.kind,
                    preview=spec.intended_action, execute=execute)
    return {"status": "confirmation_required", "action_id": spec.action_id,
            "token": draft.token, "approval_level": spec.approval_level,
            "expires_in": draft.expires_in,
            "proof_card": build_proof_card(spec)}


async def _lockdown_refuses(spec: ActionSpec) -> Optional[dict[str, Any]]:
    """The lockdown refusal for this action, or None to let it go on.

    ── Why here ───────────────────────────────────────────────────────────

    Until 31 October 2026 the only caller of `lockdown_refusal` was
    `trust/action_guard.guard_action`, and nothing in the product called
    `guard_action`. The routes that send email, delete notes and create
    calendar events call `stage_action` and `confirm_action` directly, so an
    ACTIVE lockdown refused nothing a person could trigger
    (docs/product/FEATURE_COMPLETION_MATRIX.md, F65). This module is the point
    every one of those routes passes through, so the check lives here.

    It runs in BOTH halves. At stage, so a lockdown-time action never mints a
    token or a Proof Card. At confirm, so a token staged before the lockdown
    does not spend during it. A refusal at confirm consumes nothing and writes
    nothing to the Trust Ledger: the ledger records actions that ran, and
    `lockdown_refusal` logs every refusal at WARNING.

    The policy is unchanged and lives in `trust/lockdown_enforcement.py`:
    ACTIVE refuses every action, the owner's included; UNKNOWN (neither Redis
    nor the database answered) refuses `high` and `critical`.
    """
    from trust.lockdown_enforcement import lockdown_refusal

    # `is_owner` is not passed through: confirm has no owner flag (the token is
    # its authority), and the policy makes no owner exemption, so the value
    # could not change the answer.
    refusal = await lockdown_refusal(
        risk=spec.risk, action_type=spec.kind, is_owner=False)
    if refusal is None:
        return None
    return {**refusal, "action_id": spec.action_id}


async def _constitution_denies(
    spec: ActionSpec, *, principal: Any, governed: Optional[tuple[str, str]],
) -> Optional[dict[str, Any]]:
    """Ask the tenant's Constitution at STAGE time, and refuse before a token.

    The decision is made again at confirm time, by `gateway.execute` consuming
    a freshly minted ticket. This is not redundancy for its own sake: a token
    issued for an action the Constitution denies is a token that should never
    have existed, and a user who is told "confirm to proceed" and then refused
    has been told something false. Refusing here means no draft is stored and
    no side effect is registered anywhere.

    Returns None in the private edition and whenever the caller is unwired —
    `_refuse_in_cloud` has already answered that case.
    """
    if principal is None or governed is None:
        return None

    from core.governance.action_bridge import UngovernableAction, decide

    from core.interfaces.permission import Effect

    domain, action = governed
    try:
        _request, outcome = await decide(
            spec, principal=principal, domain=domain, action=action)
    except UngovernableAction:
        raise

    # `may_execute` is the wrong question HERE, and asking it was a defect this
    # batch caught in itself. At stage time no confirmation exists yet, so every
    # side-effecting action evaluates to `require_confirmation` — that is what
    # the ceiling pins them to — and `may_execute` is False for all of them.
    # Blocking on it would refuse at stage exactly the actions the stage exists
    # to collect a confirmation for, and the product would look governed while
    # being unable to do anything at all.
    #
    # The honest question at stage time is narrower: would this be refused
    # OUTRIGHT? That is DENY, and only DENY.
    if outcome.decision.effect is not Effect.DENY:
        return None

    logger.info(
        "constitution refused %s.%s at stage: %s",
        domain, action, outcome.decision.reason)
    return {"status": "blocked", "action_id": spec.action_id,
            "reason": outcome.decision.reason,
            "governed": False,
            "effect": outcome.decision.effect.value,
            "proof_card": build_proof_card(spec)}


async def confirm_action(spec: ActionSpec, *, token: str,
                         approval_artifact: Optional[str] = None,
                         task_id: Optional[str] = None,
                         simulator: Optional[Callable[[ActionSpec], Awaitable[dict]]] = None,
                         guard: Optional[ApprovalGuardrail] = None,
                         principal: Any = None,
                         governed: Optional[tuple[str, str]] = None,
                         ) -> dict[str, Any]:
    """Gate, execute, and record. `approval_artifact` is what the flow
    collected (e.g. 'owner_password' for high risk); low risk needs none.

    Because stage and confirm are separate requests, the caller's spec may
    arrive without a simulation. The simulator (side-effect free) is re-run
    here so the gate re-validates against current state right before the
    side effect — a spec that was fine at stage but is stale now is caught.

    **The cloud refusal is repeated here, not inherited from `stage_action`.**
    This function has no `is_owner` parameter and never did: the one-shot token
    was the whole of its authority, on the reasoning that a token can only come
    from a stage that already checked. That is true of the flow and is not true
    of the function, and confirm is the half that runs the side effect. A gate
    on the stage alone is one caller away from being no gate."""
    g = guard or guardrail
    user = spec.requested_by
    principal = _as_principal(principal)

    refused = _refuse_in_cloud(spec, principal=principal, governed=governed)
    if refused is not None:
        return refused

    # Checked again, not inherited from the stage: lockdown may have been
    # engaged between the two requests. Before the simulator, the gate, the
    # ledger and the token, so a refusal here runs and records nothing.
    locked = await _lockdown_refuses(spec)
    if locked is not None:
        return locked

    if principal is not None and governed is not None:
        return await _confirm_governed(
            spec, token=token, principal=principal, governed=governed,
            simulator=simulator, guard=g)

    if spec.simulation is None and spec.risk in ("medium", "high"):
        if simulator is not None:
            try:
                result = await simulator(spec)
                result.setdefault("ok", False)
            except Exception as exc:
                result = {"ok": False, "detail": f"simulator raised: {exc}"}
            spec.simulation = result
        else:
            await simulate(spec)

    allowed, reason = execute_gate(spec, approval=approval_artifact)
    if not allowed:
        await _record("security_event", user_id=user, action_id=spec.action_id,
                      task_id=task_id,
                      record={"blocked": reason, "kind": spec.kind})
        return {"status": "blocked", "action_id": spec.action_id, "reason": reason}

    # Intent + approval recorded BEFORE the side effect, and since P1-09 a
    # refusal when the Trust Ledger cannot take them: every action confirmed
    # here is Class A, so the journal is never asked. The token is checked
    # below, after this, so a refusal here leaves it unspent and a retry works.
    intent = await _record_durably(
        "action", user_id=user, action_id=spec.action_id, task_id=task_id,
        record={"kind": spec.kind,
                "intended_action": spec.intended_action,
                "objects_affected": spec.objects_affected,
                "data_leaving_device": spec.data_leaving_device},
        journal=False)
    if not intent.durable:
        return await ledger_refusal(spec.kind, action_id=spec.action_id, recovery=False)
    approval = await _record_durably(
        "approval", user_id=user, action_id=spec.action_id, task_id=task_id,
        record={"approval_level": spec.approval_level,
                "approved_by": approval_artifact or "auto"},
        journal=False)
    if not approval.durable:
        return await ledger_refusal(spec.kind, action_id=spec.action_id, recovery=False)

    # Bound to what was staged: stage_action drafted this token with the spec's
    # kind and intended_action, which the confirming request rebuilds from its
    # own parameters. A token staged for another target or kind runs nothing.
    res = await g.confirm(owner=user, token=token,
                          action=spec.kind, preview=spec.intended_action)
    if not res.executed:
        # token stale/invalid/already used — nothing ran.
        return {"status": res.status, "action_id": spec.action_id,
                "detail": "Confirmation token invalid, already used, or expired."}

    # After the side effect: the ledger, else the journal, else CRITICAL.
    executed = await _record_durably(
        "execution", user_id=user, action_id=spec.action_id, task_id=task_id,
        record={"result": _summarize(res.result)}, journal=True)
    if not executed.durable:
        _execution_not_recorded(spec.action_id, executed)
    ledger_seqs = [s for s in (intent.seq, executed.seq) if s is not None]
    # Build the card from the records just written so it shows who approved and
    # what ran, not just the intent. Fail-soft: an unreachable ledger yields the
    # intent-only card rather than losing the response.
    records: list[dict] = []
    try:
        from trust.ledger import records_for
        records = await records_for(spec.action_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("proof card: ledger records unavailable for %s: %s",
                       spec.action_id, exc)
    return {"status": "executed", "action_id": spec.action_id,
            "result": res.result,
            # True only when all three records are in the ledger. Intent and
            # approval are by now; so it is whether the execution record is.
            "ledger_recorded": executed.store == "ledger",
            "ledger_seqs": ledger_seqs,
            "journal_entries": [executed.entry_id] if executed.entry_id else [],
            "proof_card": build_proof_card(spec, records)}


class _TokenNotHonoured(RuntimeError):
    """The one-shot approval token did not release the side effect."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


async def _confirm_governed(
    spec: ActionSpec, *, token: str, principal: Any,
    governed: tuple[str, str],
    simulator: Optional[Callable[[ActionSpec], Awaitable[dict]]],
    guard: ApprovalGuardrail,
) -> dict[str, Any]:
    """Confirm through the governed path — the segment F9-6 left unbuilt.

        ActionSpec → ActionRequest → Constitution/grants → ticket
                   → gateway.execute → Trust Ledger → side effect

    ── Why the decision is made AGAIN here ─────────────────────────────────

    Stage and confirm are separate requests, and the ticket is minted and
    consumed inside this one. The alternative — mint at stage, store, consume
    at confirm — needs somewhere to keep a live authorisation between two HTTP
    requests, and every such store is a thing that can be replayed, leaked or
    outlived by the rule that authorised it. Re-deciding costs one broker
    evaluation and means the tenant's Constitution is consulted immediately
    before the side effect, which is the same reasoning this module already
    gives for re-running the simulator here.

    ── Why the token is consumed INSIDE the executor ───────────────────────

    `gateway.execute` writes the intent record, then calls the executor, then
    writes the result record — and refuses outright if the intent record cannot
    be made durable (P10-05). Consuming the one-shot token inside that executor
    puts the approval exactly where the side effect is, so the ordering
    guarantee covers both: no durable intent record, no token consumed, nothing
    run.

    A token that is stale, replayed or missing raises, so `gateway.execute`
    reports it as a failed execution with the intent already on record. That is
    the honest shape — an attempt was made and refused — rather than a silent
    "nothing happened".
    """
    domain, action = governed

    # Re-simulate before the side effect, on the same contract as the
    # ungoverned path: a spec that was fine at stage may be stale now.
    if spec.simulation is None and spec.risk in ("medium", "high"):
        if simulator is not None:
            try:
                result = await simulator(spec)
                result.setdefault("ok", False)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "detail": f"simulator raised: {exc}"}
            spec.simulation = result

    # Captured rather than parsed back out of the outcome's reason string.
    # `gateway.execute` reports a failed executor as
    # `f"The action failed: {type(exc).__name__}"`, and matching on that text
    # would make this branch depend on a message another module is free to
    # reword — a guard that reads prose about a failure instead of observing it.
    token_refused: list[str] = []

    async def _run_side_effect() -> Any:
        res = await guard.confirm(owner=spec.requested_by, token=token,
                                  action=spec.kind, preview=spec.intended_action)
        if not res.executed:
            token_refused.append(res.status)
            raise _TokenNotHonoured(res.status)
        return res.result

    from core.governance.action_bridge import execute_spec, outcome_as_response

    outcome = await execute_spec(
        spec, principal=principal, domain=domain, action=action,
        executor=_run_side_effect, confirmation=token)

    if token_refused:
        # The intent record IS on the ledger and nothing ran. That is the true
        # state and the response says so rather than reporting a clean refusal.
        return {"status": token_refused[0] or "expired",
                "action_id": spec.action_id,
                "governed": False,
                "ledger_recorded": outcome.ledger_recorded,
                "detail": "Confirmation token invalid, already used, or expired."}
    return outcome_as_response(outcome, spec)


def _summarize(result: Any) -> dict[str, Any]:
    """Keep the ledger record small, JSON-safe and free of the payload.

    023 documents the execution record as a result *summary*: never credentials,
    never raw connector payloads. An executor's return value can carry a message
    body or a token, and truncating it is not redaction, so only the shape is
    recorded — the rule `core/governance/gateway.py::_summarise` already applies
    on the governed path — plus a short `status` when the result declares one.
    The caller still receives the real result in the response; only the ledger
    is summarised.
    """
    from core.governance.gateway import _summarise

    summary: dict[str, Any] = {"shape": _summarise(result)}
    if isinstance(result, dict):
        status = result.get("status")
        if isinstance(status, (str, bool, int)):
            summary["status"] = str(status)[:40]
    return summary


__all__ = ["stage_action", "confirm_action", "LEDGER_REFUSED", "is_ledger_refusal",
           "ledger_refusal", "DurableRecord"]
