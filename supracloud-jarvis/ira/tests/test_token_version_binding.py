"""P2-04 — "revoke all my sessions" must actually revoke all of them.

`/auth/logout/all` bumps a per-user counter in Redis. `require_auth` rejects any
access token whose `ver` claim is below the current counter, so one INCR
invalidates every access token the user holds. That is the whole account-recovery
story: it is what a stolen token, a shared laptop or a removed account is
answered with.

It did not work, because of what the refresh token is:

    _make_refresh_token()      payload = {sub, exp, jti, tok}     ← no `ver`
    POST /auth/refresh         checks tok == "refresh", checks jti revocation,
                               then issues a NEW access token stamped with the
                               CURRENT version

So the sequence is:

    1. attacker holds alice's refresh token
    2. alice revokes everything — every access token in existence dies
    3. attacker POSTs the refresh token to /auth/refresh
    4. attacker receives an access token stamped with the *new* version

Step 4 is the part that matters. The replacement token is not a leftover that
slipped through — it is stamped current, so it is indistinguishable from one
issued by a fresh login, and it keeps working until the refresh token expires
(`refresh_token_expire_days`, seven days by default). Revoke-all revoked the
short-lived credentials and left the long-lived one alone.

── Why the fix is a signature change and not a check ───────────────────────

The obvious repair is to compare the refresh token's version at /auth/refresh.
That fixes this instance. It does not fix the class: `_make_access_token` took
`ver: int = 0`, so *any* future caller that forgets the argument mints a token
that silently claims version zero, and `create_token` — public, exported, and
with no access to Redis — did exactly that on purpose.

A default value is what made an unversioned token expressible. So `ver` is now
required and keyword-only on every constructor, and the version check lives in
one function used by both verification paths. A caller that forgets it gets a
TypeError, not a token.

── Unversioned tokens are rejected in every edition, not just cloud ───────

The requirement is that a missing, malformed or stale version is rejected in
cloud. Rejecting it everywhere is stricter, and the alternative — accepting an
unversioned token in the private edition — would mean an edition branch inside
the verification path, which is the shape that produced P2-02. The one-time cost
is that refresh tokens issued before this change stop working and their holder
logs in again.

── Since migration 096 ──────────────────────────────────────────────────────

The counter now lives in PostgreSQL (`auth_token_versions`), not Redis. These
tests set it with `tests/token_version_store.py` where they used to fake Redis,
and keep a Redis double for the per-token (jti) check, which still uses Redis.
The real store is tested on real PostgreSQL in
`tests/tenancy/test_global_logout_durable.py`.
"""
from __future__ import annotations

import inspect
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from tests.token_version_store import token_version_store

_REQUIRED_ENV = {
    "IRA_SECRET_KEY": "ci-test-secret-key-for-jwt",
    "IRA_ADMIN_PASSWORD": "ci-test-password",
    "POSTGRES_PASSWORD": "ci-db-pass",
    "REDIS_PASSWORD": "ci-redis-pass",
    "VLLM_API_KEY": "ci-vllm-key",
    "DEV_MODE": "false",
}


@pytest.fixture(autouse=True)
def env():
    """Real settings, dev mode off — these tests exercise the actual token path."""
    from config import get_settings

    with patch.dict(os.environ, _REQUIRED_ENV, clear=False):
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()


def _redis(*, version=None, revoked=0):
    """A Redis double. `version` is what `ira:token_ver:{user}` holds."""
    r = AsyncMock()
    r.exists = AsyncMock(return_value=revoked)
    r.get = AsyncMock(return_value=version)
    r.incr = AsyncMock(return_value=1)
    r.setex = AsyncMock(return_value=True)
    return r


def _claims(token: str) -> dict:
    import jwt

    from config import get_settings

    return jwt.decode(token, get_settings().ira_secret_key, algorithms=["HS256"])


def _sign(**claims) -> str:
    """Mint a token directly, bypassing the constructors.

    Needed to express payloads the constructors must refuse to produce — a
    missing `ver`, a malformed one — signed with the real key so the signature
    check passes and the *claim* check is what decides.
    """
    import jwt

    from config import get_settings

    payload = {
        "sub": "alice",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
        "jti": str(uuid.uuid4()),
        "tok": "access",
        **claims,
    }
    return jwt.encode(payload, get_settings().ira_secret_key, algorithm="HS256")


def _creds(token: str):
    from fastapi.security import HTTPAuthorizationCredentials

    return HTTPAuthorizationCredentials(scheme="bearer", credentials=token)


