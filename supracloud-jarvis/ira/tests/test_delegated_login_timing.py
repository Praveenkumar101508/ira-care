"""P2-01 — the delegated login path must not answer faster for a name that
does not exist.

`auth/routes.py::login` tries two credential checks in order:

    _authed = authenticate_user(form.username, form.password)          # owner
    if not _authed:
        _authed = await authenticate_delegated(form.username, form.password)

The owner path is deliberately timing-uniform. `authenticate_user` carries this
docstring and means it:

    Always runs bcrypt regardless of whether the username matches to prevent
    username enumeration via response-time differences.

It does that with `_dummy_hash()` — a real bcrypt hash it verifies against when
the username does not match, so the work performed is the same either way.

The delegated fallback did not. `authenticate_delegated` returned early on a
missing or inactive row, **before** `verify_password`, so:

    an existing, active delegated account  → DB round-trip + bcrypt verify
    a name with no row (or an inactive one) → DB round-trip only

bcrypt at the configured cost is orders of magnitude more expensive than the
round-trip, so the difference is measurable from outside and it answers the one
question a login endpoint must never answer: *does this account exist?*

That matters more here than on a typical login form. Delegated accounts are
people the owner has explicitly granted access to; enumerating them maps the
owner's circle of trust, and it does so through the ordinary public login
endpoint with no authentication at all.

── What these tests assert, and what they do not ───────────────────────────

They assert **the work performed**, not the wall-clock. A timing assertion in
CI is a flaky test that fails under load and passes under a bug, so these count
bcrypt invocations instead: the observable proxy for the expensive operation,
measured deterministically.

They therefore do not, on their own, establish end-to-end timing uniformity of
the HTTP endpoint — see the note in
`docs/cloud/PHASE_2_AUTH_INVENTORY.md`. What they do establish is that the
*asymmetry in performed work* is gone, which is the part that lives in this
module and the part that was wrong.
"""
from __future__ import annotations

import contextlib
import os
import uuid
from datetime import datetime, timezone

import pytest

# `Settings` requires these; this module reads `ira_admin_username` directly.
for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from security import delegated as deleg


def _row(**over):
    base = {
        "id": uuid.uuid4(), "username": "anitha", "password_hash": "x",
        "role": "trusted_user", "person_id": None, "is_primary_owner": False,
        "active": True, "voice_enrolled": False,
        "created_at": datetime.now(timezone.utc), "updated_at": None,
    }
    base.update(over)
    return base


def _member(**over):
    """A membership-model account (granted since 092), as the store reads it."""
    from core.identity.principal import private_user_id
    base = {
        "user_id": private_user_id("anitha"), "username": "anitha",
        "role": "trusted_user", "person_id": None, "membership_grants_access": True,
        "user_status": "active", "deleted_at": None,
        "created_at": datetime.now(timezone.utc),
    }
    base.update(over)
    return base


class _FakeConn:
    """`row` answers the legacy table; `member`/`credential` the membership model.

    The store asks the membership model first and falls back to
    `delegated_users` only when the name has no membership account, so a test
    seeding only a legacy row gets "no membership account" first, as a real
    database holding only that row would answer.
    """

    def __init__(self, row=None, member=None, credential=None):
        self.row = row
        self.member = member
        self.credential = credential

    async def fetchrow(self, sql, *args):
        return self.member if "tenant_memberships" in sql else self.row

    async def fetchval(self, sql, *args):
        return self.credential if "user_credentials" in sql else None

    async def execute(self, sql, *args):
        return None


def _patch_db(monkeypatch, conn):
    """The store reads accounts on a connection bound to the private tenant and
    to the account (`_account_bound`), because both tables are FORCE RLS."""
    @contextlib.asynccontextmanager
    async def fake_account_bound(_user_id):
        yield conn

    monkeypatch.setattr(deleg, "_account_bound", fake_account_bound)


@pytest.fixture
def bcrypt_calls(monkeypatch):
    """Count `verify_password` invocations inside the delegated module.

    Patched where `authenticate_delegated` looks it up — it imports the symbol
    inside the function body, so the patch has to land on the source module.
    """
    calls: list[tuple[str, str]] = []
    import api.middleware.auth as auth

    real = auth.verify_password

    def counting(plain, hashed):
        calls.append((plain, hashed))
        return real(plain, hashed)

    monkeypatch.setattr(auth, "verify_password", counting)
    return calls


# ─────────────────────────────────────────────────────────────────────────────
# The asymmetry
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_nonexistent_delegated_account_still_costs_a_hash(
    monkeypatch, bcrypt_calls
):
    """The finding, in one assertion.

    No row at all. Before the fix this returned immediately after the query and
    performed zero bcrypt work, so the response came back visibly sooner than
    for a real account.
    """
    _patch_db(monkeypatch, _FakeConn(row=None))

    assert await deleg.authenticate_delegated("nobody-here", "whatever") is False
    assert len(bcrypt_calls) == 1, (
        "a login attempt against a nonexistent delegated account performed no "
        "password hash, so it returns measurably faster than one against a real "
        "account — that difference is an account-existence oracle on a public, "
        "unauthenticated endpoint")


async def test_an_inactive_delegated_account_still_costs_a_hash(
    monkeypatch, bcrypt_calls
):
    """A revoked account is the sharper case.

    The row exists, so 'does this name exist' and 'is this name still allowed
    in' are two different questions — and a fast refusal answers the second one
    for free, telling an attacker exactly whose access was removed and when.
    """
    _patch_db(monkeypatch, _FakeConn(row=_row(active=False)))

    assert await deleg.authenticate_delegated("anitha", "her-own-secret-1") is False
    assert len(bcrypt_calls) == 1, (
        "an inactive delegated account is refused without performing a hash")


