"""Which surfaces the two editions publish differently, declared once.

Two capabilities are enforced by NOT MOUNTING — billing, and the three
workspace management verbs. Everything else that differs between the editions
differs in behaviour, not in whether a route exists.

That distinction is worth one shared declaration rather than a copy in each
test file, for a reason this repository keeps rediscovering: two lists meant to
agree eventually do not. `test_edition_surface_difference.py` reads the
declaration below and requires that EVERY route the private edition lacks is
claimed by one of these predicates — so a third gate added without a line here
fails, rather than quietly widening the difference between the products.

Predicates rather than path lists, because the two gates are different shapes.
Billing is a whole prefix family. Workspace management is three verbs sharing
their paths with two ungated ones: `POST /api/v1/workspaces` creates and is
gated, `GET /api/v1/workspaces` lists and must never be — so a path-only rule
cannot express it, and a test written with one would be asserting something
subtly untrue about the picker.
"""
from __future__ import annotations

from typing import Callable, Iterator

from tests.route_introspection import iter_routes

#: HTTP methods FastAPI adds for us. Not surfaces anyone decided to publish.
_IMPLICIT = frozenset({"HEAD", "OPTIONS"})


def method_paths(app: object) -> set[tuple[str, str]]:
    """Every `(method, effective path)` the app serves.

    Method-aware on purpose: `/api/v1/workspaces` exists in both editions and
    answers different verbs in each, which a set of paths cannot express.
    """
    out: set[tuple[str, str]] = set()
    for route, path in iter_routes(app):
        for method in getattr(route, "methods", ()) or ():
            if method not in _IMPLICIT:
                out.add((method, path))
    return out


def _billing(method: str, path: str) -> bool:
    return "/billing" in path


def _workspace_management(method: str, path: str) -> bool:
    """Create, rename and leave — but never the picker.

    The picker is `GET /api/v1/workspaces` and `POST /api/v1/workspaces/select`,
    and both stay mounted in every edition: a client cannot send
    `X-Workspace-ID` before it knows which workspaces exist, and it is the
    listing's `selection_required` that tells it whether it must.
    """
    if not path.startswith("/api/v1/workspaces"):
        return False
    if path.endswith("/select"):
        return False
    return method in {"POST", "PATCH", "DELETE"}


#: capability key in `_EDITION_DEFAULTS` → what its mount gate removes.
MOUNT_GATED: dict[str, Callable[[str, str], bool]] = {
    "billing_available": _billing,
    "workspace_management_available": _workspace_management,
}


def unclaimed(difference: set[tuple[str, str]]) -> Iterator[tuple[str, str]]:
    """Routes in the difference that no declared gate accounts for."""
    for method, path in sorted(difference):
        if not any(claims(method, path) for claims in MOUNT_GATED.values()):
            yield method, path