@pytest.fixture
def client(monkeypatch):
    """An app carrying only the real auth routes.

    Rate limiting is disabled rather than mocked: the limits are real behaviour
    tested elsewhere, and leaving them on would make these tests depend on how
    many requests the preceding ones happened to make.

    `alice` stands for an active household account. The private edition's
    refresh also refuses an account that is not active; that check has its own
    tests (tests/tenancy/test_delegated_account_boundary.py). Without this stub
    every refresh here would be refused by it, and the negative tests below
    would pass whether or not the version check worked.
    """
    async def _active(_username):
        return "trusted_user"

    monkeypatch.setattr("security.delegated.get_delegated_role", _active)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded

    from auth.routes import register_auth_routes

    limiter = Limiter(key_func=lambda _r: "test", enabled=False)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, lambda _r, _e: None)
    register_auth_routes(app, limiter)
    return TestClient(app)


# ─────────────────────────────────────────────────────────────────────────────
# The finding
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_refresh_token_carries_a_version_claim():
    """Without the claim there is nothing for revoke-all to compare against."""
    from api.middleware.auth import _make_refresh_token

    token, _, _ = _make_refresh_token("alice", ver=3)
    assert _claims(token)["ver"] == 3, (
        "the refresh token has no version claim, so it cannot be invalidated by "
        "a version bump — and it is the longest-lived credential IRA issues")


def test_revoke_all_kills_the_refresh_token(client):
    """The finding, end to end, through the real endpoint.

    Version 0 at issue, version 1 at presentation: the token predates the
    revocation and must not be exchangeable.
    """
    from api.middleware.auth import _make_refresh_token

    refresh, _, _ = _make_refresh_token("alice", ver=0)

    with patch("utils.redis_client.get_redis", return_value=_redis()), token_version_store(1):
        r = client.post("/auth/refresh", headers={"Authorization": f"Bearer {refresh}"})

    assert r.status_code == 401, (
        "a refresh token issued before revoke-all was still exchanged for a "
        "working access token — the revocation applied to the short-lived "
        "credentials and left the long-lived one alone")


@pytest.mark.asyncio
async def test_the_token_minted_after_revoke_all_is_not_merely_stale(client):
    """Why the previous test is not a formality.

    The access token /auth/refresh hands back is stamped with the version that
    is current *now*, so it does not look like a leftover to any subsequent
    check. If the exchange is allowed, the replacement is fully valid — this
    test proves the consequence rather than asserting only the status code.
    """
    from api.middleware.auth import _make_refresh_token, require_auth

    refresh, _, _ = _make_refresh_token("alice", ver=0)

    with patch("utils.redis_client.get_redis", return_value=_redis()), token_version_store(1):
        r = client.post("/auth/refresh", headers={"Authorization": f"Bearer {refresh}"})
        if r.status_code == 200:
            replacement = r.json()["access_token"]
            assert _claims(replacement)["ver"] == 1
            accepted = await require_auth(_creds(replacement))
            pytest.fail(
                f"revoke-all was fully defeated: the refresh token minted a "
                f"replacement that require_auth accepted as {accepted!r}")

    assert r.status_code == 401


def test_a_current_refresh_token_still_works(client):
    """Positive control.

    A fix that rejected every refresh would pass every negative assertion here
    and break the refresh flow completely.
    """
    from api.middleware.auth import _make_refresh_token

    refresh, _, _ = _make_refresh_token("alice", ver=2)

    with patch("utils.redis_client.get_redis", return_value=_redis()), token_version_store(2):
        r = client.post("/auth/refresh", headers={"Authorization": f"Bearer {refresh}"})

    assert r.status_code == 200, r.text
    assert _claims(r.json()["access_token"])["ver"] == 2


def test_a_refresh_token_ahead_of_the_counter_is_accepted(client):
    """`>` not `!=`, deliberately.

    A token whose version is *higher* than the stored counter is not evidence of
    revocation — it is what a lost or reset Redis looks like. Rejecting it would
    turn a cache eviction into a forced logout for everybody.
    """
    from api.middleware.auth import _make_refresh_token

    refresh, _, _ = _make_refresh_token("alice", ver=5)

    with patch("utils.redis_client.get_redis", return_value=_redis()), token_version_store(2):
        r = client.post("/auth/refresh", headers={"Authorization": f"Bearer {refresh}"})

    assert r.status_code == 200, r.text


