"""Every route that names a person, or one of a person's things, checks that the caller may
see that person (D-076). Static: a new route added without the guard fails here."""
from __future__ import annotations

import re

from care import routes

RESOURCE_PARAMS = set(routes._RESOURCE_GUARDS)


def _routes():
    for r in routes.router.routes:
        path = getattr(r, "path", "")
        if "/devices/me" in path or "/providers/" in path:
            continue  # the watch's own routes act for its paired person; provider callbacks are signed
        yield r, path


def test_every_person_route_and_every_resource_route_is_guarded():
    unguarded = []
    for r, path in _routes():
        params = set(re.findall(r"{(\w+)}", path))
        deps = {d.call for d in r.dependant.dependencies}
        if "person_id" in params and routes.person_guard not in deps:
            unguarded.append(path)
        for p in params & RESOURCE_PARAMS:
            if routes._RESOURCE_GUARDS[p] not in deps:
                unguarded.append(path)
    assert not unguarded, f"routes without the person-level guard: {unguarded}"


def test_there_is_at_least_one_route_of_each_guarded_kind():
    paths = [p for _r, p in _routes()]
    for param in ["person_id", *RESOURCE_PARAMS]:
        assert any("{" + param + "}" in p for p in paths), param
