"""The request-context and IP-blocklist middlewares leave no reference cycles behind.

Found by the wall-clock soak: the demo API's memory crept up by about 6 % an
hour. Most of what built up between the collector's full collections was
cyclic garbage made on every request with a body by Starlette's
BaseHTTPMiddleware (an anyio task group per body read; its cancellation leaves
frames, a CancelledError and its traceback in a cycle). Both middlewares are
now plain ASGI middlewares. A full collection is rare in a long-running
process (CPython runs one only once the oldest generation has grown by a
quarter), so garbage made per request is memory held for a long time.

Each test counts what the collector finds after many POST requests, with
automatic collection off, and compares it with the same app without the
middleware. Negative control: with the previous BaseHTTPMiddleware versions
both tests fail (51 extra objects per request for each middleware).
"""
from __future__ import annotations

import gc

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

N = 60


def _app(*middlewares) -> FastAPI:
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request):
        return {"n": len(await request.body())}

    for m in middlewares:
        app.add_middleware(m)
    return app


def _garbage_per_request(app: FastAPI) -> float:
    with TestClient(app) as client:
        for _ in range(5):   # warm up imports and caches
            assert client.post("/echo", json={"reading": 72.5}).status_code == 200
        gc.collect()
        gc.disable()
        try:
            for _ in range(N):
                assert client.post("/echo", json={"reading": 72.5}).status_code == 200
            found = gc.collect()
        finally:
            gc.enable()
    return found / N


@pytest.fixture
def not_blocked(monkeypatch):
    import utils.playbooks as playbooks

    async def never(_address):
        return False
    monkeypatch.setattr(playbooks, "is_ip_blocked", never)


def _extra(middleware) -> float:
    bare = min(_garbage_per_request(_app()) for _ in range(2))
    with_mw = min(_garbage_per_request(_app(middleware)) for _ in range(2))
    return with_mw - bare


def test_request_context_middleware_leaves_no_cycles():
    from api.middleware.request_context import RequestContextMiddleware
    extra = _extra(RequestContextMiddleware)
    assert extra < 2, f"RequestContextMiddleware leaves {extra:.1f} cyclic objects per request"


def test_ip_block_middleware_leaves_no_cycles(not_blocked):
    from app.middleware import _IPBlockMiddleware
    extra = _extra(_IPBlockMiddleware)
    assert extra < 2, f"_IPBlockMiddleware leaves {extra:.1f} cyclic objects per request"


def test_the_ids_still_reach_the_response_and_the_state():
    """What the middleware is for, unchanged by the rewrite: the response carries
    the request and correlation IDs, and request.state has them for the error
    handlers outside it."""
    from api.middleware.request_context import RequestContextMiddleware
    from core.runtime.request_context import CORRELATION_ID_HEADER, REQUEST_ID_HEADER, get_request_id

    app = FastAPI()
    seen = {}

    @app.post("/echo")
    async def echo(request: Request):
        seen["state"] = request.state.request_id
        seen["var"] = get_request_id()
        return {}

    app.add_middleware(RequestContextMiddleware)
    with TestClient(app) as client:
        r = client.post("/echo", json={}, headers={CORRELATION_ID_HEADER: "corr-abc-123"})
    assert r.status_code == 200
    assert r.headers[REQUEST_ID_HEADER] == seen["state"] == seen["var"] != ""
    assert r.headers[CORRELATION_ID_HEADER] == "corr-abc-123"
