"""
ira/utils/approval.py — reusable approval guardrail for side-effecting actions.

Every real-world side effect (sending email, calendar writes, file writes/deletes,
executor commands) goes through a two-step flow:

  1. draft()   -> produce a human-readable PREVIEW and a one-time pending token,
                  returned to the user. NOTHING is executed yet.
  2. confirm() -> only when an explicit confirmation referencing that token arrives
                  is the action executed.

Pending actions are stored keyed by (owner, token) with a short TTL, so an
unconfirmed or stale draft simply expires and never runs. Tokens are one-shot and
scoped to the owner that created them (another user cannot confirm them).

An action confirmed through `owner_gated_action` is recorded in the Trust Ledger
(action, approval, execution), as `actions/gated_execution.py` records its own.
Since P1-09 (1 November 2026, owner-approved) it does not run unless its intent
and approval are durably recorded. When the ledger cannot take them, an ordinary
action (Class A) is refused with its token left unspent, and the two emergency
recovery actions in `RECOVERY_ACTIONS` (Class B) run only once the records are
fsync'd to the fallback journal. When neither store can take a record, nothing
runs, Class B included, and the refusal names the out-of-band break-glass
(`bash scripts/ira-backup.sh restore <archive> --yes`, `./stop-ira.sh`). The
policy, the journal and the threat model are in `trust/journal.py`.

The store is in-process, which matches the single-process native deployment; swap
the dict for a Redis-backed store if the API is ever scaled to multiple workers.
"""
from __future__ import annotations

import inspect
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Union

logger = logging.getLogger("ira.approval")

# A zero-arg callable performing the side effect; may be sync or async.
ExecuteFn = Callable[[], Union[Any, Awaitable[Any]]]


@dataclass
class Draft:
    token: str
    action: str
    preview: str
    expires_in: float


@dataclass
class ConfirmResult:
    status: str                      # "executed" | "expired" | "not_found"
    action: Optional[str] = None
    result: Any = None

    @property
    def executed(self) -> bool:
        return self.status == "executed"


@dataclass
class _Pending:
    owner: str
    action: str
    preview: str
    execute: ExecuteFn
    expires_at: float


