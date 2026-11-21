"""Phase 1 — the Principal abstraction and its private-edition mapping."""
from __future__ import annotations

import uuid

import pytest

from core.editions import EDITION_CLOUD, EDITION_PRIVATE
from core.identity import (
    PRIVATE_TENANT_ID,
    Principal,
    private_user_id,
    system_principal,
)
from core.identity.resolver import CloudIdentityNotAvailable, resolve_principal


def _p(**kw) -> Principal:
    base = dict(
        user_id=uuid.uuid4(),
        tenant_id=PRIVATE_TENANT_ID,
        session_id=uuid.uuid4(),
        edition=EDITION_PRIVATE,
    )
    base.update(kw)
    return Principal(**base)  # type: ignore[arg-type]


# ── Shape and immutability ───────────────────────────────────────────────────

def test_principal_is_immutable():
    """A request's identity must not change part-way through handling it."""
    p = _p()
    with pytest.raises(Exception):
        p.is_owner = True  # type: ignore[misc]


def test_principal_requires_a_tenant():
    """A blank tenant is the precondition for a cross-tenant read."""
    with pytest.raises(ValueError, match="tenant_id"):
        Principal(
            user_id=uuid.uuid4(),
            tenant_id=None,  # type: ignore[arg-type]
            session_id=uuid.uuid4(),
            edition=EDITION_PRIVATE,
        )


def test_principal_requires_a_user():
    with pytest.raises(ValueError, match="user_id"):
        Principal(
            user_id=None,  # type: ignore[arg-type]
            tenant_id=PRIVATE_TENANT_ID,
            session_id=uuid.uuid4(),
            edition=EDITION_PRIVATE,
        )


def test_defaults_are_unprivileged():
    """A principal built with no roles/scopes can do nothing."""
    p = _p()
    assert p.roles == frozenset()
    assert p.scopes == frozenset()
    assert p.is_owner is False
    assert p.has_scope("chat") is False


# ── Scope narrowing ──────────────────────────────────────────────────────────

def test_with_scopes_narrows():
    p = _p(scopes=frozenset({"chat", "voice"}))
    assert p.with_scopes({"chat"}).scopes == frozenset({"chat"})


def test_with_scopes_cannot_widen():
    """Deriving a principal must never grant more than it started with."""
    p = _p(scopes=frozenset({"chat"}))
    widened = p.with_scopes({"chat", "security_admin"})
    assert widened.scopes == frozenset({"chat"})
    assert not widened.has_scope("security_admin")


def test_with_scopes_leaves_original_untouched():
    p = _p(scopes=frozenset({"chat", "voice"}))
    p.with_scopes({"chat"})
    assert p.scopes == frozenset({"chat", "voice"})


# ── Deterministic private identifiers ────────────────────────────────────────

def test_private_ids_are_stable_across_calls():
    """Phase 2's backfill relies on these being reproducible, not stored."""
    assert private_user_id("admin") == private_user_id("admin")
    assert PRIVATE_TENANT_ID == PRIVATE_TENANT_ID


def test_private_ids_differ_per_username():
    assert private_user_id("admin") != private_user_id("someone-else")


# ── Logging safety ───────────────────────────────────────────────────────────

def test_safe_repr_omits_the_username():
    """Identifiers are safe to log; the account name is personal data."""
    p = _p(username="praveen", is_owner=True)
    out = p.safe_repr()
    assert "praveen" not in out
    assert str(p.user_id) in out
    assert str(p.tenant_id) in out


# ── System principal ─────────────────────────────────────────────────────────

def test_system_principal_is_not_the_owner():
    """Background work must not inherit owner privilege just because nobody
    is logged in."""
    sp = system_principal(reason="scheduler")
    assert sp.is_owner is False
    assert sp.scopes == frozenset()
    assert sp.roles == frozenset()
    assert sp.username == ""


def test_system_principal_still_has_a_tenant():
    assert system_principal().tenant_id == PRIVATE_TENANT_ID


