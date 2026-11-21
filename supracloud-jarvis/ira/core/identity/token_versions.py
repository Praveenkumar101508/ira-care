"""Per-account token versions, with PostgreSQL as the authority (migration 096).

Owner-approved on 2026-10-23, as written in
docs/security/GLOBAL_LOGOUT_REVIEW_PACKAGE_2026-10-21.md (decisions A-F).

"Log out everywhere" raises a per-account counter. Every token carries the
value that was current when it was issued (`ver`), and is refused once the
counter is higher. The counter lived in Redis, where a lost key read as 0 and
so revived every token issued before the last logout. Here it lives in
`auth_token_versions`: keyed by `users.id`, never lowered (a trigger refuses
it), and never deleted by the runtime role.

This module is the only reader and writer, and it fails closed:

  * any database failure raises `TokenVersionUnavailable`, which callers turn
    into 503. Nothing here answers "probably current";
  * a token naming no `users` row raises `UnknownAccount`, which callers turn
    into 401. An account without a row has no counter, so nothing can show its
    tokens are current.

Every read and write runs in a transaction bound to the one account it is
about (`app.user_id`, transaction-local), because the table's policies admit
only that account's row: the model `user_credentials` uses. A statement that
forgot its WHERE clause still reaches one row, never every account's.

Redis is not read on any request. A cache in front of this table would be the
restored-snapshot gap again. Redis is read once per start, by
`seed_from_redis`, to carry counters from before 096 into the table.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: The prefix the Redis counters used before 096. Read by the seeder and
#: written by the rollback helper, and by nothing else.
REDIS_PREFIX = "ira:token_ver:"

#: Seconds a version read or write may take before it counts as a failure.
#: asyncpg cancels the statement on the server when this expires, so a slow
#: database fails one request quickly instead of holding it.
QUERY_TIMEOUT_S = 2.0

#: Counters are kept this long after an account is erased, and after their
#: last change, then purged: the longest life of any credential IRA signs
#: (config.MAX_REFRESH_TOKEN_EXPIRE_DAYS). 096's purge policies repeat it.
RETENTION_AFTER_ERASURE = "7 days"


class TokenVersionUnavailable(RuntimeError):
    """The counter could not be read or written. Callers answer 503."""


class UnknownAccount(LookupError):
    """No `users` row for this subject, so no counter exists. Callers answer 401."""


# `bound` is read back so that a connection NOT bound to this account fails
# closed. Unbound, the policy hides the counter, the LEFT JOIN reads it as
# missing, and a missing counter means 0: every token from before the last
# "log out everywhere" would pass.
_CURRENT_SQL = (
    "SELECT u.id, v.version, current_setting('app.user_id', true) AS bound "
    "FROM users u LEFT JOIN auth_token_versions v ON v.user_id = u.id WHERE u.id = $1"
)
_CREATE_SQL = (
    "INSERT INTO auth_token_versions (user_id, version) VALUES ($1, 0) "
    "ON CONFLICT (user_id) DO NOTHING"
)
# The row lock ON CONFLICT DO UPDATE takes serialises concurrent bumps, so N
# bumps raise the counter by exactly N.
_BUMP_SQL = (
    "INSERT INTO auth_token_versions (user_id, version) VALUES ($1, 1) "
    "ON CONFLICT (user_id) DO UPDATE SET version = auth_token_versions.version + 1 "
    "RETURNING version"
)
_SEED_SQL = (
    "INSERT INTO auth_token_versions (user_id, version) VALUES ($1, $2) "
    "ON CONFLICT (user_id) DO UPDATE SET version = EXCLUDED.version "
    "WHERE auth_token_versions.version < EXCLUDED.version"
)
_PURGE_SQL = (
    "DELETE FROM auth_token_versions v USING users u "
    "WHERE u.id = v.user_id AND u.deleted_at IS NOT NULL "
    f"AND u.deleted_at < clock_timestamp() - interval '{RETENTION_AFTER_ERASURE}' "
    f"AND v.updated_at < clock_timestamp() - interval '{RETENTION_AFTER_ERASURE}'"
)


@asynccontextmanager
async def _account_bound(user_id: uuid.UUID):
    """A connection in a transaction bound to one account and nothing else.

    `set_config(..., true)` is transaction-local, so the binding ends with the
    transaction and never reaches the next borrower of the pooled connection.
    """
    from utils.db import acquire

    async with acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.user_id', $1, true)",
                               str(user_id), timeout=QUERY_TIMEOUT_S)
            yield conn


def _is_cloud() -> bool:
    from core.editions import is_cloud

    return is_cloud()


async def resolve_user_id(sub: str, uid: str | None = None) -> uuid.UUID:
    """The `users.id` a token's subject names.

    A token minted after 096 carries `uid`, the id itself, and it is signed
    with the token, so it is used as given. An older token has only `sub`:
    in the private edition the id is derived from the name exactly as the
    account was created (`private_user_id`); in the cloud it is looked up
    exactly as `core.identity.resolver` looks it up: by verified, undeleted
    normalised email, the one row `users_verified_email_key` allows. An
    unverified row may share the address (anyone can register one), and a
    lookup by address alone could pick it; the token would then carry the
    wrong account's counter, and erasing the real account would not end it.
    """
    if uid:
        try:
            return uuid.UUID(str(uid))
        except ValueError as exc:
            raise UnknownAccount("the token's uid is not an account id") from exc
    if not sub:
        raise UnknownAccount("the token names no subject")
    if not _is_cloud():
        from core.identity.principal import private_user_id

        return private_user_id(sub)
    from utils.db import acquire

    try:
        async with acquire() as conn:
            found = await conn.fetchval(
                "SELECT id FROM users WHERE normalised_email = normalise_email($1) "
                "AND email_verified_at IS NOT NULL AND deleted_at IS NULL",
                sub.strip(), timeout=QUERY_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - every failure is the same answer
        raise TokenVersionUnavailable("could not resolve the account") from exc
    if found is None:
        raise UnknownAccount("no account has this subject")
    return found


async def current(user_id: uuid.UUID) -> int:
    """The account's version: 0 if never bumped. One primary-key read."""
    try:
        async with _account_bound(user_id) as conn:
            row = await conn.fetchrow(_CURRENT_SQL, user_id, timeout=QUERY_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        raise TokenVersionUnavailable("could not read the token version") from exc
    return _version_of(row, user_id)


def _version_of(row, user_id: uuid.UUID) -> int:
    if row is None:
        raise UnknownAccount("no users row for this account")
    if row["bound"] != str(user_id):
        raise TokenVersionUnavailable("the read was not bound to this account")
    return int(row["version"] or 0)


async def current_for_login(user_id: uuid.UUID) -> int:
    """`current`, creating the account's row at 0 first if it has none."""
    import asyncpg

    try:
        async with _account_bound(user_id) as conn:
            await conn.execute(_CREATE_SQL, user_id, timeout=QUERY_TIMEOUT_S)
            row = await conn.fetchrow(_CURRENT_SQL, user_id, timeout=QUERY_TIMEOUT_S)
    except asyncpg.ForeignKeyViolationError as exc:
        raise UnknownAccount("no users row for this account") from exc
    except Exception as exc:  # noqa: BLE001
        raise TokenVersionUnavailable("could not read the token version") from exc
    return _version_of(row, user_id)


async def bump(user_id: uuid.UUID) -> int:
    """Raise the account's version by one and return the new value.

    Returns only after COMMIT, so a caller that reports success has a
    revocation that survives anything short of losing the database.
    """
    import asyncpg

    try:
        async with _account_bound(user_id) as conn:
            version = await conn.fetchval(_BUMP_SQL, user_id, timeout=QUERY_TIMEOUT_S)
    except asyncpg.ForeignKeyViolationError as exc:
        raise UnknownAccount("no users row for this account") from exc
    except Exception as exc:  # noqa: BLE001
        raise TokenVersionUnavailable("could not raise the token version") from exc
    return int(version)


async def bump_subject(sub: str, uid: str | None = None) -> int:
    """`bump` for the account a token subject names."""
    return await bump(await resolve_user_id(sub, uid))


@dataclass
class SeedReport:
    """What one start-up seeding did, for the log."""

    legacy_rows_created: int = 0
    redis: str = "read"
    counters_seen: int = 0
    counters_raised: int = 0
    unmatched: list[str] = field(default_factory=list)


async def _create_legacy_users(report: SeedReport) -> None:
    """A `users` row for each delegated account from before 040 that has none.

    Such an account has no counter, so until it has a row its tokens are
    refused. The row is written exactly as `security.delegated` writes one for
    a new account, at the id its tokens resolve to.
    """
    from core.identity.principal import private_user_id
    from security.delegated import _tenant_bound

    async with _tenant_bound() as conn:
        names = await conn.fetch(
            "SELECT username FROM delegated_users WHERE user_id IS NULL")
        for row in names:
            name = (row["username"] or "").strip().lower()
            if not name:
                continue
            status = await conn.execute(
                """INSERT INTO users (id, display_name, auth_provider, external_subject)
                   VALUES ($1, $2, 'password', $2)
                   ON CONFLICT DO NOTHING""",
                private_user_id(name), name)
            if status.endswith(" 1"):
                report.legacy_rows_created += 1


async def seed_from_redis() -> SeedReport:
    """Carry pre-096 counters into the table. Idempotent; run on every start.

    For each surviving `ira:token_ver:<name>` key the table keeps the higher
    of the two values, so a logout made before the upgrade is not undone and
    the table is never lowered. In the private edition it first creates the
    `users` rows legacy delegated accounts lack.

    Redis being unreachable is not an error here: the table is the authority,
    and the next start tries again. Counters for names with no account are
    listed in the report and skipped.
    """
    report = SeedReport()
    if not _is_cloud():
        try:
            await _create_legacy_users(report)
        except Exception:  # noqa: BLE001 - logged; those accounts stay refused
            logger.exception("token versions: could not create users rows for "
                             "legacy delegated accounts; they stay refused")

    try:
        from utils.redis_client import get_redis

        client = get_redis()
        counters: dict[str, int] = {}
        cursor = 0
        while True:
            cursor, keys = await client.scan(cursor=cursor, match=f"{REDIS_PREFIX}*", count=500)
            for key in keys:
                name = key.decode() if isinstance(key, bytes) else str(key)
                raw = await client.get(key)
                try:
                    counters[name[len(REDIS_PREFIX):]] = int(raw)
                except (TypeError, ValueError):
                    continue
            if not cursor:
                break
    except Exception as exc:  # noqa: BLE001
        report.redis = f"unavailable ({type(exc).__name__})"
        logger.warning("token versions: Redis unreachable at start-up; nothing "
                       "seeded this time: %s", exc)
        return report

    report.counters_seen = len(counters)
    import asyncpg

    for name, value in sorted(counters.items()):
        if value <= 0:
            continue
        try:
            user_id = await resolve_user_id(name)
            async with _account_bound(user_id) as conn:
                status = await conn.execute(_SEED_SQL, user_id, value)
        except (UnknownAccount, asyncpg.ForeignKeyViolationError):
            report.unmatched.append(name)
            continue
        if status.endswith(" 1"):
            report.counters_raised += 1
    return report


async def purge_erased() -> int:
    """Delete counters of accounts erased more than seven days ago.

    Only a counter that has also not moved for seven days goes: by then every
    token it refused has expired, so the reset to 0 revives nothing. 096's
    DELETE and read policies admit nothing else, whatever this statement says.
    Run by the API at every start (as `ira_app`) and by the daily worker job
    where a worker runs. No account is bound: the purge is the one path about
    many accounts, and its policies do not consult `app.user_id`.
    """
    from utils.db import acquire

    async with acquire() as conn:
        status = await conn.execute(_PURGE_SQL)
    try:
        return int(status.split()[-1])
    except (IndexError, ValueError):
        return 0