class ApprovalGuardrail:
    """Two-step draft -> confirm gate for side-effecting actions."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 300.0,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._now = now
        self._pending: dict[tuple[str, str], _Pending] = {}

    def _purge_expired(self) -> None:
        t = self._now()
        for key in [k for k, p in self._pending.items() if t >= p.expires_at]:
            del self._pending[key]

    def draft(self, *, owner: str, action: str, preview: str, execute: ExecuteFn) -> Draft:
        """Stage a side effect: store it under a fresh token and return its preview.

        The action is NOT executed here — only when confirm() is called with the
        returned token by the same owner.
        """
        self._purge_expired()
        token = secrets.token_urlsafe(8)
        self._pending[(owner, token)] = _Pending(
            owner=owner, action=action, preview=preview, execute=execute,
            expires_at=self._now() + self.ttl_seconds,
        )
        return Draft(token=token, action=action, preview=preview, expires_in=self.ttl_seconds)

    def _claim(self, *, owner: str, token: str,
               action: Optional[str] = None,
               preview: Optional[str] = None) -> Union[_Pending, ConfirmResult]:
        """Check and consume (owner, token) WITHOUT running the action.

        Returns the pending action when the token is honoured, or the refusal
        as a ConfirmResult. Either way the token is spent, except when it was
        never found. `confirm` is this plus the run; `owner_gated_action` uses
        it directly so that the Trust Ledger's intent record is written after
        the token is known good and before the side effect, the order
        `actions/gated_execution.py` records in.
        """
        key = (owner, token)
        pending = self._pending.get(key)
        if pending is None:
            return ConfirmResult(status="not_found")
        if self._now() >= pending.expires_at:
            del self._pending[key]
            return ConfirmResult(status="expired", action=pending.action)
        if ((action is not None and action != pending.action)
                or (preview is not None and preview != pending.preview)):
            del self._pending[key]  # consumed: never retried against another target
            # Names and verdicts only. A preview says who an email goes to and
            # what a note says; the log is not the place for either.
            logger.warning(
                "approval token refused: issued for %r, presented for %r; preview %s",
                pending.action, action if action is not None else pending.action,
                "differs" if preview is not None and preview != pending.preview else "matches")
            return ConfirmResult(status="not_found", action=pending.action)

        del self._pending[key]  # one-shot: consume before executing
        return pending

    def _release(self, *, owner: str, token: str, pending: _Pending) -> None:
        """Put a token `_claim` consumed back, unspent, with its original expiry.

        Only for a refusal in which nothing ran: the Trust Ledger could not
        record the action (P1-09), so the owner's approval was never used and
        the same confirmation must work on a retry. Holding the token while the
        intent is being recorded, rather than checking it without consuming it,
        means two requests carrying it can never both record an intent: the
        second sees "not found" until this one either runs or releases.
        """
        self._pending.setdefault((owner, token), pending)

    async def confirm(self, *, owner: str, token: str,
                      action: Optional[str] = None,
                      preview: Optional[str] = None) -> ConfirmResult:
        """Execute the staged action for (owner, token), once.

        `action` and `preview` are what the confirming request describes. When
        given, they must be what the token was issued for: a token approves the
        preview the owner was shown, not whatever request carries it. Without
        this, a token drafted to delete note A, presented on the request to
        delete note B, ran A's deletion while the caller recorded B's; and a
        token from one endpoint ran at another. A mismatch consumes the token
        and runs nothing, so one token cannot be tried against several targets.

        Returns status "not_found" (unknown token / wrong owner / already used /
        issued for a different action or preview), "expired" (TTL elapsed), or
        "executed" (with the action's result).
        """
        claimed = self._claim(owner=owner, token=token, action=action, preview=preview)
        if isinstance(claimed, ConfirmResult):
            return claimed
        result = claimed.execute()
        if inspect.isawaitable(result):
            result = await result
        return ConfirmResult(status="executed", action=claimed.action, result=result)

    def pending_count(self, owner: Optional[str] = None) -> int:
        """Number of live (unexpired) pending actions, optionally for one owner."""
        self._purge_expired()
        if owner is None:
            return len(self._pending)
        return sum(1 for (o, _tok) in self._pending if o == owner)


# App-wide singleton for the single-process native deployment.
guardrail = ApprovalGuardrail()


#: Class B (P1-09): the emergency recovery actions. When the Trust Ledger
#: cannot record one, its records go to the fallback journal and it runs once
#: they are durably there; every other action is Class A and is refused.
#:
#: Backup restore is how the owner repairs a broken database, and revoke is how
#: they contain a compromised account: the moments the ledger is likeliest to
#: be down. The set is here, in a protected file, and decided by action name,
#: so a caller cannot declare itself a recovery action. Adding one needs the
#: owner's approval.
RECOVERY_ACTIONS: frozenset[str] = frozenset({"backup_restore", "revoke_access"})


def _unrecordable(action: str, detail: str) -> dict:
    """The refusal when the recorder itself cannot be loaded. Nothing ran."""
    break_glass = ("To act without IRA, use a shell on the computer IRA runs on, in "
                    "the supracloud-jarvis folder: `bash scripts/ira-backup.sh restore "
                    "<archive> --yes` restores a backup, and `./stop-ira.sh` stops IRA "
                    "and with it all access.")
    return {"status": "ledger_unavailable", "action": action, "action_id": None,
            "detail": ("Not done: IRA could not load what records this action, and it "
                       f"does not run an action it cannot record ({detail}). Nothing "
                       "was changed. " + break_glass),
            "executed": False, "confirmation_spent": False, "ledger_recorded": False,
            "break_glass": ["bash scripts/ira-backup.sh restore <archive> --yes",
                            "./stop-ira.sh"]}


async def _run_and_record(pending: _Pending, *, owner: str, token: str,
                          guard: ApprovalGuardrail, intended_action: str) -> dict:
    """Run an honoured owner-gated action and record it in the Trust Ledger.

    ── Why this exists (P2, 1 November 2026) ──────────────────────────────

    `owner_gated_action` wrote nothing to the ledger. A clean install showed it:
    the owner forgot a memory through its confirmation, the API logged two
    DELETEs and no `ledger append`, and the Trust Ledger panel listed nothing.
    Memory forget, backup restore, file delete, people forget, access changes,
    the Command Center and the rest all confirm here, so recording here records
    every one of them, and any route added later.

    ── Same records as `actions/gated_execution.confirm_action` ───────────

    Its helpers are reused rather than copied: `_record` appends through
    `trust.ledger.append_record` (the hash chain, the tenant binding) and
    `_summarize` keeps only the shape of the result, so a returned token or
    body never reaches the ledger. The kinds and their order are the same:
    `action` and `approval` before the side effect, `execution` after it. The
    `approval` record is the one note delete and email send write, because the
    confirmation token is the owner's approval of the plan they were shown.
    The `action` record has no `objects_affected` or `data_leaving_device`:
    nothing here knows them, and an empty `data_leaving_device` would claim the
    action stayed local. `gate` says which chokepoint wrote it.

    ── Fail-closed, with one controlled exception (P1-09) ─────────────────

    This was fail-soft: a failed append did not stop the action, and only a
    `ledger_recorded: false` nobody displayed said so. The owner approved
    replacing that on 1 November 2026:

    * **Class A**, every action not in `RECOVERY_ACTIONS`: when the `action`
      or `approval` record does not land in the Trust Ledger, the action is
      refused. Nothing runs, and the token goes back unspent (`_release`), so
      the same confirmation works once the ledger is back. The journal is
      never consulted: a journal entry is not permission to run Class A.
    * **Class B**, backup restore and access revoke: a record the ledger
      refuses goes to the fallback journal, and the action runs only when the
      journal has it on disk. Once one record of an action is in the journal
      the rest follow it there, so the replay lands them together and in
      order.
    * **Neither store** can take a record: refused, Class B included, and the
      refusal names the break-glass.
    * The **`execution`** record comes after the side effect, so it cannot
      refuse anything: the ledger, else the journal, else a CRITICAL log line
      and the health report's count.

    A side effect that raises still leaves its intent and no execution, and
    spends the token, as before: it ran. `ledger_recorded` is True only when
    all three records are in the ledger; `journal_entries` lists any that went
    to the journal. `_record_durably` logs an outage at WARNING and a missing
    tenant context at ERROR, as a defect. See `trust/journal.py`.

    The import is lazy because `actions.gated_execution` imports this module.
    """
    try:
        from actions.gated_execution import (
            _execution_not_recorded,
            _record_durably,
            _summarize,
            ledger_refusal,
        )
    except Exception as exc:  # noqa: BLE001 — no recorder, no action (P1-09)
        logger.critical("REFUSED %s: the Trust Ledger's recorder could not be "
                        "imported: %s", pending.action, exc)
        guard._release(owner=owner, token=token, pending=pending)
        return _unrecordable(pending.action, type(exc).__name__)

    recovery = pending.action in RECOVERY_ACTIONS
    # Never the token: that is an approval credential, and the ledger is read
    # by every administrator of the tenant.
    action_id = f"{pending.action}-{uuid.uuid4().hex[:12]}"
    seqs: list[int] = []
    journalled: list[str] = []
    to_journal = False
    for kind, record in (
        ("action", {"kind": pending.action, "intended_action": intended_action,
                    "gate": "owner_gated_action"}),
        ("approval", {"approval_level": "plan_approval", "approved_by": "plan_approval"}),
    ):
        rec = await _record_durably(kind, user_id=owner, action_id=action_id,
                                    task_id=None, record=record,
                                    journal=recovery, skip_ledger=to_journal)
        if not rec.durable:
            guard._release(owner=owner, token=token, pending=pending)
            return await ledger_refusal(pending.action, action_id=action_id,
                                        recovery=recovery, journal_error=rec.journal_error)
        if rec.store == "journal" and rec.entry_id is not None:
            to_journal = True
            journalled.append(rec.entry_id)
        elif rec.seq is not None:
            seqs.append(rec.seq)

    result = pending.execute()
    if inspect.isawaitable(result):
        result = await result

    rec = await _record_durably("execution", user_id=owner, action_id=action_id,
                                task_id=None, record={"result": _summarize(result)},
                                journal=True, skip_ledger=to_journal)
    if rec.seq is not None:
        seqs.append(rec.seq)
    elif rec.entry_id is not None:
        journalled.append(rec.entry_id)
    else:
        _execution_not_recorded(action_id, rec)
    return {"status": "executed", "action": pending.action, "result": result,
            "action_id": action_id, "ledger_recorded": len(seqs) == 3,
            "ledger_seqs": seqs, "journal_entries": journalled}


async def owner_gated_action(
    *,
    owner_username: str,
    is_owner: bool,
    action: str,
    preview: str,
    execute: ExecuteFn,
    confirm_token: Optional[str] = None,
    guard: Optional[ApprovalGuardrail] = None,
    ledger_preview: Optional[str] = None,
) -> dict:
    """Combine the owner-gate and the approval guardrail for a high-stakes action.

    Returns a status dict:
      - {"status": "forbidden"}            — caller is not the verified owner
      - {"status": "confirmation_required", "token", "preview", "expires_in"}
                                           — no token supplied: a draft was staged
      - {"status": "executed", "result", "action_id", "ledger_recorded",
         "ledger_seqs", "journal_entries"}
                                           — token valid: the action ran, and its
                                             Trust Ledger records were appended
                                             (or, for Class B or the execution
                                             record, journalled)
      - {"status": "ledger_unavailable", "detail", "break_glass"}
                                           — token valid, but its intent could not
                                             be durably recorded: NOTHING ran and
                                             the token is still usable (P1-09)
      - {"status": "expired" | "not_found"} — token stale/invalid/already used

    The action NEVER executes for a non-owner, and never executes without a valid
    confirmation token. Endpoints map the status to HTTP codes.

    A confirmed action is recorded in the Trust Ledger: `action`, `approval`,
    `execution`, as `actions/gated_execution.py` records (see
    `_run_and_record`). A draft, a refusal and a stale or mismatched token
    record nothing, because nothing ran; `confirm_action` records nothing at
    stage either.

    `ledger_preview` is what the ledger records as the intended action, in
    place of `preview`. Pass one when the preview quotes something the action
    erases or sends: a memory being forgotten, a message body, text typed on a
    phone. The ledger is append-only, so whatever is written there outlives the
    forget; gated_execution's email record likewise names the recipient and
    subject and never the body. The token still binds `preview`.
    """
    g = guard or guardrail
    if not is_owner:
        return {"status": "forbidden", "action": action,
                "detail": f"'{action}' is restricted to the verified owner."}
    if not confirm_token:
        d = g.draft(owner=owner_username, action=action, preview=preview, execute=execute)
        return {"status": "confirmation_required", "action": action,
                "token": d.token, "preview": preview, "expires_in": d.expires_in}
    # The token approves this action and this preview, as drafted; the same
    # request parameters build the same preview, a different target does not.
    claimed = g._claim(owner=owner_username, token=confirm_token,
                       action=action, preview=preview)
    if isinstance(claimed, ConfirmResult):
        return {"status": claimed.status, "action": action,
                "detail": "Confirmation token invalid, already used, or expired."}
    return await _run_and_record(
        claimed, owner=owner_username, token=confirm_token, guard=g,
        intended_action=preview if ledger_preview is None else ledger_preview)


__all__ = [
    "ApprovalGuardrail", "Draft", "ConfirmResult", "guardrail", "owner_gated_action",
    "RECOVERY_ACTIONS",
]
