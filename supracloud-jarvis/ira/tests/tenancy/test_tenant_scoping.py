"""Phase 2 — application-side tenant scoping.

Companion to test_tenant_isolation_rls.py. That suite proves the DATABASE
refuses cross-tenant access; this one proves the application cannot get into a
state where it asks the database an unscoped question in the first place.

No database required — these are the guards that run before a connection is
opened, plus the namespacing for the stores (Redis, object storage) that have no
row-level security to fall back on.
"""
from __future__ import annotations

import uuid

import pytest

from core.editions import EDITION_PRIVATE
from core.identity import (
    MissingTenantContext,
    PRIVATE_TENANT_ID,
    Principal,
    assert_tenant_scoped,
    background_principal,
    for_each_tenant,
    object_path,
    redis_key,
    redis_user_key,
    system_principal,
    tenant_prefix,
)

TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _principal(tenant=TENANT_A, **kw) -> Principal:
    base = dict(
        user_id=uuid.uuid4(),
        tenant_id=tenant,
        session_id=uuid.uuid4(),
        edition=EDITION_PRIVATE,
    )
    base.update(kw)
    return Principal(**base)  # type: ignore[arg-type]


# ── The scoping guard ────────────────────────────────────────────────────────

def test_assert_tenant_scoped_accepts_a_real_principal():
    p = _principal()
    assert assert_tenant_scoped(p) is p


def test_assert_tenant_scoped_rejects_none():
    with pytest.raises(MissingTenantContext):
        assert_tenant_scoped(None)


@pytest.mark.asyncio
async def test_tenant_connection_refuses_without_a_principal():
    """Fail closed before a connection is even acquired."""
    from core.identity.db_context import tenant_connection

    with pytest.raises(MissingTenantContext):
        async with tenant_connection(None):  # type: ignore[arg-type]
            pass


# ── Redis namespacing ────────────────────────────────────────────────────────

def test_redis_key_includes_the_tenant():
    key = redis_key(_principal(), "session", "abc")
    assert str(TENANT_A) in key
    assert key.startswith("ira:t:")


def test_redis_keys_differ_between_tenants():
    """The whole point: two tenants must not collide on the same key.

    Redis has no RLS — a flat keyspace returns whatever key you ask for, so the
    namespace IS the boundary here."""
    a = redis_key(_principal(TENANT_A), "session", "same-id")
    b = redis_key(_principal(TENANT_B), "session", "same-id")
    assert a != b


def test_redis_user_key_is_scoped_within_the_tenant():
    p = _principal()
    key = redis_user_key(p, "draft")
    assert str(p.tenant_id) in key
    assert str(p.user_id) in key


def test_redis_user_keys_differ_between_users_in_one_tenant():
    a = redis_user_key(_principal(user_id=uuid.uuid4()), "draft")
    b = redis_user_key(_principal(user_id=uuid.uuid4()), "draft")
    assert a != b


def test_redis_key_requires_a_principal():
    with pytest.raises(MissingTenantContext):
        redis_key(None, "x")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["../other", "a b", "key*", "a/b", "", "..", "."])
def test_redis_key_rejects_unsafe_segments(bad):
    """Rejected rather than escaped: a key whose safety depends on the escaper
    being correct at every call site is not safe."""
    with pytest.raises((ValueError, MissingTenantContext)):
        redis_key(_principal(), bad)


def test_tenant_prefix_matches_only_that_tenant():
    prefix = tenant_prefix(_principal(TENANT_A))
    assert str(TENANT_A) in prefix
    assert str(TENANT_B) not in prefix


# ── Object storage namespacing ───────────────────────────────────────────────

def test_object_path_is_under_the_tenant_prefix():
    path = object_path(_principal(), "avatars", "head.glb")
    assert path.startswith(f"tenants/{TENANT_A}/")


def test_object_paths_differ_between_tenants():
    a = object_path(_principal(TENANT_A), "export.zip")
    b = object_path(_principal(TENANT_B), "export.zip")
    assert a != b


@pytest.mark.parametrize(
    "bad", ["../escape", "..", "a/../../b", "./x"]
)
def test_object_path_rejects_traversal(bad):
    """Path traversal across a tenant boundary is a cross-tenant read wearing a
    filename."""
    with pytest.raises(ValueError):
        object_path(_principal(), bad)


def test_object_path_requires_a_principal():
    with pytest.raises(MissingTenantContext):
        object_path(None, "x")  # type: ignore[arg-type]


# ── Background work ──────────────────────────────────────────────────────────

