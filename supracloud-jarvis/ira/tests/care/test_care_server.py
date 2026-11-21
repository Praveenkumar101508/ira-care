"""The deployable care server (care/server.py): the care routes, login and
workspaces, the two health routes, and no documentation pages."""
from __future__ import annotations

import os


from tests.care.test_care_routes_registered import route_paths


def _paths(app):
    return set(route_paths(app))


def test_the_care_server_mounts_what_a_care_deployment_needs(monkeypatch):
    monkeypatch.setenv("IRA_CARE_ALLOWED_ORIGINS", "")
    from care.server import create_care_app
    paths = _paths(create_care_app())
    assert {"/health", "/health/jobs", "/auth/cloud/login", "/auth/token", "/api/v1/workspaces",
            "/api/v1/care/people", "/api/v1/care/devices/me/report", "/api/v1/care/alerts"} <= paths
    assert "/docs" not in paths and "/openapi.json" not in paths
    assert not any("/chat" in p or "/voice" in p for p in paths), "the full server's surfaces are not here"


def test_browser_origins_come_from_the_setting(monkeypatch):
    from care import server
    monkeypatch.setenv("IRA_CARE_ALLOWED_ORIGINS", " https://care.example.invalid, http://localhost:3000 ")
    assert server.allowed_origins() == ["https://care.example.invalid", "http://localhost:3000"]
    monkeypatch.delenv("IRA_CARE_ALLOWED_ORIGINS", raising=False)
    assert server.allowed_origins() == []
    monkeypatch.setenv("IRA_CARE_JOBS_IN_API", "true")
    assert server.jobs_in_api() is True
    monkeypatch.delenv("IRA_CARE_JOBS_IN_API", raising=False)
    assert server.jobs_in_api() is False
    assert os.environ.get("IRA_CARE_JOBS_IN_API") is None
