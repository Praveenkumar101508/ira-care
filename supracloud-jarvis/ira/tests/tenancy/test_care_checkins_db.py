"""IRA CARE (phase 3): the check-in cycle against a real PostgreSQL.

The cycle is driven with explicit clocks so a whole day can pass in a test:
scheduling, quiet hours, the watch collecting the question, the repeat, the
answer (with a medication dose confirmed), a miss, and a check-in the watch
never collected. Then the same steps over the real HTTP routes.
"""
from __future__ import annotations

import datetime as dt
import os
import random
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")
pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy._care_fixtures import client, families, pool  # noqa: E402,F401

from care import checkin_cycle as cycle  # noqa: E402

C = "/api/v1/care"
UTC = dt.timezone.utc


async def _setup(client, fam, *, timezone="UTC", med_time=None) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people", json={"display_name": "Test Person", "preferred_name": "Tess",
                                               "timezone": timezone})
    assert r.status_code == 201, r.text
    person = r.json()
    r = await client.post(f"{C}/people/{person['id']}/circle", json={
        "display_name": "Family Member", "role": "family", "user_id": str(fam["member"])})
    assert r.status_code == 201
    if med_time:
        r = await client.post(f"{C}/people/{person['id']}/medications",
                              json={"name": "Blue tablet", "dose": "1", "times": [med_time]})
        assert r.status_code == 201, r.text
        person["medication"] = r.json()
    r = await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})
    code = r.json()["pairing_code"]
    r = await client.post(f"{C}/devices/pair", json={"pairing_code": code})
    assert r.status_code == 200, r.text
    person["device"] = r.json()
    person["token"] = r.json()["device_token"]
    return person


def _device(person) -> dict:
    d = dict(person["device"])
    d["person_id"] = uuid.UUID(person["id"])
    d["id"] = uuid.UUID(d["id"])
    return d


def _principal(fam):
    return cycle.job_principal(fam["tenant_id"], fam["workspace_id"])


async def _rows(pool, fam, person_id):
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,false)", str(fam["tenant_id"]))
        await conn.execute("SELECT set_config('app.workspace_id',$1,false)", str(fam["workspace_id"]))
        rows = await conn.fetch("SELECT * FROM care_checkins WHERE person_id = $1 ORDER BY scheduled_at",
                                uuid.UUID(person_id))
        for k in ("app.tenant_id", "app.workspace_id"):
            await conn.execute(f"SELECT set_config('{k}','',false)")
    return [dict(r) for r in rows]


async def test_a_new_person_gets_a_checkin_fifteen_to_twenty_minutes_on(client, families, pool):
    fam = families["a"]
    person = await _setup(client, fam)
    now = dt.datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
    out = await cycle.run_workspace(_principal(fam), now, rng=random.Random(1))
    assert out.people == 1 and out.scheduled == 1
    rows = await _rows(pool, fam, person["id"])
    assert len(rows) == 1 and rows[0]["status"] == "scheduled"
    assert 15 * 60 <= (rows[0]["scheduled_at"] - now).total_seconds() <= 20 * 60
    # A second pass with nothing due changes nothing.
    out2 = await cycle.run_workspace(_principal(fam), now + dt.timedelta(minutes=5), rng=random.Random(2))
    assert out2.scheduled == 0 and len(await _rows(pool, fam, person["id"])) == 1


