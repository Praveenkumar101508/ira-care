"""Outage and recovery (remediation C-24) against a real PostgreSQL.

When the database cannot be reached:
* the API says "try again shortly" (503, retryable, Retry-After), never a bare 500 and never
  the error's text, so the watch keeps an unsent Help request instead of dropping it;
* the monitoring and check-in passes do not crash, send nothing, and write no heartbeat, so
  the "monitoring paused" signal stays on until a pass really succeeds;
* when the database is back, the next pass escalates the alert that was waiting, once.

The model being unavailable and a notification provider failing are covered elsewhere:
`test_care_conversation_db` (the template and the rules answer when the model is broken) and
`test_care_delivery_db` (failed sends are retried, then recorded failed).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import os
import sys
from pathlib import Path

import pytest

asyncpg = pytest.importorskip("asyncpg")
pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy._care_fixtures import client, families, pool  # noqa: E402,F401

from care import checkin_cycle, escalation, providers  # noqa: E402
from care import repository as repo  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("simulate_patient", ROOT / "scripts" / "simulate_patient.py")
sim = importlib.util.module_from_spec(_spec)
sys.modules["simulate_patient"] = sim
_spec.loader.exec_module(sim)

C = "/api/v1/care"
UTC = dt.timezone.utc


class _DownPool:
    """A pool for a database that is not there."""

    def acquire(self, *a, **k):
        raise ConnectionRefusedError(111, "Connect call failed")

    def get_max_size(self):
        return 10

    def get_idle_size(self):
        return 0


@contextlib.contextmanager
def database_down(monkeypatch):
    import utils.db as db

    @contextlib.asynccontextmanager
    async def refused():
        raise ConnectionRefusedError(111, "Connect call failed ('127.0.0.1', 5432)")
        yield  # pragma: no cover

    with monkeypatch.context() as m:
        m.setattr(db, "acquire", refused)
        m.setattr(db, "get_pool", lambda: _DownPool())
        yield


@pytest.fixture
async def family(pool):
    fam = await sim.create_family(ADMIN_DSN, pool, label="out")
    try:
        await sim.setup_person(fam)
        yield fam
    finally:
        await sim.remove_family(ADMIN_DSN, fam)


async def test_the_api_says_try_again_when_the_database_is_down(client, families, monkeypatch):
    fam = families["a"]
    client.act_as(fam, "admin", "workspace_admin")
    person = (await client.post(f"{C}/people", json={"display_name": "Out Age", "timezone": "UTC"})).json()
    code = (await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})).json()["pairing_code"]
    token = (await client.post(f"{C}/devices/pair", json={"pairing_code": code})).json()["device_token"]
    with database_down(monkeypatch):
        r = await client.get(f"{C}/people")
        assert r.status_code == 503, r.text
        body = r.json()
        assert body["error_code"] == "dependency_unavailable" and body["retryable"] is True
        assert r.headers.get("Retry-After") == "5"
        assert "5432" not in r.text and "Connect call" not in r.text, "no error detail reaches the client"
        # A real watch's Help: the server cannot take it now and says so in a way the watch
        # retries. Never 401: on a 401 the watch unpairs itself.
        r = await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers={"X-Device-Token": token})
        assert r.status_code == 503 and r.json()["retryable"] is True
    # Back: the same requests work, and the Help is taken.
    assert (await client.get(f"{C}/people")).status_code == 200
    r = await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers={"X-Device-Token": token})
    assert r.status_code == 201, r.text


async def test_the_jobs_survive_the_outage_and_escalate_what_waited_once_it_is_back(family, monkeypatch):
    jp = sim._device_principal(family)
    alert = await repo.record_help_request(jp, family.device, {"via": "button"})
    sent: list[str] = []

    class Counting(providers.LogOnlyProvider):
        async def send_sms(self, to, text, *, status_callback=None):
            sent.append(to)
            return providers.Delivery("accepted", "", f"SM{len(sent)}")

        async def call(self, to, text, *, status_callback=None):
            sent.append(to)
            return providers.Delivery("accepted", "", f"CA{len(sent)}")

    monkeypatch.setattr(providers, "provider_from_env", lambda env=None: Counting())
    with database_down(monkeypatch):
        out = await escalation.run_once()
        assert out.errors >= 1 and out.tiers_run == 0
        cyc = await checkin_cycle.run_cycle_once()
        assert cyc.errors >= 1
    assert sent == [], "nothing was sent while the database was down"
    async with workspace_connection(jp) as conn:
        row = await conn.fetchrow("SELECT status, current_tier FROM care_alerts WHERE id = $1", alert["id"])
    assert row["status"] == "open" and row["current_tier"] == 0, "still waiting, not lost"

    # The database is back: the next pass escalates it, and the one after does not repeat it.
    await escalation.run_once()
    first = len(sent)
    assert first > 0
    async with workspace_connection(jp) as conn:
        row = await conn.fetchrow("SELECT status, current_tier FROM care_alerts WHERE id = $1", alert["id"])
    assert row["status"] == "escalating" and row["current_tier"] == 1
    await escalation.run_once()
    assert len(sent) == first, "tier 1 told once"


async def test_a_pass_that_never_reached_the_database_writes_no_heartbeat(family, monkeypatch):
    async def last_beat(job):
        conn = await asyncpg.connect(ADMIN_DSN)
        try:
            return await conn.fetchval("SELECT max(finished_at) FROM job_runs WHERE job = $1", job)
        finally:
            await conn.close()

    before = await last_beat("care_monitoring")
    with database_down(monkeypatch):
        await escalation.run_once()
    assert await last_beat("care_monitoring") == before, "a pass that did nothing must not look healthy"
    await escalation.run_once()
    after = await last_beat("care_monitoring")
    assert after is not None and (before is None or after > before), "a pass that worked does"
