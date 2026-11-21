"""3C-03 — a notification reaches one tenant, not the deployment.

`worker/notifier.py` published every notification to one hardcoded channel,
`ira:notifications`, and `/ws/notifications` subscribed any client with a
**decodable token** to that channel and forwarded everything on it.

So every authenticated user in every tenant received every notification
generated anywhere in the deployment. The payload carries `title` and `body`,
which for a briefing is the owner's own content and for the security monitor is
their security events. That is a live cross-tenant leak on an open socket, not a
theoretical one — no guessing, no forged identifier, nothing to exploit. Connect
and listen.

It also failed the "revoked users lose access immediately" property from the
other direction: a token that still parses was the entire check, so a socket
opened before a membership was revoked kept receiving, and so did one opened
after.

── The fix ─────────────────────────────────────────────────────────────────

`notify_channel(principal)` returns a per-tenant channel in cloud and refuses
when there is no principal to name one — a notification with no tenant has no
channel it could safely go to, and a guessed tenant is worse than an error. The
private edition keeps the old global name, because a single-tenant deployment
has exactly one tenant and renaming it would break clients mid-upgrade for no
security gain.

The socket resolves a real Principal at connect time rather than reading the
token's claims, because a token outlives the membership it was issued under and
a long-lived socket outlives both.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    _st.SentenceTransformer = object
    _st.CrossEncoder = object

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from tests.tenancy.adversarial import matrix  # noqa: E402,F401

from core.editions import EDITION_CLOUD, EDITION_PRIVATE  # noqa: E402
from core.identity.db_context import MissingTenantContext  # noqa: E402

# conftest replaces `worker.notifier` with a stub carrying only `notify=None`,
# because most tests want the Telegram and e-mail paths gone. This file is about
# the real module's channel logic, so the stub is evicted and the genuine module
# imported — testing the stub would be testing nothing.
sys.modules.pop("worker.notifier", None)
import importlib  # noqa: E402

_notifier = importlib.import_module("worker.notifier")
REDIS_NOTIFY_CHANNEL = _notifier.REDIS_NOTIFY_CHANNEL
notify_channel = _notifier.notify_channel


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_CLOUD)


@pytest.fixture
def private(monkeypatch):
    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_PRIVATE)


# ─────────────────────────────────────────────────────────────────────────────
# The channel
# ─────────────────────────────────────────────────────────────────────────────


def test_two_tenants_do_not_share_a_channel(matrix, cloud):
    """The finding, in one assertion.

    A and B are the same tenant and legitimately share notifications — a
    briefing is a tenant-level event. C must not hear any of it.
    """
    a = notify_channel(matrix.a.principal())
    b = notify_channel(matrix.b.principal())
    c = notify_channel(matrix.c.principal())

    assert a == b, "two workspaces of one tenant should share the tenant's channel"
    assert a != c, "two tenants share a notification channel"


def test_the_channel_names_the_tenant(matrix, cloud):
    channel = notify_channel(matrix.a.principal())

    assert str(matrix.a.tenant_id) in channel
    assert channel != REDIS_NOTIFY_CHANNEL, (
        "cloud is still publishing to the global channel, which every tenant "
        "can subscribe to")


def test_cloud_refuses_a_notification_with_no_principal(matrix, cloud):
    """No tenant, no channel. Publishing to the global one is the defect being
    fixed, and publishing to a guessed tenant would be worse — so it raises and
    the caller has to say who the notification is for."""
    with pytest.raises(MissingTenantContext):
        notify_channel(None)


def test_the_private_edition_keeps_the_global_name(matrix, private):
    """A single-tenant deployment has exactly one tenant, so the shared channel
    was never a leak there. Renaming it would break clients mid-upgrade for no
    security gain, so it does not change."""
    assert notify_channel(None) == REDIS_NOTIFY_CHANNEL
    assert notify_channel(matrix.a.principal()) == REDIS_NOTIFY_CHANNEL


# ─────────────────────────────────────────────────────────────────────────────
# The publisher and the subscriber agree
# ─────────────────────────────────────────────────────────────────────────────


async def test_notify_publishes_to_the_tenants_channel(matrix, cloud, monkeypatch):
    """Where the payload actually goes.

    A channel function that is correct but unused would leave the leak exactly
    where it was, so this asserts on the `publish` call rather than on the
    helper.
    """
    published: list[tuple[str, str]] = []

    class _Redis:
        async def publish(self, channel, payload):
            published.append((channel, payload))

    monkeypatch.setattr(_notifier, "get_redis", lambda: _Redis())

    _publish_redis = _notifier._publish_redis

    await _publish_redis({"title": "private", "body": "content"},
                         principal=matrix.a.principal())

    assert len(published) == 1
    channel, payload = published[0]
    assert str(matrix.a.tenant_id) in channel
    assert channel != REDIS_NOTIFY_CHANNEL
    # And a subscriber in another tenant is listening somewhere else entirely.
    assert channel != notify_channel(matrix.c.principal())


def test_the_socket_subscribes_to_the_resolved_channel():
    """Read from the source, because a WebSocket handshake needs a running app.

    The two failure modes are a subscribe to the bare constant, and a
    `decode_token` that is the whole authorisation. Both read as ordinary code.
    """
    import pathlib
    import re

    source = pathlib.Path(__file__).resolve().parents[2].joinpath(
        "api/routes/notifications.py").read_text(encoding="utf-8")

    assert "notify_channel(" in source, (
        "the notifications socket no longer resolves a per-tenant channel")
    assert not re.search(r"subscribe\(\s*REDIS_NOTIFY_CHANNEL\s*\)", source), (
        "the socket subscribes to the global channel again — every tenant's "
        "notifications go to every connected client")
    assert "principal_from_username" in source, (
        "the socket authorises on a decodable token alone again. A token "
        "outlives the membership it was issued under, and a long-lived socket "
        "outlives both.")


def test_every_notify_caller_can_name_a_tenant():
    """`notify()` takes a principal; the callers have to pass one.

    Not asserted as "every call site passes one" — several are private-edition
    subsystems where the global channel is correct and the parameter is
    genuinely optional. What is asserted is that the parameter exists, so a
    caller that needs to scope its notification is able to.
    """
    import inspect

    notify = _notifier.notify

    assert "principal" in inspect.signature(notify).parameters, (
        "notify() cannot name a tenant, so no caller can scope a notification")