async def test_the_watch_collects_the_question_once_and_the_answer_is_understood(client, families, pool):
    fam = families["a"]
    person = await _setup(client, fam, med_time="08:00")
    p, device = _principal(fam), _device(person)
    now = dt.datetime(2026, 11, 21, 7, 50, tzinfo=UTC)
    await cycle.run_workspace(p, now, rng=random.Random(1))
    assert await cycle.due_checkin(p, device, now) is None  # not due yet
    later = now + dt.timedelta(minutes=21)
    asked = await cycle.due_checkin(p, device, later, rng=random.Random(1))
    assert asked is not None and asked["status"] == "asked"
    assert asked["question_kind"] == "medication" and "blue tablet" in asked["question_text"].lower()
    assert "Tess" in asked["question_text"]
    again = await cycle.due_checkin(p, device, later + dt.timedelta(seconds=30))
    assert again["id"] == asked["id"] and again["question_text"] == asked["question_text"]
    repeated = await cycle.mark_repeated(p, device, asked["id"], later + dt.timedelta(seconds=60))
    assert repeated["status"] == "repeated" and repeated["repeated_at"] is not None
    answered = await cycle.record_answer(p, device, asked["id"], "Yes, I took it with my tea",
                                         via="voice", now=later + dt.timedelta(seconds=90))
    assert answered["status"] == "answered" and answered["said"] == "yes"
    assert "taken" in answered["answer_summary"].lower()
    # The dose is recorded, the words are in the transcript, the next check-in is scheduled.
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.get(f"{C}/people/{person['id']}/medication-events")
    events = r.json()["events"]
    assert len(events) == 1 and events[0]["status"] == "taken" and events[0]["confirmed_via"] == "checkin"
    assert events[0]["due_at"].startswith("2026-11-21T08:00")
    r = await client.post(f"{C}/people/{person['id']}/consents",
                          json={"kind": "transcripts_family", "granted": True, "granted_by": "person"})
    client.act_as(fam, "member", "member")
    r = await client.get(f"{C}/people/{person['id']}/transcripts")
    speakers = [(t["speaker"], t["text"]) for t in r.json()["transcripts"]]
    assert ("person", "Yes, I took it with my tea") in speakers
    assert any(s == "companion" and "blue tablet" in t.lower() for s, t in speakers)
    out = await cycle.run_workspace(p, later + dt.timedelta(minutes=2), rng=random.Random(5))
    assert out.scheduled == 1


async def test_summaries_are_shown_but_words_need_consent(client, families, pool):
    fam = families["a"]
    person = await _setup(client, fam)
    p, device = _principal(fam), _device(person)
    now = dt.datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
    await cycle.run_workspace(p, now, rng=random.Random(1))
    asked = await cycle.due_checkin(p, device, now + dt.timedelta(minutes=21), rng=random.Random(1))
    await cycle.record_answer(p, device, asked["id"], "My knee hurts a bit today", now=now + dt.timedelta(minutes=22))
    client.act_as(fam, "member", "member")
    r = await client.get(f"{C}/people/{person['id']}/checkins")
    assert r.status_code == 200 and r.json()["answers_included"] is False
    row = r.json()["checkins"][0]
    assert row["answer_text"] == "" and "mentioned pain" in row["answer_summary"]
    client.act_as(fam, "admin", "workspace_admin")
    await client.post(f"{C}/people/{person['id']}/consents",
                      json={"kind": "transcripts_family", "granted": True, "granted_by": "person"})
    client.act_as(fam, "member", "member")
    r = await client.get(f"{C}/people/{person['id']}/checkins")
    assert r.json()["answers_included"] is True
    assert r.json()["checkins"][0]["answer_text"] == "My knee hurts a bit today"


async def test_quiet_hours_are_silent_and_the_next_question_waits_for_morning(client, families, pool):
    fam = families["a"]
    person = await _setup(client, fam, timezone="Europe/London")
    p, device = _principal(fam), _device(person)
    evening = dt.datetime(2026, 11, 21, 21, 30, tzinfo=UTC)
    await cycle.run_workspace(p, evening, rng=random.Random(1))
    rows = await _rows(pool, fam, person["id"])
    assert rows[0]["scheduled_at"] < dt.datetime(2026, 11, 21, 22, 0, tzinfo=UTC)
    night = dt.datetime(2026, 11, 21, 23, 0, tzinfo=UTC)
    assert await cycle.due_checkin(p, device, night) is None, "the watch must stay silent at night"
    out = await cycle.run_workspace(p, night, rng=random.Random(2))
    assert out.skipped_quiet == 1 and out.scheduled == 1
    rows = await _rows(pool, fam, person["id"])
    assert [r["status"] for r in rows] == ["skipped_quiet", "scheduled"]
    assert rows[1]["scheduled_at"] >= dt.datetime(2026, 11, 22, 7, 1, tzinfo=UTC)
    assert rows[1]["scheduled_at"] <= dt.datetime(2026, 11, 22, 7, 15, tzinfo=UTC)
    morning = dt.datetime(2026, 11, 22, 7, 20, tzinfo=UTC)
    asked = await cycle.due_checkin(p, device, morning, rng=random.Random(3))
    assert asked is not None and asked["status"] == "asked"


