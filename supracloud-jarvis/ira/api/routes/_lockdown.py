"""How an action route answers while lockdown refuses it: 423 Locked.

Not a router. The policy is `trust/lockdown_enforcement.lockdown_refusal`; this
module only turns its refusal into one HTTP answer, the same on every route.

── Why 423 ─────────────────────────────────────────────────────────────────

The other answers an action route gives already mean something else here:

* 403 — this person may not do this (not the owner, or the Constitution says
  no). Lockdown refuses the owner too, so 403 would send them looking for a
  permission they already have.
* 409 — this request conflicts with something (a failed simulation, a stale or
  reused confirmation token). Retrying with a fresh token would not help.
* 503 — a dependency is down, try again later. An ACTIVE lockdown is not an
  outage and retrying will not get through it.

423 Locked says the resource is locked and stays locked until someone unlocks
it, and IRA already answers 423 for a locked Armory vault. The body's `detail`
is the sentence a person can read; the `X-IRA-Lockdown` header carries the
state (`active` or `unknown`) for a client that needs to tell them apart.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from trust.lockdown_enforcement import is_lockdown_refusal, lockdown_refusal

#: The status every lockdown refusal is answered with.
LOCKDOWN_STATUS_CODE = 423
#: Response header naming the lockdown state behind a refusal.
LOCKDOWN_HEADER = "X-IRA-Lockdown"


def lockdown_http_error(refusal: dict[str, Any]) -> HTTPException:
    """The HTTP answer for one lockdown refusal."""
    return HTTPException(
        status_code=LOCKDOWN_STATUS_CODE,
        detail=refusal.get("message") or refusal.get("reason") or "Refused by lockdown.",
        headers={LOCKDOWN_HEADER: str(refusal.get("lockdown_state", "unknown"))},
    )


def raise_if_lockdown_refused(outcome: Any) -> None:
    """Raise 423 when a `gated_execution` outcome is a lockdown refusal.

    The gated routes map each status they know to HTTP and return anything
    else as it is, so without this a refusal would go back as a 200 carrying
    `"status": "refused"`: nothing would run, and the client would be told the
    request succeeded.
    """
    if is_lockdown_refusal(outcome):
        raise lockdown_http_error(outcome)


async def refuse_during_lockdown(*, risk: str, action_type: str, is_owner: bool) -> None:
    """Raise 423 when lockdown refuses this action now; return when it may go on.

    For routes whose side effect does not pass through `gated_execution`. Call
    it before drafting, and on the same request path that confirms, so a token
    drafted before the lockdown cannot spend during it.

    `is_owner` is whether the caller is authorised to take this action at all.
    When it is False this returns without reading the lockdown state, because
    every caller of this helper refuses that person next (with 403, in
    `owner_gated_action` or the route's own owner check), and answering 423
    first would tell someone who may not act whether the owner has engaged a
    lockdown. A caller with no such refusal after it must pass True.
    """
    if not is_owner:
        return
    refusal = await lockdown_refusal(risk=risk, action_type=action_type, is_owner=is_owner)
    if refusal is not None:
        raise lockdown_http_error(refusal)


__all__ = [
    "LOCKDOWN_STATUS_CODE",
    "LOCKDOWN_HEADER",
    "lockdown_http_error",
    "raise_if_lockdown_refused",
    "refuse_during_lockdown",
]
