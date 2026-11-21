"""`GET /api/v1/edition` cannot disagree with the product it describes.

WHAT THIS IS FOR

The endpoint exists because a client had no way to ask which product it was
talking to — the gap that left the billing page showing an error in a private
install rather than saying the product has no billing.

An endpoint that ANSWERS that question is only worth having if the answer is
true, and the way these go wrong is well documented in this repository: a
hand-maintained description drifts from the code, and nothing notices because
the description is what everyone reads. So the tests below are not about the
handler returning 200. They are about the answer being unable to differ from
the thing it describes.

THREE PROPERTIES

1. Every client-facing edition default appears, and with the value the table
   holds. A key added to `_EDITION_DEFAULTS` is published automatically; one
   that stopped being published fails here rather than going quietly missing.
2. Where a capability is enforced by NOT MOUNTING the routes — billing, and
   the three workspace management verbs — the report and the router table
   agree. This is the property that would actually mislead a client if it
   broke, and it is driven from `tests/core/edition_surfaces.py` so a third
   mount gate is covered as soon as it is declared.
3. The endpoint is reachable in BOTH editions. An edition that withheld it
   would be the one case where the question cannot be answered.

WHAT IT DELIBERATELY DOES NOT ASSERT

That a `false` capability means the routes are absent. That holds for the two
mount-gated capabilities and NOT for armory or forge, which stay mounted and
refuse at boot instead — `21f8226` chose that on purpose. A test that asserted
absence for all of them would be asserting something untrue about two, and
would have to be weakened later by someone who assumed it was right.

Nor that `workspace_management_available` being false means `/api/v1/workspaces`
is gone. The picker is mounted in every edition and must be; only the three
management verbs move. `tests/core/test_edition_surface_workspaces.py` holds
both halves of that.
"""
from __future__ import annotations

import pytest

from tests.route_introspection import assert_enumeration_is_not_empty, route_paths


def _app(monkeypatch, edition: str | None):
    if edition is None:
        monkeypatch.delenv("IRA_EDITION", raising=False)
    else:
        monkeypatch.setenv("IRA_EDITION", edition)
        #: Cloud refuses to start under DEV_MODE; that guard has its own tests
        #: and tripping it here would fail this file for an unrelated reason.
        monkeypatch.delenv("DEV_MODE", raising=False)

    from app.factory import create_app

    app = create_app()
    assert_enumeration_is_not_empty(app)
    return app


@pytest.mark.parametrize("edition", [None, "cloud"], ids=["private", "cloud"])
def test_the_report_is_the_table(monkeypatch, edition):
    """Property 1 — every client-facing default, with the table's value."""
    _app(monkeypatch, edition)

    from api.routes.edition import _NOT_CLIENT_FACING, capability_map
    from core.editions import edition_default, edition_default_keys

    reported = capability_map()
    expected_keys = set(edition_default_keys()) - _NOT_CLIENT_FACING

    assert set(reported) == expected_keys, (
        "the report and the edition table hold different keys. A capability "
        "the table gained is not being published, or one it lost still is."
    )
    for key, value in reported.items():
        assert value == edition_default(key), (
            f"the report says {key}={value!r}, the table says "
            f"{edition_default(key)!r}. The endpoint is describing something "
            f"other than the running product."
        )


@pytest.mark.parametrize("edition", [None, "cloud"], ids=["private", "cloud"])
def test_the_mount_gated_answers_match_what_is_mounted(monkeypatch, edition):
    """Property 2 — the capabilities enforced by absent routes.

    If one of these ever disagrees, a client is told the product has a feature
    while the routes 404, or told it has none while they answer. Both mislead,
    and the endpoint is the thing the client trusted.

    Driven from `edition_surfaces.MOUNT_GATED` rather than a literal pair, so a
    third mount gate is covered here the moment it is declared — the same reason
    `capability_map` is derived from the table instead of written out.
    """
    app = _app(monkeypatch, edition)

    from api.routes.edition import capability_map
    from tests.core.edition_surfaces import MOUNT_GATED, method_paths

    served = method_paths(app)
    reported = capability_map()

    for capability, claims in MOUNT_GATED.items():
        mounted = any(claims(method, path) for method, path in served)
        assert reported[capability] is mounted, (
            f"the endpoint reports {capability}={reported[capability]!r} while "
            f"the app {'does' if mounted else 'does not'} mount the routes it "
            f"describes."
        )


@pytest.mark.parametrize(
    "edition,expected", [(None, False), ("cloud", True)], ids=["private", "cloud"],
)
def test_the_gated_capabilities_are_false_in_private_and_true_in_cloud(
    monkeypatch, edition, expected
):
    """The direction, pinned separately from the agreement above.

    Without this, a build that mounted billing in private and also reported it
    as available would satisfy the agreement test perfectly: the report and the
    routes would still match each other, and both would be wrong.
    """
    _app(monkeypatch, edition)

    from api.routes.edition import capability_map
    from tests.core.edition_surfaces import MOUNT_GATED

    reported = capability_map()
    for capability in MOUNT_GATED:
        assert reported[capability] is expected, (
            f"{capability} is {reported[capability]!r} in the "
            f"{edition or 'private'} edition; expected {expected!r}."
        )


@pytest.mark.parametrize("edition", [None, "cloud"], ids=["private", "cloud"])
def test_the_endpoint_itself_is_reachable_in_both_editions(monkeypatch, edition):
    """Property 3 — the question can always be asked.

    Gating this one behind an edition would be the single worst place to do it:
    a client that does not know which product it is talking to is exactly the
    client that needs the answer.
    """
    app = _app(monkeypatch, edition)

    assert any(p == "/api/v1/edition" for p in route_paths(app)), (
        "/api/v1/edition is not mounted in this edition, so a client cannot "
        "find out what it is talking to."
    )


def test_the_internal_posture_is_not_published(monkeypatch):
    """`auth_fail_closed_on_redis` is a correct default and none of a client's
    business. The exclusion is deliberate and small; this pins it so it is not
    widened by accident into a habit of hiding things from the client."""
    _app(monkeypatch, None)

    from api.routes.edition import _NOT_CLIENT_FACING, capability_map

    assert "auth_fail_closed_on_redis" not in capability_map()
    assert _NOT_CLIENT_FACING == {"auth_fail_closed_on_redis"}, (
        "the not-client-facing exclusion has grown. Each entry hides a fact "
        "from the client, so each needs a reason written down."
    )