async def test_no_answer_becomes_a_miss_and_so_does_a_question_never_collected(client, families, pool):
    fam = families["a"]
    person = await _setup(client, fam)
    p, device = _principal(fam), _device(person)
    now = dt.datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
    await cycle.run_workspace(p, now, rng=random.Random(1))
    t_ask = now + dt.timedelta(minutes=21)
    asked = await cycle.due_checkin(p, device, t_ask, rng=random.Random(1))
    # Still open just before the deadline (60 s repeat + 120 s sensors + 60 s grace).
    out = await cycle.run_workspace(p, t_ask + dt.timedelta(seconds=230), rng=random.Random(1))
    assert out.missed == 0
    out = await cycle.run_workspace(p, t_ask + dt.timedelta(seconds=250), rng=random.Random(1))
    assert out.missed == 1 and out.scheduled == 1
    rows = await _rows(pool, fam, person["id"])
    assert str(rows[0]["id"]) == str(asked["id"]) and rows[0]["status"] == "missed"
    assert rows[0]["answer_summary"] == "No answer."
    # The watch never collected the next one (offline): missed after a whole window.
    nxt = rows[1]
    out = await cycle.run_workspace(p, nxt["scheduled_at"] + dt.timedelta(minutes=19), rng=random.Random(1))
    assert out.missed == 0
    out = await cycle.run_workspace(p, nxt["scheduled_at"] + dt.timedelta(minutes=22), rng=random.Random(1))
    assert out.missed == 1
    rows = await _rows(pool, fam, person["id"])
    assert rows[1]["status"] == "missed" and "Not delivered" in rows[1]["answer_summary"]
    assert rows[2]["status"] == "scheduled"
    # A late answer to a missed check-in is refused: it is no longer open.
    assert await cycle.record_answer(p, device, asked["id"], "sorry, here now") is None


