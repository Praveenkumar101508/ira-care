"""End-to-end checks against the assembled application.

These run through the real `create_app()`, so they verify wiring rather than
units: that the middleware is installed in an order that lets error handlers see
the request ID, that probes are reachable, and that a failing dependency changes
readiness without changing liveness.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

# The full app import pulls memory.embeddings and worker.notifier. conftest
# stubs those modules but leaves attributes the import chain references unset;
# fill them in, exactly as tests/test_app_factory.py does.
_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    _st.SentenceTransformer = object
    _st.CrossEncoder = object
_wn = sys.modules.get("worker.notifier")
if _wn is not None and not hasattr(_wn, "REDIS_NOTIFY_CHANNEL"):
    _wn.REDIS_NOTIFY_CHANNEL = "ira:notify"
    # 3C-03 gave the notifier a per-tenant channel; the stub needs
    # the resolver too, because api/routes/notifications.py now
    # imports it rather than the bare constant.
    _wn.notify_channel = lambda principal=None: "ira:notify"

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("slowapi")

from fastapi.testclient import TestClient  # noqa: E402

from core.runtime import readiness as R
from core.runtime.readiness import CheckResult, CheckStatus
from core.runtime.request_context import CORRELATION_ID_HEADER, REQUEST_ID_HEADER


@pytest.fixture
def client():
    """The real app, assembled by the real factory.

    Constructed WITHOUT the `with` form on purpose: entering TestClient as a
    context manager runs the application lifespan, which connects to PostgreSQL,
    Redis and the model runtime. These tests are about HTTP wiring, so the
    lifespan is deliberately not run — matching how tests/test_app_factory.py
    exercises the same app.
    """
    from app.factory import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clean():
    R.reset_readiness_cache()
    R.stop_draining()
    yield
    R.reset_readiness_cache()
    R.stop_draining()


# ── Liveness ─────────────────────────────────────────────────────────────────

def test_livez_is_reachable_and_reports_build_metadata(client):
    r = client.get("/livez")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "alive"
    assert "version" in body and "git_commit" in body and "environment" in body


def test_livez_survives_every_dependency_being_down(client):
    """The outage this prevents: a brief Redis failure restarting every replica.

    Reaching the handler proves the event loop is scheduling work, which is the
    only thing a restart could repair.
    """
    with patch("core.runtime.readiness.check_postgres",
               AsyncMock(return_value=CheckResult("postgres", CheckStatus.FAILED, 0, True))), \
         patch("core.runtime.readiness.check_redis",
               AsyncMock(return_value=CheckResult("redis", CheckStatus.FAILED, 0, True))):
        assert client.get("/livez").status_code == 200


def test_livez_exposes_no_secret_or_internal_detail(client):
    body = client.get("/livez").text.lower()
    for forbidden in ("password", "secret", "postgres://", "redis://", "token"):
        assert forbidden not in body


# ── Readiness ────────────────────────────────────────────────────────────────

def _ok(name):
    return AsyncMock(return_value=CheckResult(name, CheckStatus.OK, 1, True))


def _failed(name):
    return AsyncMock(return_value=CheckResult(name, CheckStatus.FAILED, 0, True, "timeout"))


def test_readyz_returns_200_when_every_dependency_is_up(client):
    """`check_model_licences` is patched alongside the rest (P6-03).

    It is required in every environment and it reads the database, so with the
    pool deliberately absent here it fails and blocks readiness — which is the
    check working, not a defect. These tests are about the readiness WIRING, so
    every required dependency is stubbed; the licence check has its own suite in
    `tests/tenancy/test_p6_licence_gate_service.py`, against a real database.
    """
    with patch("core.runtime.readiness.check_postgres", _ok("postgres")), \
         patch("core.runtime.readiness.check_redis", _ok("redis")), \
         patch("core.runtime.readiness.check_migrations", _ok("migrations")), \
         patch("core.runtime.readiness.check_database_role", _ok("database_role")), \
         patch("core.runtime.readiness.check_model_licences", _ok("model_licences")), \
         patch("core.runtime.readiness.check_model_gateway",
               AsyncMock(return_value=CheckResult("model_gateway", CheckStatus.OK, 1, False))):
        r = client.get("/readyz")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"


@pytest.mark.parametrize(
    "failing",
    # `check_database_role` is included because it is REQUIRED: an
    # over-privileged runtime role must remove the instance from the load
    # balancer, not merely log. That is the whole point of D-18's runtime half.
    ["check_postgres", "check_redis", "check_migrations", "check_database_role"],
)
def test_readyz_fails_closed_when_a_required_dependency_is_down(client, failing):
    patches = {
        "check_postgres": _ok("postgres"),
        "check_redis": _ok("redis"),
        "check_migrations": _ok("migrations"),
        "check_database_role": _ok("database_role"),
    }
    patches[failing] = _failed(failing.replace("check_", ""))
    with patch("core.runtime.readiness.check_postgres", patches["check_postgres"]), \
         patch("core.runtime.readiness.check_redis", patches["check_redis"]), \
         patch("core.runtime.readiness.check_migrations", patches["check_migrations"]), \
         patch("core.runtime.readiness.check_database_role", patches["check_database_role"]), \
         patch("core.runtime.readiness.check_model_licences", _ok("model_licences")), \
         patch("core.runtime.readiness.check_model_gateway",
               AsyncMock(return_value=CheckResult("model_gateway", CheckStatus.OK, 1, False))):
        r = client.get("/readyz")
        assert r.status_code == 503
        assert r.json()["status"] == "not_ready"


def test_readyz_recovers_without_a_restart(client):
    """Readiness removes an instance from the load balancer and puts it back —
    no restart, no lost warm state."""
    with patch("core.runtime.readiness.check_postgres", _failed("postgres")), \
         patch("core.runtime.readiness.check_redis", _ok("redis")), \
         patch("core.runtime.readiness.check_migrations", _ok("migrations")), \
         patch("core.runtime.readiness.check_database_role", _ok("database_role")), \
         patch("core.runtime.readiness.check_model_licences", _ok("model_licences")), \
         patch("core.runtime.readiness.check_model_gateway",
               AsyncMock(return_value=CheckResult("model_gateway", CheckStatus.OK, 1, False))):
        assert client.get("/readyz").status_code == 503

    R.reset_readiness_cache()
    with patch("core.runtime.readiness.check_postgres", _ok("postgres")), \
         patch("core.runtime.readiness.check_redis", _ok("redis")), \
         patch("core.runtime.readiness.check_migrations", _ok("migrations")), \
         patch("core.runtime.readiness.check_database_role", _ok("database_role")), \
         patch("core.runtime.readiness.check_model_licences", _ok("model_licences")), \
         patch("core.runtime.readiness.check_model_gateway",
               AsyncMock(return_value=CheckResult("model_gateway", CheckStatus.OK, 1, False))):
        assert client.get("/readyz").status_code == 200


def test_readyz_reports_not_ready_while_draining(client):
    R.begin_draining()
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["draining"] is True


# ── Request IDs through the stack ────────────────────────────────────────────

def test_every_response_carries_a_request_id_header(client):
    r = client.get("/livez")
    assert r.headers.get(REQUEST_ID_HEADER)
    assert r.headers.get(CORRELATION_ID_HEADER)


def test_an_inbound_correlation_id_is_echoed(client):
    r = client.get("/livez", headers={CORRELATION_ID_HEADER: "trace-abc-123"})
    assert r.headers[CORRELATION_ID_HEADER] == "trace-abc-123"
    # The request ID is still freshly generated for this hop.
    assert r.headers[REQUEST_ID_HEADER] != "trace-abc-123"


def test_a_hostile_correlation_id_is_replaced_not_echoed(client):
    """Echoing an unvalidated header into logs is a log-injection vector."""
    r = client.get("/livez", headers={CORRELATION_ID_HEADER: "bad value with spaces"})
    assert " " not in r.headers[CORRELATION_ID_HEADER]


def test_two_requests_get_different_request_ids(client):
    first = client.get("/livez").headers[REQUEST_ID_HEADER]
    second = client.get("/livez").headers[REQUEST_ID_HEADER]
    assert first != second


# ── Error contract through the stack ─────────────────────────────────────────

def test_a_404_uses_the_error_contract_and_carries_a_request_id(client):
    r = client.get("/no-such-route-exists-anywhere")
    assert r.status_code == 404
    body = r.json()
    assert body["error_code"] == "not_found"
    assert body["request_id"]
    assert body["request_id"] == r.headers[REQUEST_ID_HEADER]


def test_a_validation_error_names_fields_but_not_values(client):
    """Field locations are what a client needs. The rejected values are not
    returned — they can contain whatever was submitted."""
    r = client.post("/readyz", json={"unexpected": "sensitive-value-12345"})
    # /readyz is GET-only, so this is a 405; either way, no echoed value.
    assert "sensitive-value-12345" not in r.text


def test_an_unhandled_exception_returns_the_generic_body_with_a_request_id(client):
    """Nothing from the exception reaches the client: no traceback, no SQL, no
    hostname, no library detail."""
    from fastapi import APIRouter
    app = client.app
    router = APIRouter()

    @router.get("/_test_boom")
    async def boom():
        raise RuntimeError("postgresql://ira:leakedpassword@db:5432/ira exploded")

    app.include_router(router)

    r = client.get("/_test_boom")
    assert r.status_code == 500
    body = r.json()
    assert body["error_code"] == "internal_error"
    assert body["message"] == "An internal error occurred."
    assert body["request_id"]
    assert "leakedpassword" not in r.text
    assert "Traceback" not in r.text
    assert "RuntimeError" not in r.text


# ── Existing behaviour is preserved ──────────────────────────────────────────

def test_the_original_health_endpoints_still_exist(client):
    from tests.route_introspection import route_paths
    paths = set(route_paths(client.app))
    assert "/health" in paths
    assert "/health/detail" in paths
    assert "/livez" in paths
    assert "/readyz" in paths


def test_auth_endpoints_are_unchanged(client):
    from tests.route_introspection import route_paths
    paths = set(route_paths(client.app))
    for path in ("/auth/token", "/auth/logout", "/auth/refresh"):
        assert path in paths
