"""P16-R1b — the route enumeration must not be able to under-count silently.

FastAPI 0.141 stopped flattening included routers into `app.routes`. The obvious
enumeration went from 300 paths to 79 without erroring, and nine tests went red.

Nine going red was luck. They asserted routes were PRESENT, so an undercount
failed them. **Two tests in this repository assert the opposite** — that no
route is unauthenticated, and that honeypots are registered before API routes.
Assertions of that shape go GREEN over an almost-empty list. Had the framework
bump landed in a batch that only asked "did the suite pass", it would have
disabled two security assertions and reported success.

So the guard is tested here the way every other control in this programme is:
by breaking the property and requiring the check to notice.

── And these tests must not pin themselves to one FastAPI ──────────────────

The first version of this file asserted the flattened count was more than twice
the naive one. True on 0.141, false on 0.115 where `app.routes` is already flat
— so it encoded one version's implementation detail as an invariant and went red
in CI against a correct tree. The assertions below hold on both.
"""
from __future__ import annotations

import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "p16r1b-placeholder")

from tests.route_introspection import (  # noqa: E402
    assert_enumeration_is_not_empty,
    iter_routes,
    route_paths,
    routes_at,
)


@pytest.fixture(scope="module")
def app():
    from app.factory import create_app
    return create_app()


def test_the_enumeration_finds_every_route_on_either_fastapi(app) -> None:
    """The PROPERTY, not the version-specific delta.

    The first version of this test asserted the flattened count was more than
    twice the naive one. That is true on FastAPI >= 0.141 and false on 0.115,
    where `app.routes` is already flat — so it encoded an implementation detail
    of one version as an invariant, and went red in CI against a correct tree.

    Exactly the defect this programme keeps finding, committed by the test
    written to catch it. What actually matters is that the enumeration reaches
    every route on WHATEVER FastAPI is installed, and that it is never a subset
    of the naive one.
    """
    naive = {getattr(r, "path", None) for r in app.routes if getattr(r, "path", None)}
    flattened = set(route_paths(app))

    assert naive <= flattened, (
        f"the flattened enumeration LOST paths the naive one had: "
        f"{sorted(naive - flattened)[:8]}. It must be a superset on every "
        f"FastAPI version."
    )
    for path in ("/health", "/readyz", "/api/v1/chat"):
        assert path in flattened, (
            f"{path} is not reachable in the enumeration. On FastAPI >= 0.141 "
            f"that means the helper stopped descending included routers; on "
            f"older versions it means the route is genuinely unregistered."
        )


def test_prefixes_are_applied_not_dropped(app) -> None:
    """`route.path` alone is the in-router path; the prefix lives elsewhere."""
    chat = routes_at(app, "/api/v1/chat")
    assert chat, "/api/v1/chat did not resolve by effective path"

    # `route.path` is the in-router path on FastAPI >= 0.141 and the already
    # prefixed one on older versions. Both are correct for their version, so
    # asserting either specific value pins the test to a FastAPI release. What
    # must hold on both is that the EFFECTIVE path is right and is never
    # double-prefixed.
    own = getattr(chat[0], "path", "")
    assert own in ("/chat", "/api/v1/chat"), (
        f"unexpected route path {own!r} — the helper may be mangling prefixes"
    )
    assert "/api/v1/api/v1" not in "".join(route_paths(app)), (
        "a prefix was applied twice somewhere in the enumeration"
    )


def test_the_emptiness_guard_fires_on_an_undercounting_enumeration() -> None:
    """PLANT — the guard is only worth having if it can go red.

    A stub app with a handful of routes stands in for the real failure: an
    enumeration that silently returns almost nothing. If this does not raise,
    every "no route does X" assertion in the suite is passing over an empty
    list and nobody would know.
    """
    class _Stub:
        routes = [type("R", (), {"path": f"/r{i}"})() for i in range(5)]

    with pytest.raises(AssertionError, match="only 5 paths"):
        assert_enumeration_is_not_empty(_Stub())


def test_the_guard_passes_on_the_real_application(app) -> None:
    """The other half — a guard that always fires is as useless as one that
    never does."""
    assert_enumeration_is_not_empty(app)


def test_iter_routes_yields_real_route_objects(app) -> None:
    """The dependency tree has to survive flattening, or the authentication
    assertions built on it are inspecting the wrong object."""
    for route, path in iter_routes(app):
        if path == "/api/v1/chat":
            assert hasattr(route, "dependant"), (
                "the flattened route lost its dependant, so security "
                "assertions built on it would inspect nothing"
            )
            return
    pytest.fail("/api/v1/chat was not yielded by iter_routes")
