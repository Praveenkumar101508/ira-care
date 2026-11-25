"""Reminders answered with the watch's buttons (D1.h; care/personal.py, care/checkin_cycle.py).

Real HTTP stack, real PostgreSQL as `ira_app`. The watch shows "Done", "Later" and
"Not today" for a reminder and sends the button as `choice` on the check-in answer.
Covers: each choice's outcome; a dose still taking two "Done"s before it is written
down (H14); a choice refused on a check-in that is not a reminder; a repeat of the
same idempotency key changing nothing; and the due check-in telling the watch it is
a reminder.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")
pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy._care_fixtures import add_wearer, client, families, pool  # noqa: E402,F401
from tests.tenancy.test_care_personal_db import _due, _setup  # noqa: E402

from care import checkin_cycle as cycle  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

C = "/api/v1/care"
UTC = dt.timezone.utc


async def _remind(client, fam, p, *, kind="reminder", text="ring Sravani", medication=False):
    client.act_as(fam, "member", "member")
    body = {"text": text, "kind": kind, "due_at": (dt.datetime.now(UTC) - dt.timedelta(seconds=1)).isoformat()}
    if medication:
        body["medication_id"] = p["medication"]["id"]
    r = await client.post(f"{C}/people/{p['id']}/reminders", json=body)
    assert r.status_code == 201, r.text
    async with workspace_connection(p["jp"]) as conn:
        await conn.execute("UPDATE care_checkins SET status = 'cancelled' WHERE person_id = $1 AND status = 'scheduled'",
                           uuid.UUID(p["id"]))
    await cycle.run_workspace(p["jp"], dt.datetime.now(UTC))
    due = await _due(client, p)
    assert due is not None and due["reminder_id"] == r.json()["id"], "the watch is told the question is a reminder"
    return r.json(), due


async def _press(client, p, checkin_id, choice, key=None):
    headers = dict(p["h"])
    if key:
        headers["X-Idempotency-Key"] = key
    return await client.post(f"{C}/devices/me/checkins/{checkin_id}/answer",
                             json={"text": "", "via": "button", "choice": choice}, headers=headers)


async def _events(client, p):
    return (await client.get(f"{C}/people/{p['id']}/medication-events")).json()["events"]


async def test_done_ticks_off_a_reminder_and_later_snoozes_it(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    reminder, due = await _remind(client, fam, p)
    r = await _press(client, p, due["id"], "later")
    assert r.status_code == 200, r.text
    assert r.json()["reminder"]["status"] == "snoozed"
    assert "Pressed Later on the watch" in r.json()["answer_summary"]
    async with workspace_connection(p["jp"]) as conn:
        await conn.execute("UPDATE care_reminders SET snoozed_until = $1 WHERE id = $2",
                           dt.datetime.now(UTC) - dt.timedelta(seconds=1), uuid.UUID(reminder["id"]))
    await cycle.run_workspace(p["jp"], dt.datetime.now(UTC))
    again = await _due(client, p)
    assert again and again["reminder_id"] == reminder["id"], "a snoozed reminder is asked again"
    r = await _press(client, p, again["id"], "done")
    assert r.status_code == 200 and r.json()["reminder"]["status"] == "confirmed"


async def test_one_done_is_never_a_dose_two_are_and_not_today_records_skipped(client, families):
    fam = families["a"]
    p = await _setup(client, fam, medication=True)
    _, due = await _remind(client, fam, p, kind="medication", text="Blue tablet", medication=True)
    r = await _press(client, p, due["id"], "done")
    assert r.json()["reminder"]["status"] == "confirming"
    assert await _events(client, p) == [], "one press of Done writes no dose (H14)"
    second = await _due(client, p)
    assert second and second["id"] != due["id"], "asked once more, in different words"
    r = await _press(client, p, second["id"], "done")
    assert r.json()["reminder"]["status"] == "confirmed"
    events = await _events(client, p)
    assert len(events) == 1 and events[0]["status"] == "taken"

    # A second medication reminder, answered "Not today": a skipped dose, said plainly.
    p2 = await _setup(client, families["b"], medication=True)
    _, due = await _remind(client, families["b"], p2, kind="medication", text="Blue tablet", medication=True)
    r = await _press(client, p2, due["id"], "skip")
    assert r.json()["reminder"]["status"] == "skipped"
    events = await _events(client, p2)
    assert len(events) == 1 and events[0]["status"] == "skipped"


async def test_a_choice_on_a_check_in_that_is_not_a_reminder_is_refused(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    await cycle.run_workspace(p["jp"], dt.datetime.now(UTC))
    async with workspace_connection(p["jp"]) as conn:
        await conn.execute("UPDATE care_checkins SET scheduled_at = $2 WHERE person_id = $1 AND status = 'scheduled'",
                           uuid.UUID(p["id"]), dt.datetime.now(UTC) - dt.timedelta(seconds=1))
    await cycle.run_workspace(p["jp"], dt.datetime.now(UTC))
    due = await _due(client, p)
    assert due is not None and due["reminder_id"] is None
    r = await _press(client, p, due["id"], "done")
    assert r.status_code == 422, "'Done' means nothing to 'How are you?'"
    async with workspace_connection(p["jp"]) as conn:
        status = await conn.fetchval("SELECT status FROM care_checkins WHERE id = $1", uuid.UUID(due["id"]))
    assert status != "answered", "a refused button leaves the question open"


async def test_the_same_press_sent_twice_changes_nothing_the_second_time(client, families):
    fam = families["a"]
    p = await _setup(client, fam, medication=True)
    _, due = await _remind(client, fam, p, kind="medication", text="Blue tablet", medication=True)
    key = str(uuid.uuid4())
    first = await _press(client, p, due["id"], "done", key=key)
    again = await _press(client, p, due["id"], "done", key=key)
    assert first.status_code == again.status_code == 200
    assert again.headers.get("X-Idempotent-Replay") == "true"
    assert again.json()["reminder"]["status"] == "confirming", "the outbox re-sending one press is not a second Done"
    assert await _events(client, p) == []


async def test_an_unknown_choice_is_refused_before_anything_is_read(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    _, due = await _remind(client, fam, p)
    r = await _press(client, p, due["id"], "taken")
    assert r.status_code == 422
