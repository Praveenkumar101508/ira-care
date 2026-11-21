"""P14-R1 — FINDING AB: lockdown becomes a control instead of an alarm.

── The finding, carried since Phase 14 ────────────────────────────────────

`initiate_lockdown()` persists `LOCKDOWN=1`, writes a CRITICAL row to
`security_events`, and sends the owner a Telegram alert. `lift_lockdown()`
reverses it. `get_lockdown_state()` reports one of three states.

**Between those, no code path consulted the state to restrict anything.** The
product presents lockdown as an emergency measure; it was an alarm bell wired
to nothing. `p15_acceptance.py` has asserted that emptiness every run since,
so that closing it would fail the documents still calling it open.

This module is the enforcement point that was missing. P14-R1 called it from
`trust/action_guard.guard_action` on the belief that every privileged
operation passes through that guard. None did: see "Where it is called" below.

── The policy, and why UNKNOWN is not simply "closed" ─────────────────────

The owner's Phase 14 instruction is explicit:

    Do not convert "could not determine lockdown state" into ordinary `False`.
    Represent uncertainty explicitly. Security-sensitive operations should not
    silently interpret UNKNOWN as "not locked down."

`ACTIVE` is easy: refuse every governed action. That is what lockdown means.

`UNKNOWN` is the interesting one, and it arises when **both** Redis and the
database are unreachable. Two tempting answers are both wrong:

* **Fail open** — treat UNKNOWN as "not locked down". That is precisely the
  silent interpretation the instruction forbids, and it means an attacker who
  can break the state store can also lift the lockdown.
* **Fail closed on everything** — refuse every action. That converts a
  datastore outage into a total product outage, and a control that takes the
  product down when its own dependencies wobble gets disabled.

So the refusal is **tiered by the risk the pipeline has already computed**.
UNKNOWN refuses `high` and `critical` — the security-sensitive operations the
instruction names — and permits `low` and `medium`, *with the uncertainty
recorded on the outcome*. Nothing is silent either way: every governed action's
result now carries `lockdown_state`, whether it was refused or allowed.

That is a judgement, and it is written here rather than buried in a branch, so
it can be disagreed with in a diff.

── What this deliberately does NOT do ─────────────────────────────────────

**It does not write to the Trust Ledger.** That looks like an omission on a
security control, so: `gated_execution` already refuses actions two other ways
— `_refuse_in_cloud` for an ungoverned caller and the owner check for a
non-owner — and **neither ledgers either**. The ledger records the intent and
result of actions that RAN; refusals are logged. A third refusal that ledgered
would make the chain mean something different depending on which branch
produced the entry.

It is not silent. Every refusal logs at WARNING with the state, the risk and
the action type, and `initiate_lockdown()` has already written a CRITICAL
`security_events` row and alerted the owner — the alarm was never the missing
half.

── Where it is called (31 October 2026) ───────────────────────────────────

Until then the only caller was `guard_action`, and `guard_action` had no
product caller: the action routes call `actions/gated_execution.stage_action`
and `confirm_action` directly, so an ACTIVE lockdown refused nothing a person
could trigger. The checks now sit at the points where IRA acts:

* `actions/gated_execution.stage_action` and `confirm_action` — email, notes,
  CalDAV events and assist scheduling. Confirm checks again, so a token staged
  before the lockdown does not spend during it.
* `api/routes/_lockdown.refuse_during_lockdown` — the routes that draft and
  confirm through `utils.approval.owner_gated_action` (Command Center, Cal.com,
  files, memory, people, Android, access grants and role changes, mobile
  tasks) and the architect's apply.
* the chat-side tools that act on the computer: the executor's commands, the
  digital agent's app launch and commands, the Armory gatekeeper, the coding
  agent and the Telegram dispatch.

Lifting the lockdown is not an action in this sense and passes through none of
them, so a lockdown can always be lifted. Revoking a delegated account is
containment, not action, and is not refused either.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("ira.trust.lockdown")

#: Risk levels refused while the lockdown state cannot be determined. Ordered
#: by `actions.shadow_world.RISK_LEVELS`; these are the two the owner's
#: instruction calls "security-sensitive".
REFUSED_WHEN_UNKNOWN = frozenset({"high", "critical"})


async def lockdown_refusal(
    *, risk: str, action_type: str, is_owner: bool,
) -> Optional[dict[str, Any]]:
    """A refusal outcome for a governed action, or None to let it proceed.

    Never raises. A failure to evaluate the policy must not become a failure to
    run the product, and it must not become a silent permit either — the
    exception path returns a refusal for the same risk levels UNKNOWN refuses,
    because "the check itself broke" is at least as uncertain as "the store was
    unreachable".
    """
    try:
        from utils.security_tools import LockdownState, get_lockdown_state

        state = await get_lockdown_state()
    except Exception as exc:  # noqa: BLE001 — see the docstring
        logger.error(
            "lockdown state could not be evaluated (%s: %s). Treating this as "
            "UNKNOWN: %s actions are refused, the rest proceed with the "
            "uncertainty recorded.",
            type(exc).__name__, exc, sorted(REFUSED_WHEN_UNKNOWN),
        )
        if risk in REFUSED_WHEN_UNKNOWN:
            return _refusal("unknown", risk, action_type,
                            f"the lockdown state could not be evaluated "
                            f"({type(exc).__name__})")
        return None

    if state is LockdownState.ACTIVE:
        # No owner exemption. An owner who wants to act lifts the lockdown
        # first: one deliberate change of state that every worker sees, as
        # opposed to a per-action bypass. (Lifting does not yet write a
        # security_events or Trust Ledger row; that is an open owner decision,
        # recorded in docs/product/FEATURE_COMPLETION_MATRIX.md under F65.)
        return _refusal("active", risk, action_type,
                        "lockdown is ACTIVE; lift it before acting")

    if state is LockdownState.UNKNOWN and risk in REFUSED_WHEN_UNKNOWN:
        return _refusal("unknown", risk, action_type,
                        "the lockdown state is UNKNOWN — both the cache and "
                        "the database were unreachable — and UNKNOWN is not "
                        "'not locked down'")

    return None


def _refusal(state: str, risk: str, action_type: str, why: str) -> dict[str, Any]:
    logger.warning("refusing %s action %r: %s", risk, action_type, why)
    return {
        "status": "refused",
        "refused_by": "lockdown",
        "lockdown_state": state,
        "risk": risk,
        "action_type": action_type,
        "reason": why,
        "message": _MESSAGES[state],
        # Named so a caller cannot mistake this for a transient error and retry
        # its way through.
        "retryable": False,
    }


#: What a person is told, by state. `reason` above is for the log and the
#: response body; this is the sentence a screen or a voice reply can use as it is.
_MESSAGES = {
    "active": ("Lockdown is on, so IRA is not taking any actions, including yours. "
               "Lift the lockdown first (say \"lift lockdown\"), then try again."),
    "unknown": ("IRA cannot tell whether lockdown is on: neither Redis nor the "
                "database answered. Until it can, it refuses high-risk actions."),
}


def is_lockdown_refusal(outcome: Any) -> bool:
    """True when `outcome` is a refusal made by this module."""
    return isinstance(outcome, dict) and outcome.get("refused_by") == "lockdown"


__all__ = ["lockdown_refusal", "is_lockdown_refusal", "REFUSED_WHEN_UNKNOWN"]
