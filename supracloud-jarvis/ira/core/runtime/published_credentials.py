"""Credentials whose values were committed to this repository, and so must never be in use.

Six credentials were committed with real values to `supracloud-jarvis/.env.example`
from `2963fe1` (14 July 2026) to `98cbff9` (27 July 2026). They remain in history,
on three branches' trees, and in every clone (`docs/cloud/SECRET_ROTATION_REGISTER.md`).
Anyone who has, or ever had, a copy of the repository has them, so an install still
using one is open to all of those people: the signing key forges an owner token, the
admin password signs in as the owner, the webhook secret injects leads and bookings.

This module holds a digest of each published value, never the value, so IRA can
refuse to start while one is in use. The digest is scrypt, salted with the variable's
name, not a fast hash. A reader of this file learns nothing from it today: they can
read the value itself in history. That must stay true if this code is ever published
without its history. A fast hash of a short value, such as a password a person chose,
can be reversed by trying candidates; scrypt makes every try cost 32 MiB and a
noticeable fraction of a second.

Every committed `.env.example` version on every ref was scanned on 23 October 2026
(24 versions): apart from empty values and `CHANGE_ME` / `change-me` placeholders,
each of the six held exactly one value. Rebuild the table with
`scripts/security/published_credential_digests.py`, which prints provenance only.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import os

#: scrypt's cost: one of OWASP's equivalent minimums (N=2^15, r=8, p=3), 32 MiB
#: per digest. Changing any of these changes every digest: rebuild the table.
SCRYPT = {"n": 2**15, "r": 8, "p": 3, "maxmem": 2**26, "dklen": 32}

#: `digest(name, value)` of every published value, by variable name.
PUBLISHED_DIGESTS: dict[str, frozenset[str]] = {
    "IRA_SECRET_KEY": frozenset({
        "d0909f108957cf5963f32685927db09ac85a0714283ccfd94db0cea962e8e870",  # gitleaks:allow
    }),
    "IRA_ADMIN_PASSWORD": frozenset({
        "710f6c680171667d2dce7ce861db43d2d3063cd2b706894b0c88bebbb121940b",  # gitleaks:allow
    }),
    "WEBHOOK_SECRET": frozenset({
        "683bdfbe2ef41e3f4446ae7790b2b495a0a84a7e4d5198ae2cdd4528fae5071e",  # gitleaks:allow
    }),
    "IRA_CORTEX_KEY": frozenset({
        "627ff63aba2b0421aeea3e583872fd621763047fc976e68e6072f80bab73e0b1",  # gitleaks:allow
    }),
    "IRA_CANARY_TOKEN": frozenset({
        "de937ed8907b910611e0a7b164b3a0dcaf5a06f75c511e55dac952d26fcc90f0",  # gitleaks:allow
    }),
    "IRA_CANARY_USERNAME": frozenset({
        "91b88faeb396c382f3ab50ec690b3d2956a649c1668a684943cf5c0a69a3e2d3",  # gitleaks:allow
    }),
}

#: Each of these grants access, so IRA refuses to start while one is published.
REFUSED = ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "WEBHOOK_SECRET")

#: These grant nothing by themselves. A published canary token or username is a
#: tripwire anyone can step around (or trip on purpose), and IRA_CORTEX_KEY is
#: read but unused. IRA warns rather than refusing.
WARNED = ("IRA_CANARY_TOKEN", "IRA_CANARY_USERNAME", "IRA_CORTEX_KEY")

#: Where each is read from: a settings field, or the process environment for the
#: three the code reads directly.
_SETTING = {
    "IRA_SECRET_KEY": "ira_secret_key",
    "IRA_ADMIN_PASSWORD": "ira_admin_password",
    "WEBHOOK_SECRET": "webhook_secret",
}


def digest(name: str, value: str) -> str:
    """The table's digest of `value` as a value of the variable `name` (hex)."""
    return hashlib.scrypt(value.encode("utf-8"),
                          salt=f"ira-published-credential:{name}".encode("utf-8"),
                          **SCRYPT).hex()


@functools.lru_cache(maxsize=32)
def _digest_once(name: str, value: str) -> str:
    # The trust page asks on every load; the values change only with a restart.
    return digest(name, value)


def is_published(name: str, value: str | None) -> bool:
    """Is `value` one of the values `name` was published with?"""
    if not value or name not in PUBLISHED_DIGESTS:
        return False
    mine = _digest_once(name, value)
    return any(hmac.compare_digest(mine, known)
               for known in PUBLISHED_DIGESTS.get(name, ()))


def current_value(cfg, name: str) -> str:
    field = _SETTING.get(name)
    if field is not None:
        return (getattr(cfg, field, "") or "").strip()
    return (os.environ.get(name, "") or "").strip()


def published_in_use(cfg) -> list[str]:
    """The names, of the six, whose current value is a published one. Never values."""
    return [name for name in (*REFUSED, *WARNED)
            if is_published(name, current_value(cfg, name))]
