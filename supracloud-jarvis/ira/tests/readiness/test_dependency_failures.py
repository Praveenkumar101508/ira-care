"""Phase 9 — what happens when a dependency IRA needs is not there.

A launch gate cares less about the happy path than about the moment a
dependency goes away, because that is when a system reveals which of its
security properties were structural and which were merely usual. Three
dependencies matter here, and this suite pins the posture for each:

  * **Redis**, which holds token revocation and the per-user token version.
  * **The local model**, whose absence is the standing temptation to route a
    private request somewhere else.

Action replay — the third failure the launch gate cares about — is already
covered by `tests/governance/test_no_execution_without_decision.py::
test_a_ticket_cannot_be_replayed`, and `RELEASE_READINESS.md` R13 cites that test
directly. Writing a second near-identical test here would make the readiness
checklist look fuller without making anything more true, which is the failure
mode this whole phase exists to prevent.

── The finding this suite was written for ───────────────────────────────────

`_is_revoked()` and `_get_token_version()` both catch a Redis error and, by
default, carry on: the token is treated as not revoked, the version as current.
The mechanism to reverse that already existed — `AUTH_FAIL_CLOSED_ON_REDIS` —
but the default was False for every edition.

For the private edition that is the right trade and is unchanged. For a
multi-user service it is not: during a Redis outage every token a user revoked
starts working again — logout, lost phone, password change, "sign out
everywhere" — for the length of the outage, with nothing in any response to say
so. The user took the one action available to them and it silently stopped
having an effect.

Phase 9 makes the cloud edition default to failing closed
(`core/editions.py`), and these tests pin both halves: cloud denies, private is
untouched.

What Phase 9 did NOT fix, and R08 since has: `revoke_token()` wrote only to
Redis in the private edition, fail-soft, so an outage that began before the
logout meant the revocation was never recorded. It now records every sign-out
in PostgreSQL first, in both editions (`f1_token_revocations`, 091), and the
request path reads it there, failing closed.
`test_a_dead_redis_no_longer_loses_a_sign_out` asserts that, where
`test_revocation_writes_are_still_fail_soft` used to assert the gap.

In the tests below the PostgreSQL store is the in-memory stand-in from
`tests/token_version_store.py`; the real one is
`tests/tenancy/test_single_logout_durable.py`.
"""
from __future__ import annotations

import pytest

# ── Redis: the revocation posture ────────────────────────────────────────────

#: Same set as tests/core/test_editions.py — Settings requires these regardless
#: of edition, and omitting one fails validation for a reason unrelated to the
#: posture under test.
_INFRA_ENV = {
    "IRA_SECRET_KEY": "ci-secret",
    "IRA_ADMIN_PASSWORD": "ci-admin",
    "POSTGRES_PASSWORD": "ci-db",
    "REDIS_PASSWORD": "ci-redis",
}

#: Cleared before building Settings unless a test sets them. The suite runs with
#: DEV_MODE=true in CI, and the cloud edition refuses DEV_MODE — without this the
#: cloud cases would fail for a reason unrelated to what they test. Same reason
#: as tests/core/test_editions.py.
_MUST_BE_UNSET = (
    "DEV_MODE", "ARMORY_ENABLED", "BROWSER_AGENT_ENABLED", "IRA_PRIVACY_MODE",
    "AUTH_FAIL_CLOSED_ON_REDIS",
)


def _settings(monkeypatch, **env):
    from config import Settings, get_settings

    for key in _MUST_BE_UNSET:
        if key not in env:
            monkeypatch.delenv(key, raising=False)
    for k, v in {**_INFRA_ENV, **env}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    return Settings()


def test_cloud_defaults_to_failing_closed_when_redis_is_unreachable(monkeypatch):
    """The fix. An authorisation service that cannot answer must deny."""
    settings = _settings(monkeypatch, IRA_EDITION="cloud")
    assert settings.auth_fail_closed_on_redis is True, (
        "the cloud edition must fail CLOSED on a Redis error. Failing open means "
        "a revoked token is honoured for the duration of an outage."
    )


def test_private_is_unchanged_and_still_fails_open(monkeypatch):
    """Not an oversight — a deliberate difference, pinned so it stays deliberate.

    One owner, one machine, Redis on localhost. Locking the owner out of their
    own assistant during a local blip is the worse outcome, and there is no
    second party the strictness would protect.
    """
    settings = _settings(monkeypatch, IRA_EDITION="private")
    assert settings.auth_fail_closed_on_redis is False

    settings = _settings(monkeypatch)  # nothing set at all
    assert settings.auth_fail_closed_on_redis is False, (
        "an install that never sets IRA_EDITION must keep today's behaviour"
    )


def test_an_explicit_operator_choice_still_wins_in_both_editions(monkeypatch):
    """Edition defaults fill gaps; they never override a stated decision."""
    assert _settings(
        monkeypatch, IRA_EDITION="cloud", AUTH_FAIL_CLOSED_ON_REDIS="false"
    ).auth_fail_closed_on_redis is False
    assert _settings(
        monkeypatch, IRA_EDITION="private", AUTH_FAIL_CLOSED_ON_REDIS="true"
    ).auth_fail_closed_on_redis is True


