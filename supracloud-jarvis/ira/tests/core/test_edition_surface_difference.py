"""Everything the private edition lacks, it lacks on purpose.

WHAT THIS CATCHES

A mount condition that is broader than it looks. `if edition_default(...)` in
`app/routers.py` guards the lines indented under it, and an `if` placed one line
too high — or a router moved beneath an existing one — takes unrelated surfaces
out of the private product with it. Every per-capability test would still pass:
each asks only about its own routes, and none of them would notice a third one
had gone missing.

So this asks the whole question once. Take every route cloud serves and private
does not, and require that each is claimed by a gate declared in
`edition_surfaces.MOUNT_GATED`. Anything unclaimed is a surface that vanished
without a decision.

It began as `test_the_two_editions_differ_only_where_intended` inside the
billing file, where it could only recognise `/billing`. The second mount gate
made it a general question, so it lives here and reads the declaration rather
than a literal.

WHY IT IS METHOD-AWARE

`POST /api/v1/workspaces` is gated and `GET /api/v1/workspaces` must never be.
Compared as bare paths those are one entry, and the difference this file exists
to measure would be invisible for the entire workspace surface.
"""
from __future__ import annotations

from tests.core.edition_surfaces import MOUNT_GATED, method_paths, unclaimed
from tests.route_introspection import assert_enumeration_is_not_empty


def _app(monkeypatch, edition: str | None):
    if edition is None:
        monkeypatch.delenv("IRA_EDITION", raising=False)
    else:
        monkeypatch.setenv("IRA_EDITION", edition)
        monkeypatch.delenv("DEV_MODE", raising=False)

    from app.factory import create_app

    app = create_app()
    assert_enumeration_is_not_empty(app)
    return app


def test_every_route_private_lacks_is_one_a_declared_gate_removed(monkeypatch):
    cloud = method_paths(_app(monkeypatch, "cloud"))
    private = method_paths(_app(monkeypatch, None))

    difference = cloud - private
    assert difference, (
        "the two editions publish identical routes, so no mount gate is taking "
        "effect at all."
    )

    stray = [f"{m} {p}" for m, p in unclaimed(difference)]
    assert not stray, (
        f"these routes are in cloud and not in private, and no gate in "
        f"MOUNT_GATED accounts for them: {stray}.\n\n"
        f"Either a mount condition is catching more than it was meant to, or a "
        f"new gate was added without declaring it in "
        f"tests/core/edition_surfaces.py — where it must be declared, so that "
        f"the difference between the two products stays something someone "
        f"chose rather than something that accumulated."
    )


def test_no_declared_gate_has_stopped_removing_anything(monkeypatch):
    """The other direction: a gate that is declared and no longer bites.

    A capability flipped to True for private, or a mount moved out from under
    its `if`, leaves the entry above still present and still passing. This is
    the check that a declaration corresponds to a real difference — the same
    reason each per-capability file asserts the cloud half as well as the
    private one.
    """
    cloud = method_paths(_app(monkeypatch, "cloud"))
    private = method_paths(_app(monkeypatch, None))
    difference = cloud - private

    idle = [
        capability
        for capability, claims in MOUNT_GATED.items()
        if not any(claims(m, p) for m, p in difference)
    ]

    assert not idle, (
        f"{idle} are declared as mount gates and remove nothing from the "
        f"private surface. Either the routes they guard are now mounted in both "
        f"editions, or the capability no longer gates anything and the "
        f"declaration should go."
    )


def test_private_is_never_the_larger_surface(monkeypatch):
    """Nothing is mounted in private and missing from cloud.

    Not a symmetry for its own sake. Every difference recorded in
    `core/editions.py` runs the same way — cloud is the edition that gains a
    surface, private the one that keeps its owner-grade tooling behind
    boot-time refusal rather than an absent route. A route present only in
    private would be a fourth shape nobody has reasoned about, and it would
    reach a multi-tenant deployment as a surprise.
    """
    cloud = method_paths(_app(monkeypatch, "cloud"))
    private = method_paths(_app(monkeypatch, None))

    extra = [f"{m} {p}" for m, p in sorted(private - cloud)]
    assert not extra, (
        f"these routes exist in private and not in cloud: {extra}. If that is "
        f"deliberate it is a new kind of edition difference, and it belongs in "
        f"core/editions.py with the reasoning written down."
    )
