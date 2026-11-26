"""IRA CARE round 2, phase 3: suspected falls against a real PostgreSQL.

The watch starts a countdown and tells the server; the server keeps its own
deadline. "I'm OK" resolves, Help or silence opens the SOS with location, a
carer can turn detection off per person, and the watch learns the settings.
"""
from __future__ import annotations

import datetime as dt
import os

import pytest

asyncpg = pytest.importorskip("asyncpg")
pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy._care_fixtures import client, families, pool  # noqa: E402,F401

from care import checkin_cycle as cycle  # noqa: E402
from care import escalation, providers  # noqa: E402

C = "/api/v1/care"
UTC = dt.timezone.utc


async def _setup(client, fam) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people", json={"display_name": "Test Person", "preferred_name": "Tess", "timezone": "UTC",
                                                "home_address": "1 Example Street", "home_latitude": 51.5, "home_longitude": -0.12})
    person = r.json()
    await client.post(f"{C}/people/{person['id']}/circle", json={"display_name": "Family Member", "role": "family", "tier": 1,
                      "user_id": str(fam["member"]), "phone": "+440000000001", "sms_enabled": True})
    r = await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})
    paired = (await client.post(f"{C}/devices/pair", json={"pairing_code": r.json()["pairing_code"]})).json()
    person["h"] = {"X-Device-Token": paired["device_token"]}
    person["jp"] = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    return person


async def test_the_watch_learns_the_fall_settings_and_a_carer_can_turn_them_off(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    me = (await client.get(f"{C}/devices/me", headers=p["h"])).json()
    assert me["fall"] == {"fall_detection_enabled": True, "fall_countdown_s": 30, "fall_impact_g": 2.5,
                          "fall_still_seconds": 20, "fall_still_threshold_g": 0.3}
    s = (await client.get(f"{C}/people/{p['id']}/settings")).json()
    body = {k: v for k, v in s.items() if k not in ("person_id", "tenant_id", "workspace_id", "set_by", "updated_at")}
    body.update(fall_detection_enabled=False, fall_countdown_s=45, fall_impact_g=3.0)
    r = await client.put(f"{C}/people/{p['id']}/settings", json=body)
    assert r.status_code == 200, r.text
    me = (await client.get(f"{C}/devices/me", headers=p["h"])).json()
    assert me["fall"]["fall_detection_enabled"] is False and me["fall"]["fall_countdown_s"] == 45
    # Off means a start is refused, so a stale watch cannot open fall alerts.
    r = await client.post(f"{C}/devices/me/fall", json={"peak_g": 3.4, "still_seconds": 20}, headers=p["h"])
    assert r.status_code == 409 and "turned off" in r.text
    # Out-of-range thresholds are refused.
    body["fall_detection_enabled"] = True
    body["fall_countdown_s"] = 5
    assert (await client.put(f"{C}/people/{p['id']}/settings", json=body)).status_code == 422


async def test_a_fall_countdown_asks_first_and_im_ok_resolves_it(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    r = await client.post(f"{C}/devices/me/fall", json={"peak_g": 3.4, "still_seconds": 20}, headers=p["h"])
    assert r.status_code == 201, r.text
    alert = r.json()
    assert alert["kind"] == "suspected_fall" and alert["status"] == "asking" and alert["checkin_id"] is None
    assert "hard impact (3.4 g)" in alert["details"]["summary"] and alert["details"]["countdown_s"] == 30
    assert alert["details"]["ask_first_wait_s"] == 40, "the server waits the countdown plus a grace"
    # A second start while one is asking is attached to it, never dropped, and opens nothing new;
    # the deadline stands (D-092; until then it was refused with 409 and the watch let it go).
    r = await client.post(f"{C}/devices/me/fall", json={"peak_g": 5.0}, headers=p["h"])
    assert r.status_code == 201 and r.json()["id"] == alert["id"] and r.json()["attached"] is True
    assert r.json()["next_action_at"] == alert["next_action_at"] and len(r.json()["details"]["detections"]) == 2
    # The watch does not get a safety question to ask: it is asking already.
    assert (await client.get(f"{C}/devices/me/checkins/due", headers=p["h"])).json()["checkin"] is None
    r = await client.post(f"{C}/devices/me/fall/ok", headers=p["h"])
    assert r.status_code == 200 and r.json()["status"] == "resolved" and r.json()["details"]["outcome"] == "answered_ok"
    assert (await client.post(f"{C}/devices/me/fall/ok", headers=p["h"])).status_code == 404
    client.act_as(fam, "member", "member")
    alerts = (await client.get(f"{C}/alerts", params={"open_only": "false"})).json()["alerts"]
    assert len(alerts) == 1 and "fall check: OK" in alerts[0]["resolution"]


async def test_help_or_silence_during_the_countdown_opens_the_sos_with_location(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    # Help pressed during the countdown, with the one-off location.
    await client.post(f"{C}/devices/me/fall", json={"peak_g": 3.1, "still_seconds": 20}, headers=p["h"])
    r = await client.post(f"{C}/devices/me/help", json={"via": "fall", "location": {"latitude": 51.5001, "longitude": -0.1201, "accuracy_m": 9}}, headers=p["h"])
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["kind"] == "suspected_fall" and a["status"] == "open" and a["at_home"] is True and a["details"]["outcome"] == "no_answer"
    provider = providers.LogOnlyProvider()
    out = await escalation.run_workspace(p["jp"], provider=provider)
    assert out.tiers_run == 1
    texts = [t for ch, _to, t in provider.log if ch == "sms"]
    assert "hard impact" in texts[0] and "AT HOME" in texts[0] and "1 Example Street" in texts[0]
    client.act_as(fam, "member", "member")
    await client.post(f"{C}/alerts/{a['id']}/resolve", json={"resolution": "Checked; all well.", "outcome": "all_well"})
    # Silence: the server's own deadline promotes the asking alert.
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/devices/me/fall", json={"peak_g": 4.0, "still_seconds": 20}, headers=p["h"])
    assert r.status_code == 201
    now = dt.datetime.now(UTC)
    before = await escalation.run_workspace(p["jp"], now + dt.timedelta(seconds=20), provider=provider)
    assert before.promoted == 0
    after = await escalation.run_workspace(p["jp"], now + dt.timedelta(seconds=45), provider=provider)
    assert after.promoted == 1 and after.tiers_run == 1
    client.act_as(fam, "member", "member")
    open_alerts = (await client.get(f"{C}/alerts")).json()["alerts"]
    assert open_alerts[0]["kind"] == "suspected_fall" and open_alerts[0]["status"] == "escalating"
    assert open_alerts[0]["location_latitude"] is None, "no location was ever sent: unknown, not guessed"


async def test_a_watch_that_was_offline_at_the_countdown_still_opens_a_fall_alert(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    r = await client.post(f"{C}/devices/me/help", json={"via": "fall"}, headers=p["h"])
    assert r.status_code == 201
    a = r.json()
    assert a["kind"] == "suspected_fall" and a["status"] == "open" and "Nobody answered" in a["details"]["summary"]
