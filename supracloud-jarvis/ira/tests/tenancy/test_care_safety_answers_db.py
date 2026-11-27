"""Remediation group A against a real PostgreSQL: IRA never writes down what it
does not know.

* A safety check ("are you OK?") is resolved only by the I'm OK button or an
  unambiguous spoken OK. Negative, distressed, unclear or empty answers never
  resolve it; unclear ones keep the ORIGINAL deadline.
* A dose is written as taken only for a clear yes about one medicine and one
  scheduled time, even when the model and the classifier insist otherwise.
* A dose already settled is never silently overwritten.
* Who recorded a dose comes from the signed-in caller, not the request.
* Only the person themself may talk to IRA as the person.
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

from care import askfirst  # noqa: E402
from care import checkin_cycle as cycle  # noqa: E402
from care import conversation as conv  # noqa: E402
from care.monitoring import Observation  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

C = "/api/v1/care"
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 25, 8, 0, tzinfo=UTC)


class _LyingModel(conv.FakeModel):
    """A model that says "taken, all well, no help needed" to everything."""

    async def complete_json(self, messages, *, max_tokens):
        self.calls.append(messages)
        import json
        return json.dumps({"wellbeing": "good", "pain_mentioned": False, "medication_taken": "yes",
                           "needs_help": False, "mood_words": [], "summary": "Said everything was fine."})


@pytest.fixture(autouse=True)
def _lying_model():
    conv.use_model(_LyingModel())
    yield
    conv.use_model(None)


async def _setup(client, fam, meds=(("Blue tablet", ["08:00"]),)) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people", json={"display_name": "Test Person", "preferred_name": "Tess", "timezone": "UTC"})
    assert r.status_code == 201, r.text
    person = r.json()
    r = await client.post(f"{C}/people/{person['id']}/circle", json={
        "display_name": "Family Member", "role": "family", "relationship": "daughter", "user_id": str(fam["member"])})
    assert r.status_code == 201, r.text
    person["meds"] = []
    for name, times in meds:
        r = await client.post(f"{C}/people/{person['id']}/medications", json={"name": name, "dose": "1", "times": times})
        assert r.status_code == 201, r.text
        person["meds"].append(r.json())
    r = await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})
    paired = (await client.post(f"{C}/devices/pair", json={"pairing_code": r.json()["pairing_code"]})).json()
    person["device"] = dict(paired, person_id=uuid.UUID(person["id"]), id=uuid.UUID(paired["id"]))
    person["jp"] = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    return person


async def _ask(p, kind, text, asked_at=NOW) -> uuid.UUID:
    async with workspace_connection(p["jp"]) as conn:
        return await conn.fetchval(
            "INSERT INTO care_checkins (person_id, scheduled_at, asked_at, status, question_kind, question_text) "
            "VALUES ($1, $2, $2, 'asked', $3, $4) RETURNING id", uuid.UUID(p["id"]), asked_at, kind, text)


async def _events(p) -> list[dict]:
    async with workspace_connection(p["jp"]) as conn:
        return [dict(r) for r in await conn.fetch(
            "SELECT * FROM care_medication_events WHERE person_id = $1 ORDER BY due_at", uuid.UUID(p["id"]))]


async def _safety_alert(p, now=NOW) -> dict:
    async with workspace_connection(p["jp"]) as conn:
        person = dict(await conn.fetchrow("SELECT * FROM care_people WHERE id = $1", uuid.UUID(p["id"])))
        obs = Observation("concerning_phrase", "sos", "A test observation.", {})
        return await askfirst.start(conn, person, {"ask_first_wait_s": 60}, obs, now)


async def _alert(p, alert_id) -> dict:
    async with workspace_connection(p["jp"]) as conn:
        return dict(await conn.fetchrow("SELECT * FROM care_alerts WHERE id = $1", alert_id))


# ── A4: the safety check never resolves on a guess ───────────────────────────

@pytest.mark.parametrize("words,expected", [
    ("I'm not OK", "open"), ("I don't feel right", "open"), ("I'm dizzy", "open"),
    ("Yes, but I cannot get up", "open"), ("not fine", "open"),
    ("Maybe", "asking"), ("I don't know", "asking"), ("I can't remember", "asking"),
    ("what's for lunch today", "asking"),
])
async def test_a_negative_or_unclear_answer_never_resolves_the_safety_check(client, families, words, expected):
    p = await _setup(client, families["a"])
    alert = await _safety_alert(p)
    out = await cycle.record_answer(p["jp"], p["device"], alert["checkin_id"], words, via="voice",
                                    now=NOW + dt.timedelta(seconds=20))
    assert out is not None
    row = await _alert(p, alert["id"])
    assert row["status"] == expected, (words, row["status"])
    assert row["status"] != "resolved"
    if expected == "asking":
        # The original deadline stands: an unclear answer does not restart it.
        assert row["next_action_at"] == alert["next_action_at"]


async def test_empty_speech_recognition_is_not_the_ok_button(client, families):
    p = await _setup(client, families["a"])
    alert = await _safety_alert(p)
    await cycle.record_answer(p["jp"], p["device"], alert["checkin_id"], "", via="voice", now=NOW + dt.timedelta(seconds=10))
    row = await _alert(p, alert["id"])
    assert row["status"] == "asking" and row["next_action_at"] == alert["next_action_at"]
    # The deadline passes: the alert opens, nobody was falsely reassured.
    async with workspace_connection(p["jp"]) as conn:
        assert await askfirst.promote_overdue(conn, NOW + dt.timedelta(seconds=61)) >= 1
    assert (await _alert(p, alert["id"]))["status"] == "open"


async def test_the_ok_button_and_a_clear_spoken_ok_resolve_the_safety_check(client, families):
    p = await _setup(client, families["a"])
    alert = await _safety_alert(p)
    await cycle.record_answer(p["jp"], p["device"], alert["checkin_id"], "", via="button", now=NOW + dt.timedelta(seconds=5))
    assert (await _alert(p, alert["id"]))["status"] == "resolved"
    alert2 = await _safety_alert(p, NOW + dt.timedelta(minutes=5))
    await cycle.record_answer(p["jp"], p["device"], alert2["checkin_id"], "Yes I'm fine thanks", via="voice",
                              now=NOW + dt.timedelta(minutes=5, seconds=5))
    assert (await _alert(p, alert2["id"]))["status"] == "resolved"


# ── C-1.b: all 18 mandatory answers, through the real check-in cycle ───────────
# The unit test reads each with the rules alone; this one records each as the answer to a real
# medication question against PostgreSQL and checks what was written (round 10, verification).

from tests.care.test_care_safety_answers import MANDATORY  # noqa: E402


@pytest.mark.parametrize("words,expected", MANDATORY, ids=[w or "empty" for w, _ in MANDATORY])
async def test_each_mandatory_answer_is_recorded_as_the_rules_read_it(client, families, words, expected):
    p = await _setup(client, families["a"])
    cid = await _ask(p, "medication", "Hello Tess. Have you taken your blue tablet yet?")
    out = await cycle.record_answer(p["jp"], p["device"], cid, words, now=NOW + dt.timedelta(minutes=2))
    taken = out["said"] == "yes" and out["structured"]["medication_taken"] == "yes"
    assert taken == (expected == "yes"), (words, out["said"], out["structured"]["medication_taken"])


# ── A1–A3: doses ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("words", [
    "I have not taken it", "No, I haven't taken it", "I will take it", "Yes, but not yet", "Okay",
    "I took it yesterday", "I took a different tablet", "I think I took it", "I cannot remember",
    "I might have taken it", "I forgot to take it", "I'll take it now", "",
])
async def test_no_dose_is_written_from_an_unclear_or_negative_answer_even_when_the_model_says_yes(client, families, words):
    p = await _setup(client, families["a"])
    cid = await _ask(p, "medication", "Hello Tess. Have you taken your blue tablet yet?")
    out = await cycle.record_answer(p["jp"], p["device"], cid, words, now=NOW + dt.timedelta(minutes=2))
    assert out["said"] != "yes" and out["structured"]["medication_taken"] != "yes", words
    assert "had taken" not in out["answer_summary"]
    assert [e for e in await _events(p) if e["status"] == "taken"] == []


async def test_a_clear_yes_writes_exactly_the_dose_that_was_asked_about(client, families):
    p = await _setup(client, families["a"])
    cid = await _ask(p, "medication", "Hello Tess. Have you taken your blue tablet yet?", NOW + dt.timedelta(minutes=10))
    out = await cycle.record_answer(p["jp"], p["device"], cid, "Yes I have", now=NOW + dt.timedelta(minutes=12))
    assert out["said"] == "yes" and out["structured"]["medication_taken"] == "yes"
    ev = await _events(p)
    assert len(ev) == 1 and ev[0]["status"] == "taken" and ev[0]["due_at"] == NOW and ev[0]["confirmed_via"] == "checkin"
    # The same answer delivered again is not a second confirmation of anything.
    assert await cycle.record_answer(p["jp"], p["device"], cid, "Yes I have", now=NOW + dt.timedelta(minutes=13)) is None
    # A later "not today" for the same dose does not silently overwrite it.
    cid2 = await _ask(p, "medication", "Hello Tess. Have you taken your blue tablet yet?", NOW + dt.timedelta(minutes=20))
    await cycle.record_answer(p["jp"], p["device"], cid2, "Not today", now=NOW + dt.timedelta(minutes=21))
    ev = await _events(p)
    assert len(ev) == 1 and ev[0]["status"] == "taken"


async def test_no_dose_is_written_when_the_medicine_or_the_time_is_ambiguous(client, families):
    p = await _setup(client, families["a"], meds=(("Aspirin", ["08:00"]), ("Aspirin 75mg", ["08:00"]),
                                                  ("Metformin", ["08:00", "08:30"])))
    # Two names fit the question.
    cid = await _ask(p, "medication", "Hello Tess. Have you taken your aspirin 75mg yet?")
    await cycle.record_answer(p["jp"], p["device"], cid, "Yes I have", now=NOW + dt.timedelta(minutes=1))
    # Two scheduled times fit the moment it was asked.
    cid = await _ask(p, "medication", "Hello Tess. Have you taken your metformin yet?", NOW + dt.timedelta(minutes=15))
    await cycle.record_answer(p["jp"], p["device"], cid, "Yes I have", now=NOW + dt.timedelta(minutes=16))
    # The question named no medicine, and there are several.
    cid = await _ask(p, "medication", "Hello Tess. Have you taken your tablets yet?")
    await cycle.record_answer(p["jp"], p["device"], cid, "Yes I have", now=NOW + dt.timedelta(minutes=2))
    assert await _events(p) == []


async def test_a_late_answer_is_not_bound_to_a_dose(client, families):
    p = await _setup(client, families["a"])
    cid = await _ask(p, "medication", "Hello Tess. Have you taken your blue tablet yet?")
    await cycle.record_answer(p["jp"], p["device"], cid, "Yes I have", now=NOW + dt.timedelta(hours=3))
    assert await _events(p) == []


async def test_a_medication_reminder_is_not_confirmed_by_intention_or_a_bare_okay(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{p['id']}/reminders", json={
        "kind": "medication", "medication_id": p["meds"][0]["id"], "text": "Blue tablet", "due_at": NOW.isoformat()})
    assert r.status_code == 201, r.text
    from care import personal
    async with workspace_connection(p["jp"]) as conn:
        person = dict(await conn.fetchrow("SELECT * FROM care_people WHERE id = $1", uuid.UUID(p["id"])))
        await personal.due_to_checkins(conn, person, None, NOW)
        cid = await conn.fetchval("SELECT id FROM care_checkins WHERE reminder_id IS NOT NULL ORDER BY scheduled_at DESC LIMIT 1")
    out = await cycle.record_answer(p["jp"], p["device"], cid, "I will take it", now=NOW + dt.timedelta(minutes=1))
    assert out["reminder"]["status"] == "snoozed"
    out_first = out
    assert out_first["said"] != "yes"
    assert await _events(p) == []


# ── A3: who recorded a dose, and no silent overwrite ─────────────────────────

async def test_who_recorded_a_dose_comes_from_the_caller_and_a_settled_dose_is_not_overwritten(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    client.act_as(fam, "member", "member")
    body = {"medication_id": p["meds"][0]["id"], "due_at": NOW.isoformat(), "status": "taken", "confirmed_via": "device"}
    r = await client.post(f"{C}/people/{p['id']}/medication-events", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["confirmed_via"] == "family", "a daughter's account cannot claim a device observed the dose"
    # The same record again is fine; a different one for the same dose is refused, not written over.
    assert (await client.post(f"{C}/people/{p['id']}/medication-events", json=body)).status_code == 201
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{p['id']}/medication-events", json=dict(body, status="missed"))
    assert r.status_code == 409, r.text
    ev = await _events(p)
    assert len(ev) == 1 and ev[0]["status"] == "taken" and ev[0]["confirmed_via"] == "family"


# ── A6: only the person talks as the person ──────────────────────────────────

async def test_only_the_wearer_may_talk_as_the_person(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    other = await _setup(client, fam)          # another wearer in the same workspace
    await add_wearer(client, fam, p["id"])
    pid = uuid.UUID(p["id"])

    async def _count():
        async with workspace_connection(p["jp"]) as conn:
            return (await conn.fetchval("SELECT count(*) FROM care_transcripts WHERE person_id = $1", pid),
                    await conn.fetchval("SELECT count(*) FROM care_conversation_memory WHERE person_id = $1", pid),
                    await conn.fetchval("SELECT count(*) FROM care_reminders WHERE person_id = $1", pid))

    before = await _count()
    # The daughter (a family member), a carer, a workspace admin outside the circle: refused, nothing written.
    client.act_as(fam, "member", "member")
    assert (await client.post(f"{C}/people/{p['id']}/talk", json={"text": "remind me to ring Sravani at three"})).status_code == 403
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{p['id']}/circle", json={"display_name": "Carer", "role": "carer", "user_id": str(fam["viewer"])})
    assert r.status_code == 201, r.text
    client.act_as(fam, "viewer", "member")
    assert (await client.post(f"{C}/people/{p['id']}/talk", json={"text": "hello"})).status_code == 403
    client.act_as(fam, "admin", "workspace_admin")
    assert (await client.post(f"{C}/people/{p['id']}/talk", json={"text": "hello"})).status_code == 403
    # Another tenant: not even found.
    client.act_as(families["b"], "admin", "workspace_admin")
    assert (await client.post(f"{C}/people/{p['id']}/talk", json={"text": "hello"})).status_code == 404
    assert await _count() == before
    # The person themself may; but not as somebody else in the same workspace.
    client.act_as(fam, "wearer", "member")
    assert (await client.post(f"{C}/people/{p['id']}/talk", json={"text": "hello"})).status_code == 200
    # Another wearer's person: not in this account's circle, so not found (D-076).
    assert (await client.post(f"{C}/people/{other['id']}/talk", json={"text": "hello"})).status_code == 404
    # A revoked membership stops at once: out of the circle, the person is not even found (D-076).
    async with workspace_connection(p["jp"]) as conn:
        await conn.execute("UPDATE care_circle_members SET active = FALSE WHERE person_id = $1 AND user_id = $2", pid, fam["wearer"])
    assert (await client.post(f"{C}/people/{p['id']}/talk", json={"text": "hello"})).status_code == 404


# ── A3d/A3e (migration 112): bound when asked, attributed, corrected in the open ──

async def _ask_bound(p, med, due, text="Hello Tess. Have you taken your tablets yet?", asked_at=NOW) -> uuid.UUID:
    async with workspace_connection(p["jp"]) as conn:
        return await conn.fetchval(
            "INSERT INTO care_checkins (person_id, scheduled_at, asked_at, status, question_kind, question_text, "
            "medication_id, dose_due_at) VALUES ($1, $2, $2, 'asked', 'medication', $3, $4, $5) RETURNING id",
            uuid.UUID(p["id"]), asked_at, text, uuid.UUID(med["id"]), due)


async def test_a_bound_question_writes_exactly_its_own_dose_even_among_similar_names(client, families):
    p = await _setup(client, families["a"], meds=(("Aspirin", ["08:00"]), ("Aspirin 75mg", ["08:00", "08:30"])))
    second = p["meds"][1]
    # Words that name no single medicine, two doses close together: the binding decides, not the words.
    cid = await _ask_bound(p, second, NOW + dt.timedelta(minutes=30), asked_at=NOW + dt.timedelta(minutes=25))
    out = await cycle.record_answer(p["jp"], p["device"], cid, "Yes I have", now=NOW + dt.timedelta(minutes=27))
    assert out["said"] == "yes"
    ev = await _events(p)
    assert len(ev) == 1 and str(ev[0]["medication_id"]) == second["id"] and ev[0]["due_at"] == NOW + dt.timedelta(minutes=30)
    assert ev[0]["source"] == "self_report" and ev[0]["recorded_by"] is None


async def test_a_bound_answer_that_comes_too_late_or_after_the_medicine_was_stopped_writes_nothing(client, families):
    p = await _setup(client, families["a"])
    med = p["meds"][0]
    cid = await _ask_bound(p, med, NOW)
    await cycle.record_answer(p["jp"], p["device"], cid, "Yes I have", now=NOW + dt.timedelta(hours=3))
    cid = await _ask_bound(p, med, NOW + dt.timedelta(days=1), asked_at=NOW + dt.timedelta(days=1))
    async with workspace_connection(p["jp"]) as conn:
        await conn.execute("UPDATE care_medications SET active = FALSE WHERE id = $1", uuid.UUID(med["id"]))
    await cycle.record_answer(p["jp"], p["device"], cid, "Yes I have", now=NOW + dt.timedelta(days=1, minutes=1))
    assert await _events(p) == []


async def test_the_scheduler_binds_the_dose_when_it_asks(client, families):
    p = await _setup(client, families["a"])
    import random
    await cycle.run_workspace(p["jp"], NOW - dt.timedelta(minutes=20), rng=random.Random(3))
    due = await cycle.due_checkin(p["jp"], p["device"], NOW, rng=random.Random(3))
    assert due is not None and due["question_kind"] == "medication"
    async with workspace_connection(p["jp"]) as conn:
        row = await conn.fetchrow("SELECT medication_id, dose_due_at FROM care_checkins WHERE id = $1", due["id"])
    assert str(row["medication_id"]) == p["meds"][0]["id"] and row["dose_due_at"] == NOW


async def test_who_recorded_it_and_whether_it_was_observed_come_from_the_caller(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{p['id']}/circle", json={"display_name": "Carer", "role": "carer", "user_id": str(fam["viewer"])})
    assert r.status_code == 201, r.text
    med = p["meds"][0]["id"]
    # A daughter cannot claim to have observed it as a carer; a carer in the circle can.
    client.act_as(fam, "member", "member")
    r = await client.post(f"{C}/people/{p['id']}/medication-events", json={
        "medication_id": med, "due_at": NOW.isoformat(), "status": "taken", "observed": True})
    assert r.status_code == 201 and r.json()["source"] == "family_report" and r.json()["recorded_by"] == str(fam["member"])
    client.act_as(fam, "viewer", "member")
    r = await client.post(f"{C}/people/{p['id']}/medication-events", json={
        "medication_id": med, "due_at": (NOW + dt.timedelta(days=1)).isoformat(), "status": "taken", "observed": True})
    assert r.status_code == 201 and r.json()["source"] == "carer_observed"


async def test_a_correction_needs_a_reason_and_keeps_the_old_record(client, families, pool):
    fam = families["a"]
    p = await _setup(client, fam)
    med = p["meds"][0]["id"]
    client.act_as(fam, "member", "member")
    body = {"medication_id": med, "due_at": NOW.isoformat(), "status": "taken"}
    assert (await client.post(f"{C}/people/{p['id']}/medication-events", json=body)).status_code == 201
    client.act_as(fam, "admin", "workspace_admin")
    assert (await client.post(f"{C}/people/{p['id']}/medication-events", json=dict(body, status="missed"))).status_code == 409
    r = await client.post(f"{C}/people/{p['id']}/medication-events",
                          json=dict(body, status="missed", correction_reason="Tablet found on the floor at lunch."))
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "missed" and r.json()["source"] == "carer_report"
    r = await client.get(f"{C}/people/{p['id']}/medication-corrections")
    assert r.status_code == 200
    c = r.json()["corrections"]
    assert len(c) == 1 and (c[0]["previous_status"], c[0]["previous_source"], c[0]["new_status"]) == ("taken", "family_report", "missed")
    assert c[0]["reason"] == "Tablet found on the floor at lunch." and c[0]["corrected_by"] == str(fam["admin"])
    # The history is append-only to the application role.
    for sql in ("UPDATE care_medication_event_corrections SET reason = 'changed'",
                "DELETE FROM care_medication_event_corrections"):
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            async with workspace_connection(p["jp"]) as conn:
                await conn.execute(sql)
    # Another tenant sees none of it.
    client.act_as(families["b"], "admin", "workspace_admin")
    assert (await client.get(f"{C}/people/{p['id']}/medication-corrections")).status_code == 404
