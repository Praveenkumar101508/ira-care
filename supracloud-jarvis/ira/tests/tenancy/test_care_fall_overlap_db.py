"""D-092: a fall detected while a fall countdown is running is never lost on the server.

On WEDA-FALL, 20 of 24 missed falls were detected while an earlier alarm's countdown ran; the
watch dropped the detection and, had it sent one, the server refused a second start with 409
(which the watch's outbox treats as final). Now every event carries the watch's incident id:

  * a later detection is attached to the fall alert already held, and its deadline stands;
  * "I'm OK" resolves the alert only for the latest incident attached to it;
  * Help, or the end of a countdown, while a fall SOS is already open opens no second SOS;
  * concurrent events open at most one fall SOS.

Every request here is synthetic; no dataset is used. Against the code before the fix the first
four tests fail (see the report of the branch fix/fall-countdown-overlap).
"""
from __future__ import annotations

import asyncio
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
from tests.tenancy.test_care_fall_db import _setup  # noqa: E402

from care import escalation, providers  # noqa: E402

C = "/api/v1/care"
UTC = dt.timezone.utc
KEY = "X-Idempotency-Key"


def _h(p: dict, key: str) -> dict:
    return {**p["h"], KEY: key}


async def _start(client, p, incident: str, detection: int, peak: float = 3.0, key: str | None = None):
    return await client.post(f"{C}/devices/me/fall", json={"peak_g": peak, "still_seconds": 0, "incident_id": incident,
                                                            "detection": detection},
                             headers=_h(p, key or f"fall-{incident}-d{detection}"))


async def _ok(client, p, incident: str):
    return await client.post(f"{C}/devices/me/fall/ok", json={"incident_id": incident}, headers=_h(p, f"fall-{incident}-ok"))


async def _help(client, p, incident: str, via: str = "fall"):
    return await client.post(f"{C}/devices/me/help", json={"via": via, "incident_id": incident},
                             headers=_h(p, f"fall-{incident}-help"))


async def _fall_alerts(client, fam) -> list[dict]:
    client.act_as(fam, "member", "member")
    alerts = (await client.get(f"{C}/alerts", params={"open_only": "false"})).json()["alerts"]
    client.act_as(fam, "admin", "workspace_admin")
    return [a for a in alerts if a["kind"] == "suspected_fall"]


def _unresolved(alerts: list[dict]) -> list[dict]:
    return [a for a in alerts if a["status"] in ("asking", "open", "escalating", "acknowledged")]


