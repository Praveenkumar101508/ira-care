"""Billing is a cloud surface, and in the private edition it is ABSENT.

WHAT THIS IS FOR

Plans, subscriptions, entitlements and invoices describe a customer paying an
operator for a hosted service. A private install has one owner, their own
machine, and nobody to bill. The billing routes were nonetheless mounted in
both editions — not by a decision, but because nothing had asked the question:
`app/routers.py` mounted all 58 routers unconditionally.

WHY ABSENT RATHER THAN REFUSING

`21f8226` settled this for Forge and the reasoning transfers unchanged: "a
router that mounts, publishes itself in /docs and then answers 403 tells an
operator the capability is present and merely unauthorised, which invites
someone to go looking for the authorisation."

A 403 answers a question nobody asked — *are you allowed?* — when the true
answer is that this product does not have the feature. 404 is the honest reply,
and it is the one that keeps `/docs` describing the product the reader is
actually running.

WHAT THIS DELIBERATELY DOES NOT ASSERT

That billing WORKS in cloud. `tests/tenancy/test_p11_*` own that, against a real
database, and they call the service functions directly — which is why gating the
mount does not disturb them. This file asks the narrower question the mount
decides: **which edition publishes the surface.**

The complement matters as much as the main assertion. A test that only checked
"absent in private" would pass just as happily if billing had been deleted
outright, so the cloud half is what stops this from being a test that a
regression could satisfy by removing the feature.
"""
from __future__ import annotations

import pytest

from tests.route_introspection import assert_enumeration_is_not_empty, route_paths


def _app_paths(monkeypatch, edition: str | None) -> list[str]:
    """Build the app under `edition` and return every route path it exposes."""
    if edition is None:
        monkeypatch.delenv("IRA_EDITION", raising=False)
    else:
        monkeypatch.setenv("IRA_EDITION", edition)
        #: Cloud refuses to start with DEV_MODE — that guard is the subject of
        #: its own tests, and tripping it here would fail this file for a reason
        #: that has nothing to do with billing.
        monkeypatch.delenv("DEV_MODE", raising=False)

    #: Imported inside the function so the edition is read at call time. A
    #: module-scope import binds whatever edition happened to be set when the
    #: test module loaded — the same trap `api/middleware/workspace.py` records
    #: in its own comment.
    from app.factory import create_app

    app = create_app()

    #: Before asserting a route is missing, prove the enumeration found
    #: anything at all. A "no billing routes" assertion passes trivially over an
    #: empty list, which is exactly how P16-R1b's route enumeration regressed:
    #: the checks that asserted ABSENCE went green over a fraction of the app.
    assert_enumeration_is_not_empty(app)
    return route_paths(app)


def test_private_does_not_publish_a_billing_surface(monkeypatch):
    paths = _app_paths(monkeypatch, None)
    billing = sorted(p for p in paths if "/billing" in p)

    assert billing == [], (
        "the private edition published billing routes: "
        f"{billing}. A single-owner install has nobody to bill, and a mounted "
        "route tells a reader the capability exists."
    )


def test_cloud_does_publish_a_billing_surface(monkeypatch):
    paths = _app_paths(monkeypatch, "cloud")
    billing = sorted(p for p in paths if "/billing" in p)

    assert billing, (
        "the cloud edition published NO billing routes. Either the mount is "
        "gated on the wrong side of the condition, or billing has been removed "
        "— and the private-edition test above cannot tell those apart on its "
        "own, which is why this one exists."
    )


#: `test_the_two_editions_differ_only_where_intended` used to live here. It
#: asked whether gating billing had removed anything else, and could only
#: recognise `/billing` while doing so. The second mount gate — workspace
#: management — made that a question about the whole surface rather than about
#: billing, so it moved to `test_edition_surface_difference.py` and now reads
#: the declaration in `edition_surfaces.py` instead of a literal. Nothing was
#: dropped: the assertion is stricter there, because it also fails on a gate
#: nobody declared.


def test_the_switch_is_declared_where_every_other_edition_difference_lives(monkeypatch):
    """Not an `if is_cloud()` scattered into a router file.

    `core/editions.py` exists so the differences between the editions can be
    read in one table rather than hunted for. A gate that works but is declared
    somewhere else is a gate the next person will not find.
    """
    from core.editions import (
        EDITION_CLOUD, EDITION_PRIVATE, edition_default, edition_default_keys,
    )

    assert "billing_available" in edition_default_keys()
    assert edition_default("billing_available", EDITION_PRIVATE) is False
    assert edition_default("billing_available", EDITION_CLOUD) is True
