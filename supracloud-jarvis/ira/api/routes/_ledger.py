"""How an action route answers when the Trust Ledger refuses an action: 503 (P1-09).

Not a router. The policy is in `trust/journal.py`, `utils/approval.py` and
`actions/gated_execution.py`; this module only turns its refusal into one HTTP
answer, the same on every route, as `_lockdown.py` does for lockdown.

── Why 503 ─────────────────────────────────────────────────────────────────

`_lockdown.py` sets out what each answer means here. 503 is "a dependency is
down, try again later", which is this exactly: the Trust Ledger could not
record the action, nothing ran, and the confirmation is still valid, so the
same request succeeds once the database is reachable. 409 would say the token
is bad, and it is not; 423 would say it stays locked until someone unlocks it.

Without this a refusal would go back as a 200 carrying
`"status": "ledger_unavailable"`: nothing would have run, and a client reading
the code would be told the request succeeded. The body's `detail` is the
sentence for the owner; when neither the ledger nor the fallback journal can
take a record it also names the break-glass, and the `X-IRA-Trust-Ledger`
header says `journal-unavailable` rather than `unavailable`.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from actions.gated_execution import is_ledger_refusal

#: The status every Trust Ledger refusal is answered with.
LEDGER_REFUSAL_STATUS_CODE = 503
#: Response header naming which store failed behind a refusal.
LEDGER_HEADER = "X-IRA-Trust-Ledger"


def ledger_http_error(outcome: dict[str, Any]) -> HTTPException:
    """The HTTP answer for one Trust Ledger refusal."""
    return HTTPException(
        status_code=LEDGER_REFUSAL_STATUS_CODE,
        detail=outcome.get("detail") or "Not done: the Trust Ledger could not record this.",
        headers={LEDGER_HEADER: "journal-unavailable" if outcome.get("break_glass")
                 else "unavailable"},
    )


def raise_if_ledger_refused(outcome: Any) -> None:
    """Raise 503 when an `owner_gated_action` or `confirm_action` outcome is a
    Trust Ledger refusal; return otherwise."""
    if is_ledger_refusal(outcome):
        raise ledger_http_error(outcome)


__all__ = [
    "LEDGER_REFUSAL_STATUS_CODE",
    "LEDGER_HEADER",
    "ledger_http_error",
    "raise_if_ledger_refused",
]