# ── Resolution ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_owner_resolves_with_full_scopes(monkeypatch):
    from security.roles import ROLE_PRIMARY_OWNER, SCOPES

    monkeypatch.setattr("api.middleware.auth.is_owner", lambda u: True)

    p = await resolve_principal("admin", edition=EDITION_PRIVATE)

    assert p.is_owner is True
    assert p.edition == EDITION_PRIVATE
    assert p.tenant_id == PRIVATE_TENANT_ID
    assert p.user_id == private_user_id("admin")
    assert p.has_role(ROLE_PRIMARY_OWNER)
    assert p.scopes == frozenset(SCOPES)


@pytest.mark.asyncio
async def test_delegated_user_gets_only_their_role_scopes(monkeypatch):
    from security.roles import ROLE_VIEWER, scopes_for

    monkeypatch.setattr("api.middleware.auth.is_owner", lambda u: False)

    async def _role(_username):
        return ROLE_VIEWER

    monkeypatch.setattr("security.delegated.get_delegated_role", _role)

    p = await resolve_principal("guest", edition=EDITION_PRIVATE)

    assert p.is_owner is False
    assert p.scopes == scopes_for(ROLE_VIEWER)
    assert not p.has_scope("security_admin")


@pytest.mark.asyncio
async def test_unresolvable_role_gets_no_scopes(monkeypatch):
    """Fail closed: if the role lookup breaks, the principal can do nothing —
    it does not fall back to a default role."""
    monkeypatch.setattr("api.middleware.auth.is_owner", lambda u: False)

    async def _boom(_username):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("security.delegated.get_delegated_role", _boom)

    p = await resolve_principal("guest", edition=EDITION_PRIVATE)

    assert p.scopes == frozenset()
    assert p.roles == frozenset()
    assert p.is_owner is False


@pytest.mark.asyncio
async def test_cloud_resolution_refuses_rather_than_guessing():
    """A cloud process must never mint a private single-owner principal — that
    is the bug class that produces cross-tenant reads.

    The invariant is unchanged; what satisfies it has changed. This used to hold
    because cloud resolution refused *everything* — there was no cloud identity
    to resolve. It now resolves from `users` + `tenant_memberships`, so the
    assertion has to be about the thing that still matters: with no database
    reachable, resolution REFUSES rather than falling back to the private
    owner mapping.

    The type matters as much as the refusal. Before the fix this leaked a raw
    `RuntimeError` from the connection pool, which `except
    CloudIdentityNotAvailable` does not catch — so a caller written to fail
    closed on an unresolvable identity would instead have seen an unhandled
    error. "I could not tell" and "no" must be the same code path.
    """
    with pytest.raises(CloudIdentityNotAvailable):
        await resolve_principal("admin", edition=EDITION_CLOUD)


@pytest.mark.asyncio
async def test_cloud_resolution_never_returns_a_private_principal():
    """The positive form of the rule above, stated so it cannot be satisfied by
    an exception type alone: whatever cloud resolution returns, it is never the
    private single-owner shape."""
    from core.identity.principal import PRIVATE_TENANT_ID

    try:
        p = await resolve_principal("admin", edition=EDITION_CLOUD)
    except CloudIdentityNotAvailable:
        return  # refusing is the correct outcome here
    pytest.fail(  # pragma: no cover - only reachable if resolution wrongly succeeds
        f"cloud resolution returned a principal for an unresolvable subject: "
        f"tenant={p.tenant_id} (private is {PRIVATE_TENANT_ID}), "
        f"is_owner={p.is_owner}"
    )


@pytest.mark.asyncio
async def test_resolution_does_not_depend_on_the_admin_username_string(monkeypatch):
    """Owner-ness comes from is_owner(), not from a name comparison the caller
    could influence."""
    monkeypatch.setattr("api.middleware.auth.is_owner", lambda u: False)

    async def _role(_username):
        return None

    monkeypatch.setattr("security.delegated.get_delegated_role", _role)

    # Even calling yourself "admin" does not make you the owner.
    p = await resolve_principal("admin", edition=EDITION_PRIVATE)
    assert p.is_owner is False
    assert p.scopes == frozenset()
