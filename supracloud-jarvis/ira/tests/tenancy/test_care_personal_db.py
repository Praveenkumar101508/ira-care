"""IRA, personal to the person (110, phase 7; care/personal.py).

Real HTTP stack, real PostgreSQL as `ira_app`, the clock moved by hand. Covers:
the profile (who may edit it, consent gating the topic lists, what reaches the
watch), a reminder spoken to IRA becoming a question on the watch at its time
and being ticked off, the two-step medication confirmation, hydration prompts,
what IRA remembers being edited and cleared by the representative, a concern
alert labelled and linked to its transcript, and tenancy.
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

from tests.tenancy._care_fixtures import client, families, pool  # noqa: E402,F401

from care import checkin_cycle as cycle  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

C = "/api/v1/care"
UTC = dt.timezone.utc


async def _setup(client, fam, *, medication=False) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people", json={"display_name": "Test Person", "preferred_name": "Tess", "timezone": "UTC"})
    person = r.json()
    r = await client.post(f"{C}/people/{person['id']}/circle", json={
        "display_name": "Sravani", "role": "family", "relationship": "daughter", "user_id": str(fam["member"]), "phone": "+440000000001"})
    person["member"] = r.json()
    s = (await client.get(f"{C}/people/{person['id']}/settings")).json()
    body = {k: v for k, v in s.items() if k not in ("person_id", "tenant_id", "workspace_id", "set_by", "updated_at")}
    body["quiet_start"], body["quiet_end"] = "00:00", "00:00"   # no quiet hours: the clock does not decide this test
    assert (await client.put(f"{C}/people/{person['id']}/settings", json=body)).status_code == 200
    if medication:
        r = await client.post(f"{C}/people/{person['id']}/medications", json={"name": "Blue tablet", "dose": "1", "times": ["08:00"]})
        person["medication"] = r.json()
    r = await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})
    paired = (await client.post(f"{C}/devices/pair", json={"pairing_code": r.json()["pairing_code"]})).json()
    person["h"] = {"X-Device-Token": paired["device_token"]}
    person["jp"] = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    return person


async def _due(client, p):
    return (await client.get(f"{C}/devices/me/checkins/due", headers=p["h"])).json()["checkin"]


async def _answer(client, p, checkin_id, text):
    r = await client.post(f"{C}/devices/me/checkins/{checkin_id}/answer", json={"text": text, "via": "voice"}, headers=p["h"])
    assert r.status_code == 200, r.text
    return r.json()


async def test_the_profile_is_edited_by_the_representative_and_reaches_the_watch(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    prof = (await client.get(f"{C}/people/{p['id']}/profile")).json()
    assert (prof["pace"], prof["prompting_level"], prof["topics_consented"]) == ("normal", "standard", False)
    # The daughter (a representative) slows IRA down, turns vibration off and names herself for worries.
    client.act_as(fam, "member", "member")
    body = {"pace": "slow", "prompting_level": "light", "voice_on": True, "touch_on": True, "vibration_on": False,
            "topics_avoid": ["the accident"], "familiar_topics": ["the garden"], "redirect_member_id": p["member"]["id"]}
    r = await client.put(f"{C}/people/{p['id']}/profile", json=body)
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["pace"] == "slow" and saved["updated_by_role"] == "representative" and saved["redirect_name"] == "Sravani"
    assert saved["topics_avoid"] == [] and saved["familiar_topics"] == [], "no topic lists before the topics consent"
    # The watch is told the pace and the prompting, not the topics.
    me = (await client.get(f"{C}/devices/me", headers=p["h"])).json()
    assert me["profile"] == {"pace": "slow", "prompting_level": "light", "speech_rate": 0.8, "repeat_after_s": 90, "repeats": 0,
                             "voice": True, "touch": True, "vibration": False}
    # With the consent recorded, the lists are kept.
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{p['id']}/consents", json={"kind": "topics", "granted": True, "granted_by": "person", "granted_by_name": "Tess"})
    assert r.status_code == 201, r.text
    client.act_as(fam, "member", "member")
    saved = (await client.put(f"{C}/people/{p['id']}/profile", json=body)).json()
    assert saved["topics_avoid"] == ["the accident"] and saved["familiar_topics"] == ["the garden"] and saved["topics_consented"]
    # A member of another family cannot see or change it.
    client.act_as(families["b"], "admin", "workspace_admin")
    assert (await client.get(f"{C}/people/{p['id']}/profile")).status_code == 404
    assert (await client.put(f"{C}/people/{p['id']}/profile", json=body)).status_code == 404


async def test_a_reminder_spoken_to_ira_is_asked_at_its_time_and_ticked_off(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    client.act_as(fam, "member", "member")
    # "Remind me to ring Sravani in 20 minutes" from the phone's Talk to IRA.
    r = await client.post(f"{C}/people/{p['id']}/talk", json={"text": "Remind me to ring Sravani in 20 minutes"})
    assert r.status_code == 200, r.text
    lines = [line for line in r.text.strip().split("\n") if line.strip()]
    import json
    done = json.loads(lines[-1])
    assert done["action"]["kind"] == "reminder" and done["action"]["when"] == "in 20 minutes" and done["action"].get("reminder_id")
    reminders = (await client.get(f"{C}/people/{p['id']}/reminders")).json()["reminders"]
    assert len(reminders) == 1 and reminders[0]["text"] == "ring Sravani" and reminders[0]["status"] == "pending"
    due_at = dt.datetime.fromisoformat(reminders[0]["due_at"])
    assert dt.timedelta(minutes=19) < due_at - dt.datetime.now(UTC) < dt.timedelta(minutes=21)
    # Not yet: the cycle leaves it; nothing is due on the watch beyond the ordinary schedule.
    await cycle.run_workspace(p["jp"], dt.datetime.now(UTC))
    assert (await client.get(f"{C}/people/{p['id']}/reminders")).json()["reminders"][0]["status"] == "pending"
    # At its time the cycle turns it into the question the watch asks, in IRA's words.
    async with workspace_connection(p["jp"]) as conn:
        await conn.execute("UPDATE care_reminders SET due_at = $1", dt.datetime.now(UTC) - dt.timedelta(seconds=1))
        await conn.execute("UPDATE care_checkins SET status = 'cancelled' WHERE person_id = $1 AND status = 'scheduled'", uuid.UUID(p["id"]))
    out = await cycle.run_workspace(p["jp"], dt.datetime.now(UTC))
    assert out.reminders_asked == 1
    due = await _due(client, p)
    assert due and due["question_kind"] == "reminder" and "ring Sravani" in due["question_text"] and due["reminder_id"]
    assert (await client.get(f"{C}/people/{p['id']}/reminders")).json()["reminders"][0]["status"] == "asked"
    # "Later": asked again in ten minutes.
    a = await _answer(client, p, due["id"], "later please")
    assert a["reminder"]["status"] == "snoozed" and "ask again in 10 minutes" in a["reply"]
    async with workspace_connection(p["jp"]) as conn:
        await conn.execute("UPDATE care_reminders SET snoozed_until = $1", dt.datetime.now(UTC) - dt.timedelta(seconds=1))
    await cycle.run_workspace(p["jp"], dt.datetime.now(UTC))
    due = await _due(client, p)
    assert due and due["question_kind"] == "reminder"
    a = await _answer(client, p, due["id"], "yes, tick it off")
    assert a["reminder"]["status"] == "confirmed" and "written that down" in a["reply"]
    # The memory holds a note about the reminder, never the words.
    mem = (await client.get(f"{C}/people/{p['id']}/memory")).json()["memory"]
    assert mem and "reminded about ring Sravani" in mem[0]["summary"] and "20 minutes" not in mem[0]["summary"].replace("in 20 minutes", "")


async def test_a_dose_is_confirmed_in_two_steps_and_hydration_prompts_follow_the_setting(client, families):
    fam = families["a"]
    p = await _setup(client, fam, medication=True)
    client.act_as(fam, "member", "member")
    r = await client.post(f"{C}/people/{p['id']}/reminders", json={
        "text": "Blue tablet", "kind": "medication", "medication_id": p["medication"]["id"],
        "due_at": (dt.datetime.now(UTC) - dt.timedelta(seconds=1)).isoformat()})
    assert r.status_code == 201, r.text
    async with workspace_connection(p["jp"]) as conn:
        await conn.execute("UPDATE care_checkins SET status = 'cancelled' WHERE person_id = $1 AND status = 'scheduled'", uuid.UUID(p["id"]))
    await cycle.run_workspace(p["jp"], dt.datetime.now(UTC))
    due = await _due(client, p)
    assert due["question_kind"] == "medication" and "Blue tablet" in due["question_text"] and "Have you taken it" in due["question_text"]
    # Step one: "yes" is not yet a dose on the record.
    a = await _answer(client, p, due["id"], "yes I have")
    assert a["reminder"]["status"] == "confirming" and "One more check" in a["reply"]
    events = (await client.get(f"{C}/people/{p['id']}/medication-events")).json()["events"]
    assert events == [], "nothing written down after one yes"
    # Step two is asked straight away, in different words; "no" sends it back for later.
    due2 = await _due(client, p)
    assert due2 and due2["id"] != due["id"] and "Is that right?" in due2["question_text"]
    a = await _answer(client, p, due2["id"], "no, not yet")
    assert a["reminder"]["status"] == "snoozed"
    assert (await client.get(f"{C}/people/{p['id']}/medication-events")).json()["events"] == []
    # Asked again, yes twice: the dose is recorded as taken, confirmed from the check-in.
    async with workspace_connection(p["jp"]) as conn:
        await conn.execute("UPDATE care_reminders SET snoozed_until = $1", dt.datetime.now(UTC) - dt.timedelta(seconds=1))
    await cycle.run_workspace(p["jp"], dt.datetime.now(UTC))
    d3 = await _due(client, p)
    assert (await _answer(client, p, d3["id"], "yes"))["reminder"]["status"] == "confirming"
    d4 = await _due(client, p)
    a = await _answer(client, p, d4["id"], "yes that's right")
    assert a["reminder"]["status"] == "confirmed"
    events = (await client.get(f"{C}/people/{p['id']}/medication-events")).json()["events"]
    assert len(events) == 1 and events[0]["status"] == "taken" and events[0]["confirmed_via"] == "checkin"
    # Hydration: the setting plants a prompt; off by default.
    assert all(r["kind"] != "hydration" for r in (await client.get(f"{C}/people/{p['id']}/reminders")).json()["reminders"])
    client.act_as(fam, "admin", "workspace_admin")
    s = (await client.get(f"{C}/people/{p['id']}/settings")).json()
    body = {k: v for k, v in s.items() if k not in ("person_id", "tenant_id", "workspace_id", "set_by", "updated_at")}
    body["hydration_every_minutes"] = 120
    assert (await client.put(f"{C}/people/{p['id']}/settings", json=body)).status_code == 200, "120 minutes is allowed"
    body["hydration_every_minutes"] = 30
    assert (await client.put(f"{C}/people/{p['id']}/settings", json=body)).status_code == 422, "under an hour is not"
    await cycle.run_workspace(p["jp"], dt.datetime.now(UTC))
    hyd = [r for r in (await client.get(f"{C}/people/{p['id']}/reminders")).json()["reminders"] if r["kind"] == "hydration"]
    assert len(hyd) == 1 and hyd[0]["status"] == "asked", "the first prompt is asked at once in waking hours"
    due = await _due(client, p)
    assert due and due["question_kind"] == "hydration" and "drink of water" in due["question_text"]
    a = await _answer(client, p, due["id"], "not today")
    assert a["reminder"]["status"] == "skipped"
    await cycle.run_workspace(p["jp"], dt.datetime.now(UTC))
    hyd = [r for r in (await client.get(f"{C}/people/{p['id']}/reminders")).json()["reminders"] if r["kind"] == "hydration"]
    assert len(hyd) == 2 and hyd[0]["status"] == "pending", "the next one is planted two hours on, not asked now"
    assert dt.datetime.fromisoformat(hyd[0]["due_at"]) > dt.datetime.now(UTC) + dt.timedelta(minutes=110)


async def test_what_ira_remembers_is_the_persons_to_change_and_a_concern_is_labelled(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    client.act_as(fam, "member", "member")
    r = await client.post(f"{C}/people/{p['id']}/talk", json={"text": "I had a lovely morning in the garden"})
    assert r.status_code == 200
    mem = (await client.get(f"{C}/people/{p['id']}/memory")).json()["memory"]
    assert len(mem) == 1 and not mem[0].get("edited_at")
    day = mem[0]["day"]
    # The daughter corrects it; the edit is marked and by whom.
    r = await client.put(f"{C}/people/{p['id']}/memory/{day}", json={"summary": "A good morning. Spoke about the garden."})
    assert r.status_code == 200, r.text
    assert r.json()["edited_by"] == "representative" and r.json()["edited_at"]
    mem = (await client.get(f"{C}/people/{p['id']}/memory")).json()["memory"]
    assert mem[0]["summary"] == "A good morning. Spoke about the garden." and mem[0]["edited_by"] == "representative"
    # A carer may not change what IRA remembers; the person's family may clear it.
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{p['id']}/circle", json={"display_name": "Care worker", "role": "carer", "user_id": str(fam["viewer"])})
    assert r.status_code == 201, r.text
    client.act_as(fam, "viewer", "member")
    assert (await client.put(f"{C}/people/{p['id']}/memory/{day}", json={"summary": "x"})).status_code == 403
    client.act_as(fam, "member", "member")
    assert (await client.delete(f"{C}/people/{p['id']}/memory/{day}")).status_code == 204
    assert (await client.get(f"{C}/people/{p['id']}/memory")).json()["memory"] == []
    # A concerning phrase: the alert's details are a summary by IRA, labelled and linked to the transcript row.
    r = await client.post(f"{C}/people/{p['id']}/talk", json={"text": "I can't breathe properly"})
    assert r.status_code == 200
    # The alert is "asking" (ask first, D-034): listed without the open-only filter.
    alerts = [a for a in (await client.get(f"{C}/alerts", params={"person_id": p["id"], "open_only": "false"})).json()["alerts"]
              if a["kind"] == "concerning_phrase"]
    assert alerts and alerts[0]["status"] == "asking"
    d = alerts[0]["details"]
    assert d["summary_by"] == "IRA" and d["said_by"] == "Tess" and d["source"]["kind"] == "transcript" and d["source"]["id"]
    assert "breathe" not in d["summary"], "the words stay in the transcript, behind consent"
    async with workspace_connection(p["jp"]) as conn:
        row = await conn.fetchrow("SELECT text, speaker FROM care_transcripts WHERE id = $1", uuid.UUID(d["source"]["id"]))
    assert row["speaker"] == "person" and "breathe" in row["text"]
    # Another family sees none of it.
    client.act_as(families["b"], "admin", "workspace_admin")
    assert (await client.get(f"{C}/people/{p['id']}/reminders")).status_code == 404
    assert (await client.put(f"{C}/people/{p['id']}/memory/{day}", json={"summary": "x"})).status_code in (403, 404)