async def test_a_later_detection_is_attached_to_the_same_alert_and_its_deadline_stands(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    a = str(uuid.uuid4())
    r = await _start(client, p, a, 1, peak=2.3)
    assert r.status_code == 201, r.text
    first = r.json()
    # The real fall, three seconds later, while the countdown runs.
    r = await _start(client, p, a, 2, peak=4.8)
    assert r.status_code in (200, 201), r.text
    second = r.json()
    assert second["id"] == first["id"] and second["attached"] is True
    assert second["status"] == "asking" and second["next_action_at"] == first["next_action_at"], "the deadline never moves"
    assert [d["detection"] for d in second["details"]["detections"]] == [1, 2]
    assert second["details"]["detections"][1]["peak_g"] == 4.8
    assert len(await _fall_alerts(client, fam)) == 1, "no second alert"
    # Nobody answers: the ORIGINAL deadline opens the one SOS.
    provider = providers.LogOnlyProvider()
    now = dt.datetime.now(UTC)
    assert (await escalation.run_workspace(p["jp"], now + dt.timedelta(seconds=20), provider=provider)).promoted == 0
    out = await escalation.run_workspace(p["jp"], now + dt.timedelta(seconds=45), provider=provider)
    assert out.promoted == 1
    alerts = await _fall_alerts(client, fam)
    assert len(alerts) == 1 and alerts[0]["status"] == "escalating"


async def test_im_ok_for_the_first_prompt_then_the_second_fall_gets_its_own_check(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    assert (await _start(client, p, a, 1, peak=2.3)).status_code == 201
    assert (await _start(client, p, a, 2, peak=4.8)).status_code in (200, 201)
    r = await _ok(client, p, a)
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    # The watch asks again about the later detection: a new incident, a new asking alert.
    r = await _start(client, p, b, 1, peak=4.8)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "asking" and r.json()["details"]["incident_id"] == b
    # No answer to the second question: the SOS opens for the second fall.
    now = dt.datetime.now(UTC)
    out = await escalation.run_workspace(p["jp"], now + dt.timedelta(seconds=45), provider=providers.LogOnlyProvider())
    assert out.promoted == 1
    unresolved = _unresolved(await _fall_alerts(client, fam))
    assert len(unresolved) == 1 and unresolved[0]["details"]["incident_id"] == b


async def test_an_ok_that_arrives_after_a_newer_detection_never_resolves_the_alert(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    first = (await _start(client, p, a, 1)).json()
    # Out of order (a watch that lost its countdown in a restart, say): a new incident arrives first.
    r = await _start(client, p, b, 1, peak=4.8)
    assert r.status_code in (200, 201) and r.json()["id"] == first["id"], r.text
    r = await _ok(client, p, a)
    assert r.status_code == 409, "an OK for the older incident does not cover the newer detection"
    alerts = await _fall_alerts(client, fam)
    assert len(alerts) == 1 and alerts[0]["status"] == "asking"
    assert alerts[0]["next_action_at"] == first["next_action_at"]
    r = await _ok(client, p, b)
    assert r.status_code == 200 and r.json()["status"] == "resolved"


async def test_a_later_fall_while_the_fall_sos_is_open_opens_no_second_sos(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    await _start(client, p, a, 1)
    opened = (await _help(client, p, a)).json()
    assert opened["status"] == "open"
    # The watch is idle again and detects another fall: it starts a new countdown.
    r = await _start(client, p, b, 1, peak=5.0)
    assert r.status_code in (200, 201) and r.json()["id"] == opened["id"] and r.json()["attached"] is True, r.text
    # That countdown ends with no answer: Help for it is the same SOS, not a second one.
    r = await _help(client, p, b)
    assert r.status_code == 201 and r.json()["id"] == opened["id"], r.text
    assert len(_unresolved(await _fall_alerts(client, fam))) == 1
    # An OK for the later incident never closes an open SOS.
    assert (await _ok(client, p, b)).status_code == 404
    assert len(_unresolved(await _fall_alerts(client, fam))) == 1


async def test_concurrent_events_for_falls_open_at_most_one_sos(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    calls = [_start(client, p, a, n) for n in (1, 2, 3, 4)]
    calls += [_start(client, p, a, 1, key=f"fall-{a}-d1")]       # a retry of the same key
    calls += [_start(client, p, b, 1)]                            # another incident
    calls += [_help(client, p, a), _help(client, p, b)]
    responses = await asyncio.gather(*calls)
    assert all(r.status_code in (200, 201) for r in responses), [r.text for r in responses]
    unresolved = _unresolved(await _fall_alerts(client, fam))
    assert len(unresolved) == 1, unresolved
    assert unresolved[0]["status"] in ("open", "escalating")


async def test_a_repeat_is_answered_once_and_a_detection_is_recorded_once(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    a = str(uuid.uuid4())
    first = await _start(client, p, a, 1)
    again = await _start(client, p, a, 1)
    assert again.headers.get("X-Idempotent-Replay") == "true" and again.json()["id"] == first.json()["id"]
    # The same detection under another key (the outbox lost its line and wrote it again) is not counted twice.
    r = await _start(client, p, a, 1, key="another-key-0001")
    assert [d["detection"] for d in r.json()["details"]["detections"]] == [1]
    assert len(await _fall_alerts(client, fam)) == 1
