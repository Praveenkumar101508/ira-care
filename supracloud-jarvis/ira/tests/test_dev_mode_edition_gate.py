"""P2-02 — DEV_MODE cannot authenticate anyone in a multi-tenant deployment.

`DEV_MODE=true` is a local convenience: it makes `require_auth` return the owner
username without inspecting a token at all, and `authenticate_user` return True
without checking a password. On one person's laptop that is fine. In a
multi-tenant deployment it means **every request from anyone is the owner**,
which is not a degraded state — it is no authentication at all.

Four call sites honoured the flag, and none of them asked which edition it was
running in:

    api/middleware/auth.py::require_auth        returns the admin username
    api/middleware/auth.py::authenticate_user   returns True for any password
    security/scope_enforcer.py                  skips scope enforcement entirely
    api/routes/voice.py                         sets is_owner = True, no biometrics

The existing mitigation is `config.py::_forbid_dev_mode_on_public_domain`, which
refuses `dev_mode` on a non-local domain. That is a real control and it is not
this one: it constrains where the process may *run*, and says nothing about which
*edition* it serves. A cloud deployment reachable on an internal hostname
satisfies it.

── The scope enforcer carried a second defect ──────────────────────────────

    username = decode_token(auth[7:]).sub
    if cfg.dev_mode or username == cfg.ira_admin_username:
        return await call_next(request)   # unrestricted

That comparison is **3C-04 exactly** — a deployment-wide setting compared
against a per-tenant username — and the 3C-04 guard never saw it because that
guard scans `api/routes/` and this file is in `security/`. The username comes
from the token's `sub` claim, so in cloud a tenant's user named `admin` would
skip the scope middleware on the strength of their own name.

── Why the gate is `!= private`, not `== cloud` ────────────────────────────

`is_owner` asks `== EDITION_CLOUD`. That is correct for what it does, but it
means a *third* edition added later would inherit the permissive branch by
default. `dev_mode_active()` inverts the test: the bypass is honoured only for
the edition explicitly known to be single-owner, and anything else — cloud,
unknown, misconfigured — denies. An authentication bypass should be opt-in by
name, not by absence.
"""
from __future__ import annotations

import os
import types

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from api.middleware.auth import (  # noqa: E402
    authenticate_user,
    dev_mode_active,
    is_owner,
)
from core.editions import EDITION_CLOUD, EDITION_PRIVATE  # noqa: E402


@pytest.fixture
def dev_mode_on(monkeypatch):
    """`Settings.dev_mode` true — the most permissive configuration there is.

    Every cloud assertion below is that this is *not enough*: an operator who
    turns dev mode on must not thereby turn authentication off for a
    multi-tenant deployment.
    """
    import api.middleware.auth as auth

    cfg = types.SimpleNamespace(
        dev_mode=True,
        ira_admin_username="admin",
        ira_admin_password="the-real-password",
        ira_voice_service_username="ira-voice",
    )
    monkeypatch.setattr(auth, "get_settings", lambda: cfg)
    return cfg


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_CLOUD)


@pytest.fixture
def private(monkeypatch):
    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_PRIVATE)


# ─────────────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────────────


def test_dev_mode_is_never_active_in_cloud(cloud, dev_mode_on):
    """The finding, in one assertion. The flag is on; the answer is still no."""
    assert dev_mode_active() is False, (
        "DEV_MODE is honoured in the cloud edition, so every request "
        "authenticates as the owner without a token")


def test_dev_mode_is_active_in_private_when_enabled(private, dev_mode_on):
    """Positive control. A fix that disabled dev mode everywhere would satisfy
    every cloud assertion here and break local development for no security
    gain."""
    assert dev_mode_active() is True


def test_dev_mode_is_inactive_in_private_when_disabled(private, monkeypatch):
    import api.middleware.auth as auth

    monkeypatch.setattr(
        auth, "get_settings",
        lambda: types.SimpleNamespace(dev_mode=False, ira_admin_username="admin"))
    assert dev_mode_active() is False


def test_an_unknown_edition_denies(dev_mode_on, monkeypatch):
    """Criterion: default and unknown editions fail safely.

    This is why the gate asks `!= private` rather than `== cloud`. A future
    edition — or a misconfigured value — must not inherit an authentication
    bypass simply because nobody remembered to add it to a deny-list.
    """
    for unknown in ("enterprise", "staging", "", "PRIVATE", None):
        monkeypatch.setattr("core.editions.current_edition", lambda u=unknown: u)
        assert dev_mode_active() is False, (
            f"edition {unknown!r} honoured the DEV_MODE authentication bypass")


# ─────────────────────────────────────────────────────────────────────────────
# The call sites
# ─────────────────────────────────────────────────────────────────────────────


def test_credentials_are_actually_checked_in_cloud(cloud, dev_mode_on):
    """`authenticate_user` returned True for any password under dev mode."""
    assert authenticate_user("admin", "not-the-password") is False
    assert authenticate_user("anyone", "anything") is False