async def test_the_watch_side_over_the_real_routes(client, families, pool):
    fam = families["a"]
    person = await _setup(client, fam)
    # These routes run on the real clock, so switch quiet hours off for this person:
    # otherwise the test passes by day and fails after 22:00 UTC.
    s = (await client.get(f"{C}/people/{person['id']}/settings")).json()
    body = {k: v for k, v in s.items() if k not in ("person_id", "tenant_id", "workspace_id", "set_by", "updated_at")}
    body.update(quiet_start="00:00", quiet_end="00:00")
    assert (await client.put(f"{C}/people/{person['id']}/settings", json=body)).status_code == 200
    h = {"X-Device-Token": person["token"]}
    r = await client.get(f"{C}/devices/me/checkins/due", headers=h)
    assert r.status_code == 200 and r.json()["checkin"] is None
    # A viewer cannot ask for a check-in now; a member can.
    client.act_as(fam, "viewer", "viewer")
    assert (await client.post(f"{C}/people/{person['id']}/checkins/ask-now", json={})).status_code == 403
    client.act_as(fam, "member", "member")
    r = await client.post(f"{C}/people/{person['id']}/checkins/ask-now", json={"kind": "mood"})
    assert r.status_code == 201, r.text
    r = await client.get(f"{C}/devices/me/checkins/due", headers=h)
    due = r.json()["checkin"]
    assert due is not None and due["status"] == "asked" and "Tess" in due["question_text"]
    r = await client.post(f"{C}/devices/me/checkins/{due['id']}/repeated", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "repeated"
    r = await client.post(f"{C}/devices/me/checkins/{due['id']}/answer", headers=h,
                          json={"text": "", "via": "button"})
    assert r.status_code == 200 and r.json()["status"] == "answered"
    assert r.json()["answer_summary"] == "No answer." or r.json()["answered_via"] == "button"
    # Second answer to the same check-in: no longer open.
    r = await client.post(f"{C}/devices/me/checkins/{due['id']}/answer", headers=h, json={"text": "yes"})
    assert r.status_code == 404
    # A new one, reported missed by the watch.
    client.act_as(fam, "member", "member")
    await client.post(f"{C}/people/{person['id']}/checkins/ask-now", json={})
    due2 = (await client.get(f"{C}/devices/me/checkins/due", headers=h)).json()["checkin"]
    r = await client.post(f"{C}/devices/me/checkins/{due2['id']}/missed", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "missed"
    # Another family's watch cannot touch these check-ins.
    other = await _setup(client, families["b"])
    r = await client.post(f"{C}/devices/me/checkins/{due2['id']}/answer",
                          headers={"X-Device-Token": other["token"]}, json={"text": "yes"})
    assert r.status_code == 404


async def test_the_whole_cycle_runs_over_every_tenant(client, families, pool):
    a, b = families["a"], families["b"]
    pa = await _setup(client, a)
    pb = await _setup(client, b)
    out = await cycle.run_cycle_once(dt.datetime(2026, 11, 21, 10, 0, tzinfo=UTC), rng=random.Random(1))
    assert out.errors == 0 and out.scheduled >= 2
    assert len(await _rows(pool, a, pa["id"])) == 1 and len(await _rows(pool, b, pb["id"])) == 1


async def test_the_question_is_prepared_a_few_minutes_early_and_asked_in_those_words_at_once(client, families, pool, monkeypatch):
    """Round 4: the scheduler composes the next check-in's question up to five
    minutes before it is due, so the watch speaks it the moment it is due."""
    from care import conversation as conv
    fam = families["a"]
    person = await _setup(client, fam, med_time="08:00")
    p, device = _principal(fam), _device(person)
    now = dt.datetime(2026, 11, 21, 7, 40, tzinfo=UTC)
    await cycle.run_workspace(p, now, rng=random.Random(1))
    row = (await _rows(pool, fam, person["id"]))[0]
    assert row["status"] == "scheduled" and row["question_text"] == ""
    due = row["scheduled_at"]
    # Too early: nothing is prepared. Within five minutes: the words are composed and kept.
    out = await cycle.run_workspace(p, due - dt.timedelta(minutes=6), rng=random.Random(2))
    assert out.prepared == 0 and (await _rows(pool, fam, person["id"]))[0]["question_text"] == ""
    composed = conv.FakeModel()
    conv.use_model(composed)
    try:
        out = await cycle.run_workspace(p, due - dt.timedelta(minutes=4), rng=random.Random(3))
        assert out.prepared == 1
        prepared = (await _rows(pool, fam, person["id"]))[0]
        assert prepared["status"] == "scheduled" and "blue tablet" in prepared["question_text"].lower()
        assert prepared["question_kind"] == "medication" and prepared["question_source"] == "model"
        calls_before = len(composed.calls)
        # A second pass does not compose it again.
        out = await cycle.run_workspace(p, due - dt.timedelta(minutes=2), rng=random.Random(4))
        assert out.prepared == 0 and len(composed.calls) == calls_before
        # Due: the watch gets the prepared words without waiting on the model.
        asked = await cycle.due_checkin(p, device, due + dt.timedelta(seconds=5), rng=random.Random(5))
        assert asked["prepared"] is True and asked["question_text"] == prepared["question_text"]
        assert asked["status"] == "asked" and asked["question_source"] == "model" and len(composed.calls) == calls_before
    finally:
        conv.use_model(None)
    # The answer is still read against the medication the prepared question named.
    answered = await cycle.record_answer(p, device, asked["id"], "Yes, taken", via="voice", now=due + dt.timedelta(seconds=40))
    assert answered["said"] == "yes"
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.get(f"{C}/people/{person['id']}/medication-events")
    assert [e["status"] for e in r.json()["events"]] == ["taken"]