def test_background_principal_requires_a_tenant():
    """Background work never runs unscoped."""
    with pytest.raises(MissingTenantContext):
        background_principal(None, job="nightly")  # type: ignore[arg-type]


def test_background_principal_is_not_the_owner():
    p = background_principal(TENANT_A, job="nightly")
    assert p.is_owner is False
    assert p.scopes == frozenset()
    assert p.roles == frozenset()
    assert p.username == ""


def test_background_principal_is_bound_to_one_tenant():
    p = background_principal(TENANT_A, job="nightly")
    assert p.tenant_id == TENANT_A


def test_background_principal_is_stable_per_job():
    """Audit rows must attribute consistently to the job that wrote them."""
    a = background_principal(TENANT_A, job="briefing")
    b = background_principal(TENANT_B, job="briefing")
    assert a.user_id == b.user_id       # same job identity
    assert a.tenant_id != b.tenant_id   # different tenant scope


def test_different_jobs_get_different_identities():
    a = background_principal(TENANT_A, job="briefing")
    b = background_principal(TENANT_A, job="cleanup")
    assert a.user_id != b.user_id


@pytest.mark.asyncio
async def test_for_each_tenant_yields_one_scoped_principal_per_tenant():
    """Cross-tenant work is N scoped passes, never one unscoped pass."""
    seen = []
    async for p in for_each_tenant([TENANT_A, TENANT_B], job="sweep"):
        seen.append(p.tenant_id)
        assert p.is_owner is False
    assert seen == [TENANT_A, TENANT_B]


# ── System principal ─────────────────────────────────────────────────────────

def test_system_principal_defaults_to_the_private_tenant():
    assert system_principal().tenant_id == PRIVATE_TENANT_ID


# ── Auth provider abstraction ────────────────────────────────────────────────

def test_authenticated_subject_requires_a_subject():
    from core.identity import AuthenticatedSubject, AuthMethod

    with pytest.raises(ValueError):
        AuthenticatedSubject(subject="", method=AuthMethod.PASSWORD)


@pytest.mark.asyncio
async def test_password_provider_authenticates_the_owner(monkeypatch):
    from core.identity import AuthMethod, PasswordAuthProvider

    monkeypatch.setattr("api.middleware.auth.authenticate_user", lambda u, p: True)
    subject = await PasswordAuthProvider().authenticate(
        {"username": "admin", "password": "correct"}
    )
    assert subject.subject == "admin"
    assert subject.method is AuthMethod.PASSWORD


@pytest.mark.asyncio
async def test_password_provider_raises_rather_than_returning_none(monkeypatch):
    """A None return reads as success at a careless call site."""
    from core.identity import AuthenticationFailed, PasswordAuthProvider

    monkeypatch.setattr("api.middleware.auth.authenticate_user", lambda u, p: False)
    with pytest.raises(AuthenticationFailed):
        await PasswordAuthProvider().authenticate({"username": "admin", "password": "wrong"})


@pytest.mark.asyncio
async def test_password_provider_rejects_empty_credentials():
    from core.identity import AuthenticationFailed, PasswordAuthProvider

    with pytest.raises(AuthenticationFailed):
        await PasswordAuthProvider().authenticate({})


def test_authentication_carries_no_tenant():
    """Authentication proves identity; it must not decide a tenant. Otherwise a
    provider compromise grants access to tenants the user never joined."""
    from core.identity import AuthenticatedSubject, AuthMethod

    subject = AuthenticatedSubject(subject="u1", method=AuthMethod.GOOGLE)
    assert not hasattr(subject, "tenant_id")


# ── Token signing policy ─────────────────────────────────────────────────────

def test_shared_secret_signing_is_refused_for_cloud():
    """One symmetric secret means every verifier can also mint tokens for every
    tenant, and rotation logs everyone out at once."""
    from core.identity import TokenSigningPolicy, assert_signing_policy_acceptable

    with pytest.raises(RuntimeError, match="not acceptable for the cloud"):
        assert_signing_policy_acceptable("cloud", TokenSigningPolicy.SHARED_HS256)


def test_shared_secret_signing_is_fine_for_private():
    from core.identity import TokenSigningPolicy, assert_signing_policy_acceptable

    assert_signing_policy_acceptable("private", TokenSigningPolicy.SHARED_HS256)


@pytest.mark.parametrize(
    "policy_name", ["ASYMMETRIC_ROTATING", "MANAGED_OIDC"]
)
def test_acceptable_cloud_signing_policies(policy_name):
    from core.identity import TokenSigningPolicy, assert_signing_policy_acceptable

    assert_signing_policy_acceptable("cloud", getattr(TokenSigningPolicy, policy_name))
