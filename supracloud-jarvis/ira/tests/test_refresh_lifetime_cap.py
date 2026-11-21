"""A refresh token may not outlive migration 096's purge window.

"Log out everywhere" refuses any token stamped with a version older than the
account's counter. 096 purges a counter that has not moved for seven days,
which resets it to 0, and that is safe only because no token lives longer than
seven days: by then every token the counter refused has expired.

`REFRESH_TOKEN_EXPIRE_DAYS` is an ordinary setting (`.env.example` lists it),
so without a cap an operator could set 30, and after a purge a refresh token
refused by "log out everywhere" would be accepted again.
"""
import pytest

from config import MAX_REFRESH_TOKEN_EXPIRE_DAYS, Settings

_SECRETS = dict(
    ira_secret_key="ci-secret",
    ira_admin_password="ci-admin",
    postgres_password="ci-db",
    redis_password="ci-redis",
)


def test_the_cap_is_the_purge_window():
    from core.identity.token_versions import RETENTION_AFTER_ERASURE

    assert MAX_REFRESH_TOKEN_EXPIRE_DAYS == 7
    assert RETENTION_AFTER_ERASURE == f"{MAX_REFRESH_TOKEN_EXPIRE_DAYS} days"


@pytest.mark.parametrize("days", [1, 7])
def test_a_lifetime_within_the_window_is_accepted(days):
    assert Settings(**_SECRETS, refresh_token_expire_days=days).refresh_token_expire_days == days


@pytest.mark.parametrize("days", [8, 30, 0, -1])
def test_a_lifetime_outside_the_window_is_refused_at_start(days):
    with pytest.raises(RuntimeError, match="REFRESH_TOKEN_EXPIRE_DAYS"):
        Settings(**_SECRETS, refresh_token_expire_days=days)


def test_the_default_is_within_the_window():
    assert Settings(**_SECRETS).refresh_token_expire_days == 7