# ─────────────────────────────────────────────────────────────────────────────
# The class: an unversioned token must not be expressible
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name", ["_make_access_token", "_make_refresh_token", "create_token"])
def test_no_constructor_will_mint_a_token_without_a_version(name):
    """`ver: int = 0` is what made an unversioned token possible.

    Required, and keyword-only: positional would let a future caller pass the
    wrong integer silently, and a default lets them pass none at all.
    """
    import api.middleware.auth as auth

    fn = getattr(auth, name)
    param = inspect.signature(fn).parameters.get("ver")

    assert param is not None, f"{name} takes no version at all"
    assert param.default is inspect.Parameter.empty, (
        f"{name} defaults `ver` to {param.default!r}, so a caller that forgets "
        f"it mints a token silently claiming that version")
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        f"{name} accepts `ver` positionally")

    with pytest.raises(TypeError):
        fn("alice")


def test_create_token_does_not_invent_a_version():
    """It is synchronous and has no Redis, so it cannot resolve the authoritative
    value — which is exactly why it must not supply one.

    The caller has to have looked it up. That is a worse API and a correct one:
    the alternative is a public, exported function that mints tokens claiming a
    version nobody checked.
    """
    from api.middleware.auth import create_token

    resp = create_token("alice", ver=7)
    assert _claims(resp.access_token)["ver"] == 7


@pytest.mark.asyncio
async def test_login_resolves_the_version_rather_than_assuming_it():
    """Both tokens from a login carry the version that was authoritative at the
    moment of issue — not zero, and not each other's."""
    from api.middleware.auth import create_login_tokens

    with patch("utils.redis_client.get_redis", return_value=_redis()), token_version_store(4):
        resp = await create_login_tokens("alice")

    assert _claims(resp.access_token)["ver"] == 4
    assert _claims(resp.refresh_token)["ver"] == 4


# ─────────────────────────────────────────────────────────────────────────────
# Missing and malformed version claims
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_access_token_with_no_version_claim_is_rejected():
    """A signed token that simply omits `ver`.

    While the model defaulted the field to 0 this decoded as "version zero" —
    a claim the issuer never made, treated as one it did.
    """
    from fastapi import HTTPException

    from api.middleware.auth import require_auth

    token = _sign()  # no ver
    with patch("utils.redis_client.get_redis", return_value=_redis()), token_version_store(0):
        with pytest.raises(HTTPException) as exc:
            await require_auth(_creds(token))
    assert exc.value.status_code == 401


def test_a_refresh_token_with_no_version_claim_is_rejected(client):
    token = _sign(tok="refresh")
    with patch("utils.redis_client.get_redis", return_value=_redis()), token_version_store(0):
        r = client.post("/auth/refresh", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["abc", None, [], {"a": 1}, 1.5])
async def test_a_malformed_version_claim_is_a_401_not_a_500(bad):
    """`TokenPayload(**data)` raises pydantic's ValidationError, which
    `decode_token` did not catch — so a token it could not model produced a 500
    and an unhandled traceback rather than a refusal."""
    from fastapi import HTTPException

    from api.middleware.auth import decode_token

    with pytest.raises(HTTPException) as exc:
        decode_token(_sign(ver=bad))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_a_token_with_no_subject_is_a_401_not_a_500():
    """Same defect, different required claim — the fix has to be at the
    decode boundary rather than on the `ver` field specifically."""
    from fastapi import HTTPException

    from api.middleware.auth import decode_token

    token = _sign()
    stripped = ".".join(token.split(".")[:2])  # invalid signature → JWTError path
    with pytest.raises(HTTPException) as exc:
        decode_token(stripped)
    assert exc.value.status_code == 401

    import jwt

    from config import get_settings

    no_sub = jwt.encode(
        {"exp": datetime.now(timezone.utc) + timedelta(minutes=5), "ver": 0},
        get_settings().ira_secret_key, algorithm="HS256")
    with pytest.raises(HTTPException) as exc:
        decode_token(no_sub)
    assert exc.value.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# The bump itself
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_bump_is_not_reported_as_success():
    """`bump_token_version` swallowed every Redis exception and logged a warning.

    `/auth/logout/all` then returned "All tokens have been invalidated" to
    somebody who had just been told their account was compromised, having
    invalidated nothing. A revocation that cannot be performed must be refused,
    not reported.
    """
    from fastapi import HTTPException

    from api.middleware.auth import bump_token_version

    with patch("utils.redis_client.get_redis", return_value=_redis()), \
            token_version_store(0, unavailable=True):
        with pytest.raises(HTTPException) as exc:
            await bump_token_version("alice")
    assert exc.value.status_code == 503


