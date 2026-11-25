"""The research heart-rate routes (D-086): care managers only, workspace-bound, nothing stored.

`GET /care/research/heart/samples` and `POST /care/research/heart/run` over the care app's
real HTTP stack. Only `require_workspace_principal` is replaced, by a principal of the role
under test; the database is made to fail if anything opens a connection, so a route that
started writing would fail here before it reached one.
"""
from __future__ import annotations

import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

pytest.importorskip("slowapi")

from fastapi.testclient import TestClient  # noqa: E402

from api.middleware.workspace import require_workspace_principal  # noqa: E402
from app.middleware import limiter  # noqa: E402
from care import heart_research as hr  # noqa: E402
from care import routes  # noqa: E402
from tests.conftest import make_workspace_principal  # noqa: E402

R = "/api/v1/care/research/heart"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("IRA_CARE_ALLOWED_ORIGINS", "")
    limiter.reset()

    def no_database(*a, **k):
        raise AssertionError("the research route must not open a database connection")

    monkeypatch.setattr(routes, "workspace_connection", no_database)
    from care.server import create_care_app
    app = create_care_app()
    c = TestClient(app)

    def act_as(role, is_owner=False):
        app.dependency_overrides[require_workspace_principal] = lambda: make_workspace_principal(role=role, is_owner=is_owner)

    c.act_as = act_as
    yield c
    limiter.reset()


def _upload(**kw):
    return {"recording": hr.simulate(kw.pop("bpm", 72.0), kw.pop("seconds", 20.0), **kw)}


def test_a_care_manager_lists_the_samples_and_runs_one(client):
    client.act_as("workspace_admin")
    r = client.get(f"{R}/samples")
    assert r.status_code == 200
    names = {s["name"] for s in r.json()["samples"]}
    assert {"ppg_dalia_s1_excerpt", "simulated_rest", "simulated_arm_swing", "simulated_faults"} <= names
    r = client.post(f"{R}/run", json={"sample": "simulated_arm_swing"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["kind"], d["source"], d["is_measurement"], d["may_alert"], d["stored"]) == (
        "heart_research_result", "simulated", False, False, False)
    assert d["provenance"]["learned_model"]["sha256"] and d["summary"]["windows"] == 27


def test_the_deployment_owner_may_run_it_too(client):
    client.act_as(None, is_owner=True)
    assert client.post(f"{R}/run", json={"sample": "simulated_rest"}).status_code == 200


@pytest.mark.parametrize("role", ["member", "viewer", None])
def test_carers_viewers_and_no_role_are_refused(client, role):
    client.act_as(role)
    assert client.get(f"{R}/samples").status_code == 403
    assert client.post(f"{R}/run", json={"sample": "simulated_rest"}).status_code == 403


def test_without_a_signed_in_workspace_principal_nothing_runs(client):
    r = client.post(f"{R}/run", json={"sample": "simulated_rest"})     # no override: the real dependency
    assert r.status_code in (401, 403)


def test_an_uploaded_simulated_recording_is_processed(client):
    client.act_as("workspace_admin")
    r = client.post(f"{R}/run", json=_upload(bpm=66.0))
    assert r.status_code == 200, r.text
    windows = r.json()["windows"]
    assert len(windows) == 7 and all(w["status"] == "estimate" and abs(w["bpm"] - 66.0) <= 2.0 for w in windows)


def test_device_data_is_refused_at_the_door(client):
    client.act_as("workspace_admin")
    body = _upload()
    body["recording"]["source"] = "device"
    assert client.post(f"{R}/run", json=body).status_code == 422
    body["recording"]["source"] = "research_recording"                # negative control
    assert client.post(f"{R}/run", json=body).status_code == 200


@pytest.mark.parametrize("body", [
    {},
    {"sample": "simulated_rest", "recording": hr.simulate(72.0, 20.0)},
    {"sample": "../../etc/passwd"},
    {"sample": "simulated_rest", "person_id": "00000000-0000-0000-0000-000000000001"},
])
def test_malformed_requests_are_refused(client, body):
    client.act_as("workspace_admin")
    assert client.post(f"{R}/run", json=body).status_code == 422


def test_an_unknown_sample_is_not_found(client):
    client.act_as("workspace_admin")
    assert client.post(f"{R}/run", json={"sample": "no_such_sample"}).status_code == 404


def test_a_contract_breach_says_what_is_wrong(client):
    client.act_as("workspace_admin")
    body = _upload()
    body["recording"]["acc"]["t0_s"] = 4.0
    r = client.post(f"{R}/run", json=body)
    assert r.status_code == 422 and "timebase" in r.json()["detail"]


def test_runs_are_rationed(client):
    client.act_as("workspace_admin")
    budget = int(routes.RESEARCH_LIMIT.split("/")[0])
    codes = [client.post(f"{R}/run", json={"sample": "simulated_rest"}).status_code for _ in range(budget + 2)]
    assert codes[:budget] == [200] * budget and set(codes[budget:]) == {429}