async def test_an_active_account_costs_exactly_one_hash(monkeypatch, bcrypt_calls):
    """The control, and the reason the count is `== 1` rather than `>= 1`.

    If the fix hashed twice on the success path, the two branches would be
    unequal again — in the other direction, and just as measurable.
    """
    from api.middleware.auth import hash_password

    _patch_db(monkeypatch, _FakeConn(
        row=_row(password_hash=hash_password("her-own-secret-1"))))

    assert await deleg.authenticate_delegated("anitha", "her-own-secret-1") is True
    assert len(bcrypt_calls) == 1


async def test_a_wrong_password_costs_exactly_one_hash(monkeypatch, bcrypt_calls):
    from api.middleware.auth import hash_password

    _patch_db(monkeypatch, _FakeConn(
        row=_row(password_hash=hash_password("her-own-secret-1"))))

    assert await deleg.authenticate_delegated("anitha", "wrong-password") is False
    assert len(bcrypt_calls) == 1


async def test_a_suspended_membership_account_still_costs_a_hash(
    monkeypatch, bcrypt_calls
):
    """The revoked-account case for an account granted since 092.

    Revoking suspends the membership. The credential row stays, so a fast
    refusal here would say exactly whose access was removed, as above.
    """
    from api.middleware.auth import hash_password

    _patch_db(monkeypatch, _FakeConn(
        member=_member(membership_grants_access=False),
        credential=hash_password("her-own-secret-1")))

    assert await deleg.authenticate_delegated("anitha", "her-own-secret-1") is False
    assert len(bcrypt_calls) == 1


async def test_an_active_membership_account_costs_exactly_one_hash(
    monkeypatch, bcrypt_calls
):
    from api.middleware.auth import hash_password

    _patch_db(monkeypatch, _FakeConn(
        member=_member(), credential=hash_password("her-own-secret-1")))

    assert await deleg.authenticate_delegated("anitha", "her-own-secret-1") is True
    assert len(bcrypt_calls) == 1


async def test_every_refusal_reason_performs_the_same_work(
    monkeypatch, bcrypt_calls
):
    """The property stated once, across all four outcomes.

    Whatever the reason a delegated login fails — no row, inactive row, wrong
    password — the module must have done the same amount of expensive work by
    the time it answers.
    """
    from api.middleware.auth import hash_password

    good = hash_password("her-own-secret-1")
    cases = {
        "no row":         _FakeConn(row=None),
        "inactive row":   _FakeConn(row=_row(active=False, password_hash=good)),
        "wrong password": _FakeConn(row=_row(password_hash=good)),
        # The same three outcomes for an account granted since 092.
        "suspended membership": _FakeConn(
            member=_member(membership_grants_access=False), credential=good),
        "membership, no credential": _FakeConn(member=_member(), credential=None),
        "membership, wrong password": _FakeConn(member=_member(), credential=good),
    }

    counts = {}
    for label, conn in cases.items():
        bcrypt_calls.clear()
        _patch_db(monkeypatch, conn)
        assert await deleg.authenticate_delegated("anitha", "wrong-password") is False
        counts[label] = len(bcrypt_calls)

    assert len(set(counts.values())) == 1, (
        f"delegated login failures perform different amounts of work depending "
        f"on why they failed: {counts}. Each distinct cost is a distinguishable "
        f"answer to a question the endpoint should not answer.")


# ─────────────────────────────────────────────────────────────────────────────
# The cases that must NOT hash
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_owner_username_is_refused_without_a_delegated_hash(
    monkeypatch, bcrypt_calls
):
    """The primary owner never authenticates through this path.

    This refusal is not an enumeration risk: the owner's username is a
    deployment setting, not a secret, and `authenticate_user` has already run a
    full bcrypt verification against it on the line above the caller. Adding a
    second hash here would double the cost of every owner login to hide
    something that is not hidden.
    """
    from config import get_settings

    _patch_db(monkeypatch, _FakeConn(row=None))

    assert await deleg.authenticate_delegated(
        get_settings().ira_admin_username, "anything") is False
    assert len(bcrypt_calls) == 0


async def test_an_empty_username_or_password_is_refused_without_a_hash(
    monkeypatch, bcrypt_calls
):
    """A blank field is a malformed request, not a guess at an account. It
    cannot distinguish anything, and hashing it would burn CPU on input that
    never reaches a lookup."""
    _patch_db(monkeypatch, _FakeConn(row=None))

    assert await deleg.authenticate_delegated("", "password") is False
    assert await deleg.authenticate_delegated("anitha", "") is False
    assert len(bcrypt_calls) == 0


async def test_a_database_failure_fails_closed_without_leaking(
    monkeypatch, bcrypt_calls
):
    """DB down must still refuse, and must not become a distinguishable state.

    Kept explicit because the pre-existing `except: return False` is what makes
    this fail closed, and a refactor that let the exception escape would turn a
    refusal into a 500 — which is itself a signal.
    """
    @contextlib.asynccontextmanager
    async def exploding_connection(_user_id):
        raise RuntimeError("database unreachable")
        yield  # pragma: no cover

    monkeypatch.setattr(deleg, "_account_bound", exploding_connection)

    assert await deleg.authenticate_delegated("anitha", "her-own-secret-1") is False
