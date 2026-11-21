"""Deriving one role's DSN from another's, without assuming how it is written.

Not a test module (no `test_` prefix, so pytest does not collect it).

Two suites used to do this instead:

    migrator = admin_dsn.replace("postgresql://postgres@",
                                 "postgresql://ira_migrator:ira_migrator@")

which works only when the admin DSN is written in exactly that form —
passwordless, user `postgres`, no port. A DSN with a password in it,
`postgresql://postgres:postgres@localhost:5432/iratest`, does not match, so the
substitution silently does nothing and `migrator` stays the SUPERUSER DSN.

That failure is quiet in the worst way. Nothing errors: the variable is still a
valid DSN, it still connects, and the migration command it is handed is the one
that refuses to run as a superuser — so eight tests failed with "REFUSING:
connected as 'postgres', a SUPERUSER" and the cause looked like a migration
defect rather than a string that did not match.

Parsing the URL and replacing the credentials removes the assumption. Both DSN
shapes this repository uses are covered, and they are quite different:

    postgresql://postgres:postgres@localhost:5432/iratest        (CI, TCP)
    postgresql://postgres@/ira_ci?host=/var/lib/postgresql/run   (local, socket)

The second carries its host and port in the QUERY string and has an empty
netloc host, which is exactly the shape a naive rewrite mangles.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def as_role(dsn: str, role: str, password: str | None = None) -> str:
    """`dsn` with its credentials replaced by `role` (and `password`).

    Host, port, database and every query parameter are preserved, so a
    difference in behaviour between two DSNs produced by this function is
    attributable to the role and to nothing else — which is the entire reason
    the suites that use it exist.
    """
    parts = urlsplit(dsn)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    credential = role if password is None else f"{role}:{password}"
    return urlunsplit(parts._replace(netloc=f"{credential}@{host}"))
