"""A stand-in for the PostgreSQL token-version store, for tests with no database.

Migration 096 moved the per-account token version ("log out everywhere") from
Redis to `auth_token_versions`, read and written only by
`core.identity.token_versions`. Tests that exercise the token path without a
database used to set the version by faking Redis; they set it here instead.

The real store is tested against real PostgreSQL in
`tests/tenancy/test_global_logout_durable.py`. This fake keeps only its
contract: one counter per account, read as-is, raised by one per bump, and a
failure raised as `TokenVersionUnavailable` when the store is down.

R08 put single-device sign-outs in PostgreSQL in both editions
(`core.identity.f1_revocation`, tested for real in
`tests/tenancy/test_single_logout_durable.py`). The fake holds them too: a set
of signed-out token ids, recorded once, read as-is, and unreachable when the
store is down. `state["revoked"]` is that set.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def token_version_store(version: int | str | None = 0, *, unavailable: bool = False):
    """Patch the store for the duration. Yields a dict: `state["version"]`.

    `version` is what the account's row holds (None or "" reads as 0, as a
    missing row does). With `unavailable=True` every read and bump raises
    `TokenVersionUnavailable`, which the auth layer answers with 503.
    """
    from core.identity import f1_revocation as f1
    from core.identity import token_versions as tv

    state = {"version": int(version or 0), "bumps": 0, "revoked": set()}

    async def current(user_id):
        if unavailable:
            raise tv.TokenVersionUnavailable("synthetic: the store is down")
        return state["version"]

    async def bump(user_id):
        if unavailable:
            raise tv.TokenVersionUnavailable("synthetic: the store is down")
        state["version"] += 1
        state["bumps"] += 1
        return state["version"]

    async def resolve_user_id(sub, uid=None):
        return uuid.UUID(str(uid)) if uid else uuid.uuid5(uuid.NAMESPACE_URL, f"test-account:{sub}")

    async def record_revocation(jti):
        if unavailable:
            raise ConnectionRefusedError("synthetic: the store is down")
        state["revoked"].add(jti)

    async def is_revoked(jti):
        if unavailable:
            raise ConnectionRefusedError("synthetic: the store is down")
        return jti in state["revoked"]

    with patch.object(tv, "current", current), \
            patch.object(tv, "current_for_login", current), \
            patch.object(tv, "bump", bump), \
            patch.object(tv, "resolve_user_id", resolve_user_id), \
            patch.object(f1, "record_revocation", record_revocation), \
            patch.object(f1, "is_revoked", is_revoked):
        yield state
