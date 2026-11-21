"""IRA CARE round 2, phase 4: IRA really talks, against a real PostgreSQL.

Check-ins are composed by the (fake) model from the person's context and fall
back to the template; answers are read into a structure with the stricter
reading winning; red-flag phrases ask first; Talk to IRA streams sentences,
keeps transcripts under consent, rolls the memory, and shows a contact to
call; the person's words never reach a summary.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random

import pytest

asyncpg = pytest.importorskip("asyncpg")
pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy._care_fixtures import client, families, pool  # noqa: E402,F401

from care import checkin_cycle as cycle  # noqa: E402
from care import conversation as conv  # noqa: E402

C = "/api/v1/care"
UTC = dt.timezone.utc


@pytest.fixture(autouse=True)
def _fake_model():
    conv.use_model(conv.FakeModel())
    yield
    conv.use_model(None)


async def _setup(client, fam) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people", json={"display_name": "Test Person", "preferred_name": "Tess", "timezone": "UTC"})
    person = r.json()
    await client.post(f"{C}/people/{person['id']}/circle", json={"display_name": "Family Member", "role": "family", "tier": 1,
                      "relationship": "daughter", "user_id": str(fam["member"]), "phone": "+440000000001"})
    await client.post(f"{C}/people/{person['id']}/circle", json={"display_name": "Carer One", "role": "carer", "tier": 1, "phone": "+440000000002"})
    await client.post(f"{C}/people/{person['id']}/routines", json={"name": "lunch", "kind": "meal", "time_of_day": "12:30"})
    await client.post(f"{C}/people/{person['id']}/medications", json={"name": "the morning tablets", "dose": "one", "times": ["08:00"]})
    r = await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})
    paired = (await client.post(f"{C}/devices/pair", json={"pairing_code": r.json()["pairing_code"]})).json()
    person["h"] = {"X-Device-Token": paired["device_token"]}
    person["device"] = dict(paired, person_id=__import__("uuid").UUID(person["id"]), id=__import__("uuid").UUID(paired["id"]))
    person["jp"] = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    return person


def _lines(text: str) -> list[dict]:
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


async def test_checkins_are_composed_by_the_model_and_fall_back_to_the_template(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    now = dt.datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    await cycle.run_workspace(p["jp"], now, rng=random.Random(1))
    due = await cycle.due_checkin(p["jp"], p["device"], now + dt.timedelta(minutes=25), rng=random.Random(1))
    assert due is not None and due["question_source"] == "model" and "Tess" in due["question_text"]
    assert due["question_text"].count("?") == 1
    # The model breaks: the template is asked, and the record says so.
    conv.use_model(conv.FakeModel(broken=True))
    await cycle.record_answer(p["jp"], p["device"], due["id"], "fine thanks", now=now + dt.timedelta(minutes=26))
    await cycle.run_workspace(p["jp"], now + dt.timedelta(minutes=27), rng=random.Random(2))
    due2 = await cycle.due_checkin(p["jp"], p["device"], now + dt.timedelta(minutes=50), rng=random.Random(2))
    assert due2 is not None and due2["question_source"] == "template" and "Tess" in due2["question_text"]


async def test_answers_are_read_into_a_structure_and_the_stricter_reading_wins(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    now = dt.datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    await cycle.run_workspace(p["jp"], now, rng=random.Random(1))
    due = await cycle.due_checkin(p["jp"], p["device"], now + dt.timedelta(minutes=25), rng=random.Random(1))
    # The model would call this fine; the keywords hear "help": help wins.
    conv.use_model(conv.FakeModel(json_reply={"wellbeing": "good", "needs_help": False, "summary": "Said they were fine."}))
    out = await cycle.record_answer(p["jp"], p["device"], due["id"], "I'm fine, but please get help", now=now + dt.timedelta(minutes=26))
    assert out["asked_for_help"] is True and out["structured"]["needs_help"] is True and out["reading_source"] == "model+rules"
    assert "Said they were fine" in out["answer_summary"] and "asked for help" in out["answer_summary"]
    # A model summary quoting the person is dropped for the keyword summary.
    await cycle.run_workspace(p["jp"], now + dt.timedelta(minutes=27), rng=random.Random(2))
    due2 = await cycle.due_checkin(p["jp"], p["device"], now + dt.timedelta(minutes=50), rng=random.Random(2))
    words = "I went to the shops with my neighbour this morning"
    conv.use_model(conv.FakeModel(json_reply={"wellbeing": "good", "summary": f"Said: {words}"}))
    out = await cycle.record_answer(p["jp"], p["device"], due2["id"], words, now=now + dt.timedelta(minutes=51))
    assert words not in out["answer_summary"] and "neighbour" not in out["answer_summary"]
    client.act_as(fam, "viewer", "viewer")
    listed = (await client.get(f"{C}/people/{p['id']}/checkins")).json()["checkins"]
    assert all("neighbour" not in (c["answer_summary"] or "") for c in listed)


async def test_a_red_flag_phrase_in_an_answer_asks_first_then_escalates(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    now = dt.datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    await cycle.run_workspace(p["jp"], now, rng=random.Random(1))
    due = await cycle.due_checkin(p["jp"], p["device"], now + dt.timedelta(minutes=25), rng=random.Random(1))
    out = await cycle.record_answer(p["jp"], p["device"], due["id"], "I fell in the kitchen", now=now + dt.timedelta(minutes=26))
    assert out["safety_check"] is True and out["asked_for_help"] is True
    client.act_as(fam, "member", "member")
    alerts = (await client.get(f"{C}/alerts", params={"open_only": "false"})).json()["alerts"]
    assert alerts[0]["kind"] == "concerning_phrase" and alerts[0]["status"] == "asking"
    assert "kitchen" not in alerts[0]["details"]["summary"], "the alert describes, it does not quote"
    safety = await cycle.due_checkin(p["jp"], p["device"], now + dt.timedelta(minutes=26, seconds=5))
    assert safety is not None and safety["safety"] is True
    from care import escalation
    out2 = await escalation.run_workspace(p["jp"], now + dt.timedelta(minutes=28))
    assert out2.promoted == 1 and out2.tiers_run == 1


async def test_talk_streams_sentences_keeps_transcripts_under_consent_and_rolls_the_memory(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    r = await client.post(f"{C}/devices/me/talk", json={"text": "I had a lovely walk this morning"}, headers=p["h"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    lines = _lines(r.text)
    assert lines[-1]["done"] is True and lines[-1]["source"] == "fake" and lines[-1]["needs_help"] is False
    sentences = [ln["sentence"] for ln in lines if "sentence" in ln]
    assert sentences and "Tess" in sentences[0]
    # The day's plan and the tablets come from the record, not the model.
    r = await client.post(f"{C}/devices/me/talk", json={"text": "what's on today?"}, headers=p["h"])
    assert "lunch at 12:30" in _lines(r.text)[0]["sentence"]
    r = await client.post(f"{C}/devices/me/talk", json={"text": "should I take another tablet?"}, headers=p["h"])
    assert _lines(r.text)[0]["sentence"] == "I can't advise on that, Tess. Shall I let Carer One know?"
    # Transcripts hold both sides, and are private without consent.
    client.act_as(fam, "member", "member")
    assert (await client.get(f"{C}/people/{p['id']}/transcripts")).status_code == 403
    client.act_as(fam, "admin", "workspace_admin")
    await client.post(f"{C}/people/{p['id']}/consents", json={"kind": "transcripts_family", "granted": True, "granted_by": "person", "granted_by_name": "Tess"})
    client.act_as(fam, "member", "member")
    t = (await client.get(f"{C}/people/{p['id']}/transcripts")).json()["transcripts"]
    assert {x["speaker"] for x in t} == {"person", "companion"} and any("lovely walk" in x["text"] for x in t)
    assert all(x["checkin_id"] is None for x in t)
    # The memory is summaries only, and the read is logged.
    mem = (await client.get(f"{C}/people/{p['id']}/memory")).json()["memory"]
    assert len(mem) == 1 and mem[0]["exchanges"] == 3 and "lovely walk" not in mem[0]["summary"]
    assert "Talked (watch)" in mem[0]["summary"]
    client.act_as(fam, "admin", "workspace_admin")
    log = (await client.get(f"{C}/people/{p['id']}/access-log")).json()["entries"]
    assert any(e["detail"].get("memory") for e in log)


async def test_talk_from_the_phone_shows_a_contact_to_call_and_a_concerning_phrase_asks_first(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    client.act_as(fam, "member", "member")
    r = await client.post(f"{C}/people/{p['id']}/talk", json={"text": "call my daughter"})
    assert r.status_code == 200
    done = _lines(r.text)[-1]
    assert done["action"] == {"kind": "call", "name": "Family Member", "phone": "+440000000001"}
    r = await client.post(f"{C}/people/{p['id']}/talk", json={"text": "I can't breathe properly"})
    done = _lines(r.text)[-1]
    assert done["needs_help"] is True and done["action"]["kind"] == "safety_check" and done["action"]["alert_id"]
    alerts = (await client.get(f"{C}/alerts", params={"open_only": "false"})).json()["alerts"]
    assert alerts[0]["kind"] == "concerning_phrase" and alerts[0]["status"] == "asking"
    # A viewer may not talk as the person; another family's person is not found.
    client.act_as(fam, "viewer", "viewer")
    assert (await client.post(f"{C}/people/{p['id']}/talk", json={"text": "hello"})).status_code == 403
    client.act_as(families["b"], "member", "member")
    assert (await client.post(f"{C}/people/{p['id']}/talk", json={"text": "hello"})).status_code == 404


async def test_the_memory_is_purged_by_retention_and_erased_with_the_person(client, families, pool):
    fam = families["a"]
    p = await _setup(client, fam)
    await client.post(f"{C}/devices/me/talk", json={"text": "hello there"}, headers=p["h"])
    from care import retention
    from core.identity.db_context import workspace_connection
    async with workspace_connection(p["jp"]) as conn:
        await conn.execute("UPDATE care_conversation_memory SET updated_at = NOW() - interval '400 days', day = CURRENT_DATE - 400 WHERE person_id = $1",
                           p["device"]["person_id"])
    await retention.run_workspace(p["jp"])
    async with workspace_connection(p["jp"]) as conn:
        assert await conn.fetchval("SELECT count(*) FROM care_conversation_memory WHERE person_id = $1", p["device"]["person_id"]) == 0
    await client.post(f"{C}/devices/me/talk", json={"text": "hello again"}, headers=p["h"])
    client.act_as(fam, "admin", "workspace_admin")
    assert (await client.delete(f"{C}/people/{p['id']}")).status_code == 204
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id', $1, false)", str(fam["tenant_id"]))
        await conn.execute("SELECT set_config('app.workspace_id', $1, false)", str(fam["workspace_id"]))
        assert await conn.fetchval("SELECT count(*) FROM care_conversation_memory WHERE person_id = $1", p["device"]["person_id"]) == 0