@pytest.mark.asyncio
async def test_a_dead_redis_denies_when_failing_closed(monkeypatch):
    """Exercises the real code path with a Redis client that raises.

    Not a check of the flag — a check that the flag reaches the decision.
    """
    from api.middleware import auth as auth_mod

    class DeadRedis:
        async def exists(self, *a, **k):
            raise ConnectionError("Redis is down")

        async def get(self, *a, **k):
            raise ConnectionError("Redis is down")

    monkeypatch.setattr(
        "utils.redis_client.get_redis", lambda: DeadRedis(), raising=False
    )
    monkeypatch.setattr(auth_mod, "_auth_fail_closed", lambda: True)
    from tests.token_version_store import token_version_store

    with token_version_store():          # PostgreSQL: not signed out
        assert await auth_mod._is_revoked("some-jti") is True, (
            "with a dead Redis and a fail-closed posture, a token must be treated "
            "as revoked"
        )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await auth_mod._get_token_version("someone")
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_a_dead_redis_admits_when_failing_open(monkeypatch):
    """The private posture, exercised rather than assumed.

    Documents precisely what the private edition accepts: an unreachable Redis
    means a token whose sign-out exists ONLY in Redis is honoured. Since R08
    that is only a sign-out made before the upgrade and not yet copied by the
    start-up seeder; every later one is in PostgreSQL. Stated in a test so
    nobody has to infer it from a default.

    "Log out everywhere" is no longer part of it. Since migration 096 the
    per-account version is read from PostgreSQL, so a dead Redis leaves it as
    stored: here 0, the store's value, not a fallback.
    """
    from tests.token_version_store import token_version_store
    from api.middleware import auth as auth_mod

    class DeadRedis:
        async def exists(self, *a, **k):
            raise ConnectionError("Redis is down")

        async def get(self, *a, **k):
            raise ConnectionError("Redis is down")

    monkeypatch.setattr(
        "utils.redis_client.get_redis", lambda: DeadRedis(), raising=False
    )
    monkeypatch.setattr(auth_mod, "_auth_fail_closed", lambda: False)

    with token_version_store():          # PostgreSQL: not signed out
        assert await auth_mod._is_revoked("some-jti") is False
    with token_version_store(4):
        assert await auth_mod._get_token_version("someone") == 4


@pytest.mark.asyncio
async def test_a_dead_redis_no_longer_loses_a_sign_out(monkeypatch):
    """The gap Phase 9 did NOT close, and R08 has (RELEASE_READINESS.md R08).

    This test used to assert that `revoke_token` swallowed a Redis error and
    recorded the sign-out nowhere. Now the sign-out is recorded in PostgreSQL
    first, so with Redis down at the logout AND at the next request, the token
    is still refused, in the private edition, whatever the fail-open posture.
    """
    from api.middleware import auth as auth_mod
    from tests.token_version_store import token_version_store

    class DeadRedis:
        async def setex(self, *a, **k):
            raise ConnectionError("Redis is down")

        async def exists(self, *a, **k):
            raise ConnectionError("Redis is down")

    monkeypatch.setattr(
        "utils.redis_client.get_redis", lambda: DeadRedis(), raising=False
    )
    monkeypatch.setattr(auth_mod, "_auth_fail_closed", lambda: False)

    with token_version_store() as store:
        await auth_mod.revoke_token("some-jti", 60)       # no error: PostgreSQL has it
        assert "some-jti" in store["revoked"]
        assert await auth_mod._is_revoked("some-jti") is True


# ── The local model going away ───────────────────────────────────────────────


def test_a_local_model_outage_does_not_widen_where_a_request_may_go():
    """Availability is step 3; it cannot re-admit what step 1 excluded.

    Phase 8 tests this through the gateway. Here it is asserted at the policy
    level as a launch property: for every sensitivity above `normal`, no privacy
    mode below `external_ok` admits an external provider — and nothing about the
    local model's health is an input to that answer.
    """
    from core.interfaces.model_provider import Locality, ProviderDisclosure
    from core.models.policy import PrivacyMode, SENSITIVE, admits

    external = ProviderDisclosure(
        provider_id="somewhere-else",
        display_name="Somewhere Else",
        locality=Locality.EXTERNAL,
        retains_prompts=False,
        trains_on_data=False,
    )
    for mode in (PrivacyMode.LOCAL_ONLY, PrivacyMode.LOCAL_FIRST, PrivacyMode.BALANCED):
        for level in sorted(SENSITIVE):
            allowed, why = admits(external, mode=mode, sensitivity=level)
            assert allowed is False, (
                f"{mode.value} + {level} admitted an external provider. A local "
                f"model outage must produce a refusal, not a wider candidate set."
            )
            assert why, "a refusal must carry a reason a user can act on"


def test_credentials_are_refused_even_in_the_most_permissive_mode():
    """There is no outage, and no setting, that sends a password to a third
    party. Asserted here as well as in Phase 8 because it is the one routing
    property whose failure is unrecoverable."""
    from core.interfaces.model_provider import Locality, ProviderDisclosure
    from core.models.policy import PrivacyMode, admits

    external = ProviderDisclosure(
        provider_id="somewhere-else",
        display_name="Somewhere Else",
        locality=Locality.EXTERNAL,
        retains_prompts=False,
        trains_on_data=False,
    )
    allowed, why = admits(
        external, mode=PrivacyMode.EXTERNAL_OK, sensitivity="credential"
    )
    assert allowed is False
    assert "credential" in why
