"""P16-R1b — enumerate an app's routes under FastAPI >= 0.141.

── What changed, and why it is not a test-only concern ─────────────────────

Before 0.141, `include_router()` COPIED each sub-route into `app.routes` with
the prefix already baked into `route.path`. `app.routes` was therefore a flat
list of every reachable path, and a dozen tests read it that way.

FastAPI 0.141 / Starlette 1.6 stopped copying. `app.routes` now holds one
`_IncludedRouter` per `include_router()` call — 58 of them in this app — and the
real routes hang off `original_router.routes` with their prefixes carried
separately on `include_context.prefix`, applied at match time.

Nothing about the application changed. What changed is that **the obvious way to
ask "what routes exist?" now silently returns a fraction of them**: 79 entries
instead of several hundred, with `/health`, `/readyz` and `/api/v1/chat` all
absent.

That is exactly the failure this programme keeps finding — a check that still
runs, still passes its own assertions, and covers less than it appears to. Here
it surfaced as nine red tests, because those tests asserted routes were
PRESENT. The dangerous direction is the opposite one: a test asserting no route
is unauthenticated, or that honeypots precede API routes, would have gone
**green over an empty enumeration**.

Two of the nine did exactly that shape:
`test_the_security_scanner_reports_nothing_for_an_open_route` and
`test_canary_honeypots_registered_before_api_routes`.

── Why product code is unaffected ──────────────────────────────────────────

Checked rather than assumed: nothing outside `tests/` enumerates `app.routes`.
Routing itself is unaffected — Starlette matches through the `_IncludedRouter`,
which is the whole point of the change.
"""
from __future__ import annotations

from typing import Iterator


def _prefix_of(route: object) -> str:
    ctx = getattr(route, "include_context", None)
    return getattr(ctx, "prefix", "") or ""


def iter_routes(app_or_router: object, prefix: str = "") -> Iterator[tuple[object, str]]:
    """Yield `(route, effective_path)` for every route, descending includes.

    `route` is the real route object — its dependencies, methods and endpoint
    are the ones that serve traffic. `effective_path` is what a client calls,
    which `route.path` alone no longer gives once a prefix is involved.
    """
    routes = getattr(app_or_router, "routes", None)
    if routes is None:
        return
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from iter_routes(original, prefix + _prefix_of(route))
        else:
            yield route, prefix + (getattr(route, "path", "") or "")


def route_paths(app: object) -> list[str]:
    """Every effective path, including those behind included routers."""
    return [path for _route, path in iter_routes(app)]


def routes_at(app: object, path: str) -> list[object]:
    """Every route object serving `path`, by effective path."""
    return [route for route, effective in iter_routes(app) if effective == path]


def assert_enumeration_is_not_empty(app: object) -> None:
    """Guard against the silent-undercount failure this module exists for.

    A route enumeration that returns almost nothing makes every "no route
    does X" assertion vacuously true. Any test that scans routes looking for
    something BAD should call this first, so that an enumeration which stopped
    working fails loudly instead of passing perfectly.
    """
    paths = route_paths(app)
    assert len(paths) > 100, (
        f"route enumeration returned only {len(paths)} paths. This app mounts "
        f"several hundred. Any assertion that no route is unauthenticated "
        f"would now be passing over an almost-empty list — see this module's "
        f"docstring for the FastAPI 0.141 change that caused it."
    )
