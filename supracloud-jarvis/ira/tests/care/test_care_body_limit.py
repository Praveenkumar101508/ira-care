"""The IRA CARE server refuses an oversized request body before reading it (REVIEW.md S10, S11).

Before the ceiling, every route that takes a body, the unauthenticated sign-up and pairing
routes included, read the whole body into memory and parsed it before its schema or its rate
limit could refuse it. These tests run the care app's real middleware stack with a small
ceiling, and check that a body over it is answered 413 whether it declares its length or is
streamed without one, and that the route never runs. The controls show the same requests under
the ceiling still reach the route.
"""
from __future__ import annotations

import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

pytest.importorskip("slowapi")

from fastapi.testclient import TestClient  # noqa: E402

from app.middleware import limiter  # noqa: E402
from care import body_limit, routes, schemas  # noqa: E402

LIMIT = 4096
PAIR = "/api/v1/care/devices/pair"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("IRA_CARE_ALLOWED_ORIGINS", "")
    monkeypatch.setenv(body_limit.ENV, str(LIMIT))
    limiter.reset()
    reached = []

    async def no_such_code(code, app_version=None):
        reached.append(code)
        return None

    monkeypatch.setattr(routes.repo, "pair_device", no_such_code)
    from care.server import create_care_app
    c = TestClient(create_care_app())      # no `with`: the lifespan (database, jobs) is not started
    c.reached = reached
    yield c
    limiter.reset()


def _pair_body(padding: int) -> str:
    return '{"pairing_code": "cp1.x.GUESS000", "pad": "' + "x" * padding + '"}'


def test_a_declared_oversized_body_is_refused_before_the_route(client):
    r = client.post(PAIR, content=_pair_body(LIMIT * 4), headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["detail"] == body_limit.DETAIL
    assert client.reached == []


def test_a_streamed_oversized_body_is_refused_before_the_route(client):
    def chunks():                          # no Content-Length: sent chunked
        yield b'{"pairing_code": "cp1.x.GUESS000", "pad": "'
        for _ in range(8):
            yield b"x" * 1024
        yield b'"}'

    r = client.post(PAIR, content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["detail"] == body_limit.DETAIL
    assert client.reached == []


def test_control_a_body_under_the_ceiling_reaches_the_route(client):
    r = client.post(PAIR, content=_pair_body(100), headers={"content-type": "application/json"})
    assert r.status_code == 404 and client.reached == ["cp1.x.GUESS000"]

    def chunks():
        yield b'{"pairing_code": "cp1.x.GUESS001",'
        yield b' "pad": "' + b"x" * 100 + b'"}'

    assert client.post(PAIR, content=chunks(), headers={"content-type": "application/json"}).status_code == 404
    assert client.reached == ["cp1.x.GUESS000", "cp1.x.GUESS001"]


def test_the_default_ceiling_fits_the_largest_research_recording(monkeypatch):
    monkeypatch.delenv(body_limit.ENV, raising=False)
    assert body_limit.max_request_bytes() == body_limit.DEFAULT_MAX_BYTES == 16 * 1024 * 1024
    # The widest legal research body: every stream at its sample cap, every number at its
    # longest JSON spelling (care/schemas.py).
    n = schemas.RESEARCH_MAX_SAMPLES
    number = len("-1.2345678901234567e-300, ")
    widest = (n + n) * number + (3 * n + n) * number + n * 4
    assert widest < body_limit.DEFAULT_MAX_BYTES
    for bad in ("", "0", "-5", "lots"):
        monkeypatch.setenv(body_limit.ENV, bad)
        assert body_limit.max_request_bytes() == body_limit.DEFAULT_MAX_BYTES


def test_research_accelerometer_rows_are_exactly_three_axes():
    ok = {"fs_hz": 32, "samples": [[0.0, 0.1, 1.0]]}
    assert schemas.ResearchAccIn(**ok).samples == [[0.0, 0.1, 1.0]]
    for row in ([0.0, 0.1], [0.0] * 4, [0.0] * 100_000):
        with pytest.raises(ValueError):
            schemas.ResearchAccIn(fs_hz=32, samples=[row])
