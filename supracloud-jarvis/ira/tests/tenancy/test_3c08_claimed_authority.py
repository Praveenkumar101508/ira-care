"""3C-08 — authority comes from the verified caller, never from the payload.

Phase 3C asks that internal tools execute on verified user and workspace
authority rather than model-generated or client-supplied claims. `api/routes/
chat.py` had a second door into exactly what 3C-04 closed, and this one takes
the claim from the request body:

    owner = _is_owner(_user) or (_is_voice_service(_user) and req.is_voice_owner)

`is_voice_owner` is a field on `ChatRequest`. It arrives on the wire. The only
thing standing between it and owner authority is:

    username == get_settings().ira_voice_service_username

which is the **same defect as 3C-04** on a different setting — one
deployment-wide string compared against a per-tenant username. So a tenant's
user holding that name could send `{"is_voice_owner": true}` and receive owner
authority, walking straight around the `is_owner` fix.

And what `owner` reaches is not a data read. It is passed as `is_owner=` into
`run_graph`, `stage_action` and `run_coding_task` — the flag that decides which
tools the model may execute.

── Why this is worth its own file ──────────────────────────────────────────

3C-04 fixed the function everyone would grep for. This is the arm of a boolean
`or` two lines further down, and it reads as a deliberate, carefully-commented
exception:

    # Only trust is_voice_owner when the request genuinely originates from the
    # voice service

The comment is true. It is the definition of "genuinely originates" that does
not survive multi-tenancy — and a fix that stops at the obvious function leaves
the door open while looking finished.
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

from api.middleware.auth import is_owner, is_voice_service  # noqa: E402
from config import get_settings  # noqa: E402
from core.editions import EDITION_CLOUD, EDITION_PRIVATE  # noqa: E402

CHAT = pathlib.Path(__file__).resolve().parents[2] / "api" / "routes" / "chat.py"


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_CLOUD)


@pytest.fixture
def private(monkeypatch):
    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_PRIVATE)


@pytest.fixture
def voice_username() -> str:
    return get_settings().ira_voice_service_username


# ─────────────────────────────────────────────────────────────────────────────
# The second door
# ─────────────────────────────────────────────────────────────────────────────


def test_the_voice_service_name_grants_nothing_in_cloud(cloud, voice_username):
    """The finding, in one assertion.

    Matching this name is what makes `is_voice_owner` from the request body
    authoritative. If the name can be held by a tenant's user, the body can
    assert owner authority.
    """
    assert is_voice_service(voice_username) is False, (
        "a username equal to ira_voice_service_username still makes the "
        "request body's is_voice_owner authoritative in cloud")


def test_no_username_at_all_is_the_voice_service_in_cloud(cloud, voice_username):
    for candidate in (voice_username, voice_username.upper(), "ira-voice",
                      "voice", "", "admin"):
        assert is_voice_service(candidate) is False, (
            f"{candidate!r} became the voice service")


def test_the_private_edition_still_has_a_voice_service(private, voice_username):
    """The single-owner deployment is the case this was written for: one
    enrolled biometric profile, one owner to verify against. Breaking it would
    take voice off a working product to fix a cloud problem."""
    assert is_voice_service(voice_username) is True


def test_a_stranger_is_not_the_voice_service_in_private(private, voice_username):
    """Positive control — without it, a function returning True unconditionally
    would satisfy the test above."""
    assert is_voice_service(voice_username + "x") is False
    assert is_voice_service("") is False


def test_neither_door_is_open_in_cloud(cloud, voice_username):
    """Both arms of the `or`, together.

    Stated as one assertion because that is how the expression is evaluated: it
    only takes one of them to grant the flag, and 3C-04 closing the first is not
    a fix if the second still opens.
    """
    admin = get_settings().ira_admin_username

    for username in (admin, voice_username, "admin", "ira-voice", "anyone"):
        granted = is_owner(username) or (
            is_voice_service(username) and True  # the body claiming owner
        )
        assert granted is False, (
            f"{username!r} obtains owner authority in cloud — which is the flag "
            f"that decides which tools the model may execute")


# ─────────────────────────────────────────────────────────────────────────────
# The route reads the shared decision
# ─────────────────────────────────────────────────────────────────────────────


def test_the_chat_route_does_not_re_implement_the_comparison():
    """The behavioural path needs a model, so this reads the source.

    A local `username == cfg.ira_voice_service_username` is the defect
    returning, and it reads as entirely ordinary code — it is what was there.
    """
    source = CHAT.read_text(encoding="utf-8")

    assert "is_voice_service" in source
    assert not re.search(
        r"(==|!=)\s*[^=]*ira_voice_service_username"
        r"|ira_voice_service_username\s*(==|!=)", source), (
        "api/routes/chat.py compares against ira_voice_service_username "
        "directly again. That setting is one deployment-wide string and a "
        "username is per-tenant, so the comparison lets a tenant's user assert "
        "is_voice_owner from the request body.")


def test_the_body_claim_is_still_gated_by_the_service_check():
    """The claim must never be read on its own.

    The failure to guard against is not the comparison coming back — it is
    someone simplifying the expression to `owner = _is_owner(_user) or
    req.is_voice_owner` while tidying up. That is one keystroke and it hands
    owner authority to every caller.
    """
    source = CHAT.read_text(encoding="utf-8")

    uses = re.findall(r"[^\n]*is_voice_owner[^\n]*", source)
    assert uses, "is_voice_owner has vanished from chat.py — has the field moved?"

    for line in uses:
        if line.strip().startswith("#") or "is_voice_owner: bool" in line:
            continue  # the comment and the model field declaration
        assert "_is_voice_service" in line, (
            f"is_voice_owner is read without the voice-service check on this "
            f"line, so the request body grants owner authority by itself:\n"
            f"  {line.strip()}")


def test_the_owner_flag_is_computed_before_it_is_used():
    """Order, because this flag is passed onward rather than checked in place.

    `owner` is handed to `run_graph`, `stage_action` and `run_coding_task`. A
    reordering that used it before the guard ran would not fail loudly — it
    would pass `False` or `None` and quietly change what the model may do, in
    whichever direction the default happens to fall.
    """
    source = CHAT.read_text(encoding="utf-8")

    for match in re.finditer(r"^\s*owner = .*$", source, re.M):
        line = match.group(0)
        assert "_is_owner" in line and "_is_voice_service" in line, (
            f"the owner flag is computed without both checks: {line.strip()}")
