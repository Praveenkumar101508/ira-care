"""Retention: what is kept for how long, enforced.

Each person's `care_consents` may carry a `data_retention` record with a
number of months; without one the default is 12 (care/consent.py). The purge
removes, per person, everything older than that:

    readings, check-ins, transcripts, medication events, reports,
    and alerts that are resolved or cancelled (with their attempts and
    acknowledgements, by cascade).

It never removes an alert that is still open, acknowledged or escalating, and
never touches the access log: that is audit evidence with its own retention,
purged only by the schema owner (migration 099).

The purge runs daily as the `care_retention` worker job, one workspace at a
time under row-level security, and writes one access-log row per person per
run saying how many rows went, so a deletion is itself on the record.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Optional

from core.identity.principal import Principal

from . import audit, consent

logger = logging.getLogger("ira.care.retention")

JOB = "care_retention"

#: The tables purged by age, with the column that dates a row.
PURGED: tuple[tuple[str, str], ...] = (
    ("care_vitals", "recorded_at"),
    ("care_transcripts", "created_at"),
    ("care_medication_events", "due_at"),
    ("care_checkins", "scheduled_at"),
    ("care_reports", "generated_at"),
)


@dataclass
class RetentionOutcome:
    people: int = 0
    deleted: dict[str, int] = field(default_factory=dict)
    errors: int = 0

    def add(self, table: str, n: int) -> None:
        self.deleted[table] = self.deleted.get(table, 0) + n

    @property
    def total(self) -> int:
        return sum(self.deleted.values())


def cutoff_for(records, now: dt.datetime) -> dt.datetime:
    """The moment before which a person's rows are older than they may be kept."""
    months = consent.retention_months(records)
    return now - dt.timedelta(days=int(round(months * 30.4375)))


def _count(tag: str) -> int:
    try:
        return int(tag.split()[-1])
    except (ValueError, IndexError):
        return 0


async def purge_person(conn, principal: Principal, person: dict, now: dt.datetime,
                       outcome: Optional[RetentionOutcome] = None) -> RetentionOutcome:
    outcome = outcome or RetentionOutcome()
    records = [dict(r) for r in await conn.fetch(
        "SELECT kind, granted, recorded_at, revoked_at, retention_months FROM care_consents WHERE person_id = $1",
        person["id"])]
    cutoff = cutoff_for(records, now)
    deleted: dict[str, int] = {}
    for table, column in PURGED:
        tag = await conn.execute(f"DELETE FROM {table} WHERE person_id = $1 AND {column} < $2", person["id"], cutoff)  # noqa: S608
        deleted[table] = _count(tag)
    tag = await conn.execute(
        "DELETE FROM care_alerts WHERE person_id = $1 AND opened_at < $2 AND status IN ('resolved', 'cancelled')",
        person["id"], cutoff)
    deleted["care_alerts"] = _count(tag)
    total = sum(deleted.values())
    for table, n in deleted.items():
        outcome.add(table, n)
    outcome.people += 1
    if total:
        await audit.record_access(conn, principal, person_id=person["id"], resource_kind="person", action="export",
                                  detail={"event": "retention purge", "cutoff": cutoff.isoformat(),
                                          "retention_months": consent.retention_months(records), "deleted": deleted})
    return outcome


async def run_workspace(principal: Principal, now: Optional[dt.datetime] = None) -> RetentionOutcome:
    from core.identity.db_context import workspace_connection
    now = now or dt.datetime.now(dt.timezone.utc)
    outcome = RetentionOutcome()
    async with workspace_connection(principal) as conn:
        people = [dict(p) for p in await conn.fetch("SELECT id, display_name FROM care_people")]
        for person in people:
            try:
                await purge_person(conn, principal, person, now, outcome)
            except Exception:  # noqa: BLE001
                logger.exception("care retention: purge failed for a person")
                outcome.errors += 1
    return outcome


async def run_once(now: Optional[dt.datetime] = None) -> RetentionOutcome:
    from core.identity.db_context import scheduler_connection
    from utils.db import get_pool

    from .checkin_cycle import job_principal

    total = RetentionOutcome()
    async with get_pool().acquire() as conn:
        tenant_ids = [r["id"] for r in await conn.fetch("SELECT id FROM tenants WHERE status = 'active' ORDER BY id")]
    for tenant_id in tenant_ids:
        try:
            async with scheduler_connection(tenant_id) as conn:
                workspaces = [r["id"] for r in await conn.fetch(
                    "SELECT id FROM workspaces WHERE tenant_id = $1 AND status = 'active'", tenant_id)]
            for ws in workspaces:
                out = await run_workspace(job_principal(tenant_id, ws), now)
                total.people += out.people; total.errors += out.errors
                for t, n in out.deleted.items():
                    total.add(t, n)
        except Exception:  # noqa: BLE001
            logger.exception("care retention: tenant pass failed")
            total.errors += 1
    return total