def test_logout_all_surfaces_the_failure(client):
    """The endpoint must not answer 200 when the bump did not happen."""
    from api.middleware.auth import _make_access_token

    token, _, _ = _make_access_token("alice", ver=0)

    # The store can be read (the caller's token is current) and cannot be
    # raised: the one failure logout-all must not report as success.
    from core.identity import token_versions

    async def _down(user_id):
        raise token_versions.TokenVersionUnavailable("synthetic: the store is down")

    with patch("utils.redis_client.get_redis", return_value=_redis()), \
            token_version_store(0), patch.object(token_versions, "bump", _down):
        r = client.post("/auth/logout/all",
                        headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 503, (
        f"logout/all answered {r.status_code} after the version bump failed — "
        f"the caller is told every session is dead and none of them are")


def test_logout_all_still_works_when_redis_works(client):
    """Positive control for the two tests above."""
    from api.middleware.auth import _make_access_token

    token, _, _ = _make_access_token("alice", ver=0)

    with patch("utils.redis_client.get_redis", return_value=_redis()), \
            token_version_store(0) as store:
        r = client.post("/auth/logout/all",
                        headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200, r.text
    assert store["bumps"] == 1 and store["version"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# Access tokens — the path that already worked, kept working
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_stale_access_token_is_still_rejected():
    from fastapi import HTTPException

    from api.middleware.auth import _make_access_token, require_auth

    token, _, _ = _make_access_token("alice", ver=0)
    with patch("utils.redis_client.get_redis", return_value=_redis()), token_version_store(1):
        with pytest.raises(HTTPException) as exc:
            await require_auth(_creds(token))
    assert exc.value.status_code == 401
    assert "invalidated" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_a_current_access_token_is_still_accepted():
    from api.middleware.auth import _make_access_token, require_auth

    token, _, _ = _make_access_token("alice", ver=1)
    with patch("utils.redis_client.get_redis", return_value=_redis()), token_version_store(1):
        assert await require_auth(_creds(token)) == "alice"


# ─────────────────────────────────────────────────────────────────────────────
# Static guard — the next constructor inherits the rule
# ─────────────────────────────────────────────────────────────────────────────

#: `jwt.encode` call sites that are NOT IRA session tokens. Each needs a reason,
#: because "it is not a session token" is the exemption an unversioned session
#: token would be smuggled in under.
NON_SESSION_ENCODERS = {
    "core/realtime/transport.py":
        "LiveKit room access token — a third party's credential, signed with "
        "their API secret, carrying their claim schema. IRA's revoke-all "
        "counter has no meaning to it.",
}


def test_every_ira_token_encoder_embeds_a_version():
    """Parsed, not grepped: the check is that the dict literal handed to
    `jwt.encode` contains a `ver` key, which a substring search cannot tell from
    the word appearing in a comment."""
    import ast
    import pathlib

    ira = pathlib.Path(__file__).resolve().parents[1]
    offenders = []

    for path in sorted(ira.rglob("*.py")):
        rel = path.relative_to(ira).as_posix()
        if rel.startswith(("tests/", "scripts/", ".venv/")) or rel in NON_SESSION_ENCODERS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue

        assigned = {}
        for node in ast.walk(tree):
            # `payload = {...}` — remember the literal so the encode call below
            # can be resolved back to it.
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigned[target.id] = node.value
            # `jwt.encode` / `_jwt.encode` only. Matching every `.encode`
            # attribute would sweep in `str.encode` and
            # `json.dumps(...).encode()`, which is how the first version of this
            # guard reported forty offenders and told me nothing.
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "encode"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id.lstrip("_") == "jwt"):
                continue
            if not node.args:
                continue
            claims = node.args[0]
            if isinstance(claims, ast.Name):
                claims = assigned.get(claims.id)
            if not isinstance(claims, ast.Dict):
                offenders.append(f"{rel}: claims are not a literal — cannot verify")
                continue
            keys = {k.value for k in claims.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if "ver" not in keys:
                offenders.append(f"{rel}: token payload has no `ver` claim")

    assert not offenders, (
        f"{offenders}\n\nAn IRA session token without a version claim cannot be "
        f"invalidated by /auth/logout/all. If this really is not a session "
        f"token, add it to NON_SESSION_ENCODERS with the reason.")


def test_the_static_guard_can_see_the_shape_it_forbids():
    """A guard that matched nothing would be permanently and silently green."""
    import ast

    planted = ast.parse(
        "payload = {'sub': u, 'exp': e, 'jti': j, 'tok': 'refresh'}\n"
        "token = jwt.encode(payload, key, algorithm='HS256')\n")

    assigned, found = {}, False
    for node in ast.walk(planted):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            assigned[node.targets[0].id] = node.value
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "encode"):
            claims = assigned[node.args[0].id]
            keys = {k.value for k in claims.keys}
            found = "ver" not in keys
    assert found, "the guard cannot detect a payload that omits `ver`"