def test_require_auth_does_not_hand_out_the_owner_in_cloud():
    """Read from the source: `require_auth` is a FastAPI dependency whose other
    branches need a request, Redis and a token.

    The defect is a single early `return cfg.ira_admin_username` before any
    credential is examined, and it reads as ordinary convenience code.
    """
    import pathlib
    import re

    source = pathlib.Path(__file__).resolve().parents[1].joinpath(
        "api/middleware/auth.py").read_text(encoding="utf-8")
    body = source[source.index("async def require_auth"):source.index("def is_owner")]

    assert "dev_mode_active()" in body, (
        "require_auth reads cfg.dev_mode directly again, so the bypass is not "
        "edition-gated")
    assert not re.search(r"if\s+cfg\.dev_mode\s*:", body), (
        "require_auth still branches on the raw setting")


def test_the_scope_enforcer_is_gated_and_no_longer_compares_usernames():
    """Both defects in that one line, asserted separately.

    The username came from the token's `sub` claim. `is_owner` refuses in cloud
    (3C-04), so routing the check through it closes the name collision as well
    as the dev-mode bypass.
    """
    import pathlib
    import re

    source = pathlib.Path(__file__).resolve().parents[1].joinpath(
        "security/scope_enforcer.py").read_text(encoding="utf-8")

    assert "dev_mode_active()" in source, (
        "the scope enforcer honours raw cfg.dev_mode, skipping enforcement "
        "entirely for every caller")
    assert not re.search(
        r"username\s*==\s*cfg\.ira_admin_username", source), (
        "the scope enforcer decides owner-ness by comparing the token's `sub` "
        "claim against a deployment-wide setting — that is 3C-04, in a "
        "directory the 3C-04 guard does not scan")
    assert "is_owner(" in source


def test_the_voice_biometric_gate_is_edition_aware():
    """`if cfg.dev_mode: is_owner = True` skipped the ECAPA speaker check."""
    import pathlib
    import re

    source = pathlib.Path(__file__).resolve().parents[1].joinpath(
        "api/routes/voice.py").read_text(encoding="utf-8")

    assert "dev_mode_active()" in source
    assert not re.search(r"if\s+cfg\.dev_mode\s*:\s*\n\s+is_owner\s*=\s*True",
                         source), (
        "the voice route grants owner authority from the raw dev-mode flag, "
        "bypassing the biometric check")


# ─────────────────────────────────────────────────────────────────────────────
# Nothing else grants owner authority from a claim
# ─────────────────────────────────────────────────────────────────────────────

#: Modules allowed to read `Settings.dev_mode` for a **security** decision.
#: Everything else reading it is doing something non-authorising (CORS origins,
#: model routing, a status warning) and is listed in NON_SECURITY below.
SECURITY_DEV_MODE_HOME = "api/middleware/auth.py"

#: Reads of `cfg.dev_mode` that decide nothing about identity or authority.
NON_SECURITY = {
    "app/middleware.py": "adds localhost CORS origins",
    "app/lifespan.py": "startup behaviour",
    "utils/llm.py": "selects the local model backend",
    "core/runtime/environment.py": "environment reporting",
    "api/routes/trust.py": "surfaces a warning that DEV_MODE is on",
}
# `agents/graph.py` ("model routing") and `api/routes/chat.py` ("already
# owner-gated") were listed here and were not that: the graph's read handed out
# owner clearance and the chat route's read switched a security gate off. Both
# now go through `dev_mode_active()` (tests/test_owner_gate_is_edition_gated.py).


def test_no_new_module_decides_authority_from_the_raw_dev_mode_flag():
    """The rule, across the tree, so the next call site inherits it.

    `if cfg.dev_mode` reads as harmless convenience — it is what four security
    call sites looked like. The guard is on the shape, not on a list of known
    offenders, and a genuinely non-authorising read has to say so out loud.
    """
    import pathlib
    import re

    ira = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in sorted(ira.rglob("*.py")):
        rel = path.relative_to(ira).as_posix()
        if rel.startswith(("tests/", "scripts/")) or rel == SECURITY_DEV_MODE_HOME:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if not re.search(r"(?<!\w)cfg\.dev_mode|settings\.dev_mode", text):
            continue
        if rel not in NON_SECURITY:
            offenders.append(rel)

    assert not offenders, (
        f"these modules read Settings.dev_mode directly: {offenders}. If the "
        f"read decides identity, authority or a security gate, route it through "
        f"`dev_mode_active()` so it is edition-gated. If it decides something "
        f"else, add it to NON_SECURITY with the reason — do not add an "
        f"authorising read to that list to silence this.")


def test_the_guard_can_see_the_shape_it_forbids():
    """A regex over source that matched nothing would be permanently green."""
    import re

    planted = "    if cfg.dev_mode:\n        return cfg.ira_admin_username"
    assert re.search(r"(?<!\w)cfg\.dev_mode", planted)


def test_ordinary_credentials_still_work_in_private(private, monkeypatch):
    """The whole point of the gate is that it changes nothing for the real
    single-owner login path."""
    from api.middleware.auth import hash_password
    import api.middleware.auth as auth

    monkeypatch.setattr(
        auth, "get_settings",
        lambda: types.SimpleNamespace(dev_mode=False, ira_admin_username="admin"))
    monkeypatch.setattr(auth, "_admin_password_hash",
                        lambda: hash_password("the-real-password"))

    assert authenticate_user("admin", "the-real-password") is True
    assert authenticate_user("admin", "wrong") is False
    assert authenticate_user("someone-else", "the-real-password") is False
    assert is_owner("admin") is True
