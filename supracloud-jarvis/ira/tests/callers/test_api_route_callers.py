"""Two API routes, driven over the real HTTP stack.

Four more (agents, files, technical library, self knowledge) were retired with
the parts of the source project they tested; see docs/DECISIONS.md D-007.

These go through `create_app()` over ASGI rather than by calling the handler
functions directly, because the question P12-01 asks about a route is
not only "does its query bind" but "does the binding survive the path a request
actually takes" — the router, the dependency graph, the middleware.

Only the two authentication dependencies are overridden. That is a deliberate
and stated limit rather than a convenience:

  * `require_auth` and `require_workspace_principal` are where a request's
    workspace authority is DERIVED, and the properties about that derivation —
    that the tenant is server-side, that a header cannot manufacture a
    membership — are D3 items 1 and 2, which belong to P12-02's workspace
    surface, not here.
  * What P12-01 needs from a route is what happens AFTER a verified principal
    exists, and overriding the dependency with a real verified principal is the
    way to reach that without also asserting the thing P12-02 will assert.

So these tests prove binding, and they prove nothing about authentication.
Said plainly here so the coverage number is not read as more than it is.
"""
from __future__ import annotations


import pytest

from tests.callers.conftest import assert_touched

httpx = pytest.importorskip("httpx")


@pytest.fixture
async def client(principal, pool):
    """The real app over ASGI, in the TEST's event loop.

    Two things this fixture is deliberately not doing, each because the
    obvious alternative was tried and failed for a reason worth keeping:

      * **No application lifespan.** Entering the app's lifespan builds its own
        pool from the deployment DSN, which would replace the one the `pool`
        fixture installed. The first attempt did exactly that and failed on a
        password for a role that does not exist here.
      * **No `TestClient`.** Starlette's `TestClient` drives the app from its
        own portal thread with its own event loop, while the asyncpg pool
        belongs to the test's loop. Sharing a connection across the two
        produced `InterfaceError: cannot perform operation: another operation
        is in progress` from inside `pool.release` — a genuinely confusing
        failure that had nothing to do with workspaces.

    `ASGITransport` runs the same routers, the same middleware stack and the
    same dependency graph, in this loop. Only the two auth dependencies are
    replaced, for the reason the module docstring gives.
    """
    from httpx import ASGITransport, AsyncClient

    from api.middleware.auth import require_auth
    from api.middleware.workspace import require_workspace_principal
    from app.factory import create_app

    app = create_app()

    async def _auth():
        return str(principal.user_id)

    async def _principal():
        return principal

    app.dependency_overrides[require_auth] = _auth
    app.dependency_overrides[require_workspace_principal] = _principal
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://p1201.invalid") as c:
        yield c
    app.dependency_overrides.clear()


def _ran(response) -> None:
    """A route that 404s at the ROUTER never reached its handler.

    Any other status — including a 500 from a missing model — still means the
    handler ran and its query was observed, which is what is being collected.
    A 404 from the router itself means the path is wrong and the test is
    measuring nothing.
    """
    assert response.status_code not in (401, 403, 404), (
        f"{response.request.method} {response.url} returned "
        f"{response.status_code}, so the handler never ran and nothing was "
        f"observed. 404 means the path is wrong; 401/403 means a guard ahead "
        f"of the overridden dependencies refused, and the request stopped "
        f"before the query this test exists to watch. Body: "
        f"{response.text[:300]}"
    )


async def test_memory_route_binds(client, audit_probe):
    """`api/routes/memory.py` — the one route with a bare `acquire()` in it."""
    _ran(await client.get("/api/v1/memory"))
    await client.post("/api/v1/memory", json={"content": "P12-01 probe memory"})

    counts = assert_touched(audit_probe, "api/routes/memory.py")
    assert counts["unbound"] == 0, (
        f"api/routes/memory.py ran {counts['unbound']} unbound query(ies). "
        f"This module contains a bare `acquire()` alongside eight "
        f"`workspace_connection` uses, which is exactly the mixture a grep "
        f"reports as bound."
    )


async def test_notifications_route_binds(client, audit_probe):
    """`api/routes/notifications.py`.

    `notifications` is the one workspace-carrying table whose policy reads
    `workspace_id IS NULL OR workspace_id = app.workspace_id`, so a tenant-wide
    notification is readable from any of the tenant's workspaces by design.
    Unbound access here is therefore not automatically a defect — which is why
    the assertion below is about what was OBSERVED rather than about a count of
    zero.
    """
    _ran(await client.get("/notifications"))

    assert_touched(audit_probe, "api/routes/notifications.py")


