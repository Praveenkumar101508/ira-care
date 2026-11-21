"""
IRA Business Monitor — Real-time lead and booking alerting.

Checks every 5 minutes:
  - New leads since last check → alert with qualification suggestion
  - New bookings → confirm and add to calendar
  - Hot leads (high-value, urgent) → immediate CRITICAL notification
  - Stale leads (no follow-up in 48h) → nudge Sir to act

IRA proactively says: "Sir, you have 3 new leads. Shall I qualify them?"
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum

from config import get_settings
from core.identity.db_context import owner_connection
from utils.db import acquire
from utils.llm import chat_complete
from worker.notifier import notify

logger = logging.getLogger("ira.business")

# ── Lead qualification prompt ──────────────────────────────────────────────────
def _qualify_prompt() -> str:
    cfg = get_settings()
    return (
        f"You are IRA, a professional business assistant for {cfg.owner_name}. "
        "Briefly qualify these leads. For each lead, give: likely budget tier (Low/Mid/High), "
        "urgency (cold/warm/hot), and recommended action. "
        "Keep it under 100 words total. Speak warmly and professionally."
    )


_QUALIFY_PROMPT = _qualify_prompt()  # Keep for backwards compat


async def _get_new_leads_since(last_check: datetime) -> list[dict]:
    # Batch 4 gave this table a tenant and forced row-level security, so a bare
    # `acquire()` binds no `app.tenant_id`, every predicate evaluates against
    # NULL, and the query matches zero rows *without failing*. `owner_connection`
    # binds the single private tenant and REFUSES in the cloud edition — which is
    # the honest state of this subsystem: it has no Principal to thread and is
    # Private-edition only (CLOUD-ADR-0001).
    async with owner_connection(reason="worker/business_monitor.py") as conn:
        rows = await conn.fetch(
            """SELECT id, title, status, payload, created_at
               FROM business_events
               WHERE event_type='lead' AND created_at > $1
               ORDER BY created_at DESC""",
            last_check,
        )
    return [
        {
            "id": str(r["id"]),
            "title": r["title"],
            "status": r["status"],
            "details": r["payload"],
            "time": r["created_at"].isoformat(),
        }
        for r in rows
    ]


async def _get_stale_leads(hours: int = 48) -> int:
    # Batch 4 gave this table a tenant and forced row-level security, so a bare
    # `acquire()` binds no `app.tenant_id`, every predicate evaluates against
    # NULL, and the query matches zero rows *without failing*. `owner_connection`
    # binds the single private tenant and REFUSES in the cloud edition — which is
    # the honest state of this subsystem: it has no Principal to thread and is
    # Private-edition only (CLOUD-ADR-0001).
    async with owner_connection(reason="worker/business_monitor.py") as conn:
        return await conn.fetchval(
            """SELECT COUNT(*) FROM business_events
               WHERE event_type='lead' AND status='new'
               AND created_at < NOW() - make_interval(hours => $1)""",
            hours,
        ) or 0


#: Where a corrupt `last_business_check` is preserved instead of overwritten,
#: and where the previous known-good checkpoint is kept so a corrupt one has
#: somewhere trustworthy to resume from.
_CHECKPOINT_KEY = "last_business_check"
_PREVIOUS_KEY = "last_business_check_prev"
_QUARANTINE_KEY = "last_business_check_corrupt"


class CheckpointState(str, Enum):
    """How much the lower bound below can be trusted.

    P14-01b, finding T. The value `_get_last_check_time` returns is the lower
    bound of `_get_new_leads_since`, so "where did this bound come from" decides
    whether a quiet scan means *no new leads* or *leads that will never be seen*.
    A bare `datetime` cannot carry that, which is why the three cases used to be
    indistinguishable from outside the function.
    """

    #: A stored checkpoint parsed. The window is exactly what it should be.
    OK = "ok"
    #: No checkpoint row at all — genuinely the first run.
    COLD_START = "cold_start"
    #: A row exists and will not parse. The bound below is a RECOVERY point,
    #: not a checkpoint, and it is deliberately early rather than late.
    CORRUPT = "corrupt"


@dataclass(frozen=True)
class Checkpoint:
    since: datetime
    state: CheckpointState
    #: The stored string, when there was one. Preserved as evidence.
    raw: str | None = None
    #: Which source `since` came from, when the checkpoint was CORRUPT.
    resumed_from: str | None = None

    @property
    def trustworthy(self) -> bool:
        return self.state is CheckpointState.OK


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except Exception:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _get_last_check_time() -> Checkpoint:
    """The lower bound of the lead scan, **and how much it can be trusted.**

    P13-01 finding T found the defect: a stored value that would not parse fell
    to "five minutes ago" under a bare `pass`, so every lead created between the
    TRUE last check and that moment was never alerted on — and `_update_last_check`
    then overwrote the unparseable value with `now`, so the window was never
    revisited AND the evidence of the corruption was destroyed on the same tick.
    P13-01 made that audible and left the direction to the owner, because
    widening the window instead re-alerts on leads the owner has already seen.

    The owner's Phase 14 instruction chose:

        Do not silently overwrite the evidence and narrow the monitoring window.
        Preserve the corrupt checkpoint, resume from the last trustworthy state;
        duplicate alerts are preferable to silently losing the window.

    So a corrupt checkpoint no longer produces a *narrower* window than the truth.
    It resumes from the most trustworthy earlier marker available, in this order:

    1. `last_business_check_prev` — the checkpoint before this one, written by
       `_update_last_check` only from a value that had already parsed. This is a
       real checkpoint, so resuming from it re-scans one cycle: duplicates, which
       is the side the owner chose.
    2. `monitor_state.updated_at` — when the corrupt row was last written. Not a
       checkpoint, but a server-side fact about when scanning last reached here.
    3. Five minutes ago — the old behaviour, now the LAST resort and logged as a
       genuinely lost window rather than as an ordinary default.
    """
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value, updated_at FROM monitor_state WHERE key=$1",
            _CHECKPOINT_KEY,
        )
        previous = await conn.fetchrow(
            "SELECT value FROM monitor_state WHERE key=$1", _PREVIOUS_KEY)

    fallback = datetime.now(timezone.utc) - timedelta(minutes=5)
    if not row:
        # A cold start is not a failure, and is now stated as itself rather than
        # sharing a return value with the corrupt case.
        return Checkpoint(since=fallback, state=CheckpointState.COLD_START)

    parsed = _parse(row["value"])
    if parsed is not None:
        return Checkpoint(since=parsed, state=CheckpointState.OK,
                          raw=row["value"])

    prior = _parse(previous["value"]) if previous else None
    updated_at = row["updated_at"] if isinstance(row["updated_at"], datetime) else None
    if updated_at is not None and updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)

    if prior is not None:
        since, source = prior, _PREVIOUS_KEY
    elif updated_at is not None:
        since, source = updated_at, "monitor_state.updated_at"
    else:
        since, source = fallback, "five-minute fallback"

    logger.error(
        "%s is stored but UNPARSEABLE (%r). Resuming from %s (%s) instead of "
        "narrowing the window to %s; the corrupt value is preserved under %r "
        "and is NOT being overwritten silently.",
        _CHECKPOINT_KEY, row["value"], since.isoformat(), source,
        fallback.isoformat(), _QUARANTINE_KEY)
    if source == "five-minute fallback":
        logger.error(
            "no trustworthy earlier checkpoint exists (%s absent or also "
            "unparseable, and the row carries no usable updated_at), so any "
            "lead created before %s and after the real last check will NEVER "
            "be alerted on. This window is lost, not deferred.",
            _PREVIOUS_KEY, since.isoformat())

    return Checkpoint(since=since, state=CheckpointState.CORRUPT,
                      raw=row["value"] if isinstance(row["value"], str) else repr(row["value"]),
                      resumed_from=source)


async def _quarantine_corrupt_checkpoint(raw: str) -> None:
    """Copy the corrupt value somewhere it survives the repair.

    The point of finding T is that the corruption used to be destroyed on the
    same tick that hid its consequences, so nobody could ever tell it had
    happened. The repair below still writes a fresh checkpoint — refusing to
    would leave the monitor re-scanning the same window on every tick forever,
    which is a stuck monitor, not a preserved one — but the evidence outlives it.

    Best effort by design: failing to preserve evidence must not stop the scan
    that alerts the owner about actual leads. It is logged loudly instead.
    """
    stamp = datetime.now(timezone.utc).isoformat()
    try:
        async with acquire() as conn:
            await conn.execute(
                """INSERT INTO monitor_state (key, value) VALUES ($1, $2)
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,
                                                   updated_at=NOW()""",
                _QUARANTINE_KEY, json.dumps({"observed_at": stamp, "value": raw}),
            )
    except Exception as exc:
        logger.error(
            "could not preserve the corrupt %s value %r under %r (%s: %s) — the "
            "scan continues, but this corruption may leave no durable trace",
            _CHECKPOINT_KEY, raw, _QUARANTINE_KEY, type(exc).__name__, exc)


async def _update_last_check(previous: str | None = None) -> None:
    """Advance the checkpoint, keeping the one it replaces as a fallback.

    `previous` is passed in rather than read back here, and only ever from a
    value that already parsed — promoting a corrupt value to the "last
    trustworthy state" would defeat the whole recovery path above.
    """
    now = datetime.now(timezone.utc).isoformat()
    async with acquire() as conn:
        if previous is not None:
            await conn.execute(
                """INSERT INTO monitor_state (key, value) VALUES ($1, $2)
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,
                                                   updated_at=NOW()""",
                _PREVIOUS_KEY, previous,
            )
        await conn.execute(
            """INSERT INTO monitor_state (key, value) VALUES ($1, $2)
               ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,
                                               updated_at=NOW()""",
            _CHECKPOINT_KEY, now,
        )


async def run_business_scan() -> None:
    """Check for new leads and bookings. Called every 5 minutes by scheduler."""
    logger.debug("Running business scan...")

    checkpoint = await _get_last_check_time()
    if checkpoint.state is CheckpointState.CORRUPT and checkpoint.raw is not None:
        # Before anything can overwrite it. Finding T's defect was ordering as
        # much as it was semantics.
        await _quarantine_corrupt_checkpoint(checkpoint.raw)
    new_leads = await _get_new_leads_since(checkpoint.since)
    await _update_last_check(previous=checkpoint.raw if checkpoint.trustworthy else None)

    if not new_leads:
        return

    count = len(new_leads)
    logger.info(f"Business scan: {count} new lead(s) found")

    # Quick LLM qualification
    lead_text = "\n".join(
        f"- {l['title']} (status: {l['status']})"
        for l in new_leads[:5]
    )
    messages = [
        {"role": "system", "content": _QUALIFY_PROMPT},
        {"role": "user", "content": f"Leads to qualify:\n{lead_text}"},
    ]
    qualification = await chat_complete(messages, use_deep=False, temperature=0.4, max_tokens=150)

    # Use the LLM qualification text for hot detection — it already assessed urgency.
    # Checking raw payload dicts is unreliable (JSONB stringification varies).
    qual_lower = qualification.lower()
    hot_leads = [l for l in new_leads if "hot" in qual_lower or "urgent" in qual_lower]

    priority = "critical" if hot_leads else "warning" if count >= 3 else "info"

    owner = get_settings().owner_name
    body = (
        f"{owner}, IRA has detected {count} new lead{'s' if count > 1 else ''}.\n\n"
        f"**Quick Assessment:**\n{qualification}\n\n"
        f"Shall I qualify them further, draft follow-up emails, or schedule calls?"
    )

    await notify(
        f"{count} New Lead{'s' if count > 1 else ''} Require{'s' if count == 1 else ''} Attention",
        body,
        category="business",
        priority=priority,
        metadata={"lead_ids": [l["id"] for l in new_leads]},
    )

    # Stale lead check
    stale = await _get_stale_leads(hours=48)
    if stale > 0:
        await notify(
            f"{stale} Lead{'s' if stale > 1 else ''} Need Follow-Up",
            f"{owner}, {stale} lead{'s have' if stale > 1 else ' has'} not been actioned in over 48 hours. "
            f"Shall I draft follow-up messages or mark them for review?",
            category="business",
            priority="warning",
        )
