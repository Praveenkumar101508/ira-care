"""3C-05 — the cognitive kernel and the brain stream are process-global.

3C-03 closed `/ws/notifications`, which authorised on a decodable token and then
subscribed the client to one hardcoded channel. That fixed **a socket**. It did
not fix **the class**, and the socket one file over had the same shape:

    @router.websocket("/ws/brain")
    async def ws_brain(websocket: WebSocket):
        ...
        decode_token(token)          # the whole check
        ...
        brain.on_thought(thought_cb) # a process-global object
        brain.on_speak(speak_cb)

`app.state.brain` is one object for the whole process. `feed_percept` pushes
chat and voice transcripts into it from any subsystem, and every subscriber gets
every `thought` and `speak` frame that comes out. The existing code comments
call it "the shared brain" — that is accurate, and in a multi-tenant deployment
it is also the finding.

The ingest side is the same story from the other end. `feed_cognitive_percept`
delivers into `get_kernel()`, a process-global `NeuroKernel` — one event bus,
one global workspace, one self-model, one store — and all three call sites pass
`get_settings().ira_admin_username` as the `owner_id`. So every tenant's content
would land in one object, attributed to one identity, readable through
`/cognition/status`.

── The fix ─────────────────────────────────────────────────────────────────

Three closures, deliberately independent, because this surface carries the
deployment's own inner monologue and no single edit should be able to open it:

  * `brain_enabled()` is False in cloud, so the loop never starts there.
  * `feed_cognitive_percept` refuses in cloud — at the choke point rather than
    at the three call sites, because the fourth call site would not know to
    check.
  * `/ws/brain` resolves a real Principal and requires owner authority, which
    also closes the private-edition case of a delegated user subscribing to the
    owner's thoughts.

None of this implements per-tenant cognition. It says that a single-owner
subsystem does not run in a multi-tenant deployment, which is what the edition
architecture said from Phase 1: owner-grade tooling is absent from cloud, not
merely flagged off.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    _st.SentenceTransformer = object
    _st.CrossEncoder = object

pytest.importorskip("fastapi")

from core.editions import EDITION_CLOUD, EDITION_PRIVATE  # noqa: E402

IRA = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_CLOUD)


@pytest.fixture
def private(monkeypatch):
    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_PRIVATE)


@pytest.fixture
def brain_opted_in(monkeypatch):
    """The environment variable set to its most permissive value.

    The point of every cloud assertion below is that this is *not enough* — an
    operator who turns the brain on must not thereby turn it on for a
    multi-tenant deployment.
    """
    monkeypatch.setenv("IRA_BRAIN_ENABLED", "true")


# ─────────────────────────────────────────────────────────────────────────────
# The loop does not run in cloud
# ─────────────────────────────────────────────────────────────────────────────


def test_the_brain_does_not_run_in_cloud_even_when_enabled(cloud, brain_opted_in):
    from api.routes.brain import brain_enabled

    assert brain_enabled() is False, (
        "the continuous brain loop starts in cloud. It is one process-global "
        "object fed by every caller's transcripts, with no tenant dimension "
        "anywhere in it.")


def test_the_brain_still_runs_in_private_when_enabled(private, brain_opted_in):
    """Positive control. A fix that disabled the feature everywhere would pass
    every cloud assertion in this file and remove a working product."""
    from api.routes.brain import brain_enabled

    assert brain_enabled() is True


def test_the_brain_is_still_off_by_default_in_private(private, monkeypatch):
    monkeypatch.delenv("IRA_BRAIN_ENABLED", raising=False)
    from api.routes.brain import brain_enabled

    assert brain_enabled() is False


# ─────────────────────────────────────────────────────────────────────────────
# Nothing is fed into the shared kernel in cloud
# ─────────────────────────────────────────────────────────────────────────────


async def test_percepts_are_refused_in_cloud(cloud, monkeypatch):
    """The ingest half of the finding.

    Asserted on the kernel not being *reached*, rather than on the return value:
    a version that delivered the percept and then returned False would satisfy a
    weaker test and leak exactly as much.
    """
    import cognition.ingest as ingest

    reached = []
    monkeypatch.setattr(ingest, "get_kernel",
                        lambda: reached.append("kernel") or (_ for _ in ()).throw(
                            AssertionError("cloud reached the process-global kernel")))

    delivered = await ingest.feed_cognitive_percept(
        "admin", "chat", "a message belonging to one tenant")

    assert delivered is False
    assert not reached, "cloud reached the process-global cognitive kernel"


async def test_percepts_still_reach_the_kernel_in_private(private, monkeypatch):
    """Positive control for the test above — without it, a
    `feed_cognitive_percept` that returned False unconditionally would pass."""
    import cognition.ingest as ingest

    seen: list = []

    class _Kernel:
        settings = type("S", (), {"enabled": True})()

        async def perceive(self, event):
            seen.append(event)

    monkeypatch.setattr(ingest, "get_kernel", lambda: _Kernel())

    await ingest.feed_cognitive_percept("owner", "chat", "the owner's own words")

    assert len(seen) == 1, "the private edition no longer feeds its own kernel"


async def test_a_disabled_kernel_is_still_refused_in_private(private, monkeypatch):
    """The pre-existing opt-in must survive the edition check being added in
    front of it."""
    import cognition.ingest as ingest

    class _Kernel:
        settings = type("S", (), {"enabled": False})()

        async def perceive(self, event):  # pragma: no cover - must not run
            raise AssertionError("a disabled kernel received a percept")

    monkeypatch.setattr(ingest, "get_kernel", lambda: _Kernel())

    assert await ingest.feed_cognitive_percept("owner", "chat", "text") is False


def test_the_refusal_is_at_the_choke_point_not_the_call_sites():
    """Three call sites today; the fourth is the one that matters.

    A fix applied at each caller reads as complete and leaves the next caller
    unprotected — which is how `feed_percept` in `brain.py` came to attribute
    every subsystem's transcripts to the deployment admin in the first place.
    """
    source = (IRA / "cognition" / "ingest.py").read_text(encoding="utf-8")

    assert "EDITION_CLOUD" in source, (
        "cognition/ingest.py no longer refuses in cloud. Every caller passes "
        "ira_admin_username as the owner_id, so cloud would pour every "
        "tenant's content into one process-global kernel under one identity.")


# ─────────────────────────────────────────────────────────────────────────────
# The stream is not open to a decodable token
# ─────────────────────────────────────────────────────────────────────────────


def test_the_brain_socket_does_not_authorise_on_a_token_alone():
    """Read from the source: a WebSocket handshake needs a running app.

    This is the assertion 3C-03 made about `/ws/notifications`, made again here
    because the fix there was to a socket and this is a different socket with
    the same shape.
    """
    source = (IRA / "api" / "routes" / "brain.py").read_text(encoding="utf-8")
    ws = source[source.index("async def ws_brain"):]

    assert "principal_from_username" in ws, (
        "the brain socket authorises on a decodable token alone again. A token "
        "outlives the membership it was issued under, and this socket streams a "
        "process-global thought and speech feed derived from every caller's "
        "chat and voice content.")
    assert "is_owner" in ws, (
        "the brain socket no longer requires owner authority, so a delegated "
        "user can subscribe to the owner's inner monologue")


def test_the_brain_socket_refuses_rather_than_accepting_first():
    """Order matters on a socket.

    Accepting the handshake and *then* deciding is a live subscription for
    however long the check takes, and on this socket the frames are pushed
    rather than pulled. The refusal must come before `websocket.accept`.
    """
    source = (IRA / "api" / "routes" / "brain.py").read_text(encoding="utf-8")
    ws = source[source.index("async def ws_brain"):]

    accept_at = ws.index("websocket.accept")
    for marker in ("principal_from_username", "is_owner", "4003"):
        assert ws.index(marker) < accept_at, (
            f"{marker!r} is checked after the handshake is accepted — the "
            f"client is subscribed to the shared brain before it is authorised")


def test_both_closures_are_present_and_independent():
    """The brain surface is closed twice, and that is deliberate.

    `brain_enabled()` being False in cloud already prevents the stream from
    having anything to send. The socket's own check exists so that flipping one
    of them — an operator setting an environment variable, an edit to the
    edition helper — does not open a cross-tenant stream on its own.
    """
    source = (IRA / "api" / "routes" / "brain.py").read_text(encoding="utf-8")

    enabled_fn = source[source.index("def brain_enabled"):source.index("def _safe_source")]
    assert "EDITION_CLOUD" in enabled_fn, "brain_enabled() no longer refuses in cloud"

    ws = source[source.index("async def ws_brain"):]
    assert "is_owner" in ws, "the brain socket lost its independent owner check"


def test_the_shared_kernel_really_is_shared():
    """The premise, asserted rather than assumed.

    Everything above rests on `get_kernel()` returning one object per process.
    If it ever became per-tenant, these refusals would be over-restrictive
    rather than necessary — and this test is what would say so.
    """
    from cognition.runtime import get_kernel, reset_kernel

    reset_kernel()
    first = get_kernel()
    second = get_kernel()
    try:
        assert first is second, (
            "get_kernel() no longer returns a process-global object — re-read "
            "3C-05, the refusals it justifies may no longer be the right shape")
    finally:
        reset_kernel()
