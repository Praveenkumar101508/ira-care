"""The credential boundary — the only module in IRA that decrypts a token.

The rule, from the architecture brief and quoted in
`core/interfaces/connector_gateway.py`:

    The model must never directly access OAuth credentials, database
    credentials, email tokens, calendar tokens, user secrets, payment
    credentials or cloud provider secrets.

"Never" is a strong word, so it needs something stronger than care behind it.
Three things carry it here:

**1. One module decrypts.** `open_secret` is the only function in the codebase
that turns ciphertext into a usable token, and
`tests/architecture/test_credential_boundary.py` walks the AST of every module
and fails if anything outside `core/connectors/` imports it. A leak therefore
requires a new import that CI refuses, not merely a careless line.

**2. Nothing returns a token.** The public shape is
`use_secret(sealed, fn)` — a callback. The caller receives whatever `fn`
returns, and `fn` is written inside a connector. There is deliberately no
`get_token()` for an agent to reach: a function that returns a credential is
one whose return value ends up in a log, a trace, an exception, or a model's
context, and no amount of care at the call sites prevents that.

**3. Missing key means refuse.** The brief lists "missing encryption key" among
the conditions that must fail closed. `seal` and `open_secret` raise
`MissingEncryptionKey` rather than falling back to storing plaintext or to a
hardcoded development key. A build with no key configured cannot connect an
account at all, which is the correct outcome — the alternative is a deployment
that silently stores refresh tokens in the clear.

── The cryptography ──────────────────────────────────────────────────────────

AES-256-GCM, from `cryptography` (already a dependency via
`python-jose[cryptography]`). Authenticated, so tampering with the ciphertext in
the database is detected rather than producing a garbage token. A fresh 96-bit
nonce per encryption, never reused — GCM nonce reuse under the same key is
catastrophic, so the nonce is generated here and never accepted from a caller.

Keys are addressed by id so a rotation can find the rows it must re-wrap.

── Envelope encryption (blocker B3) ─────────────────────────────────────────

Phase 7 sealed every credential in the deployment directly under the
environment key. `RELEASE_READINESS.md` R11 recorded what that cost: *"a leaked
key compromises every stored credential at once"*, and *"`rewrap` exists with no
caller, so rotating the key re-encrypts nothing"*.

So the environment key is now a **key-encryption key** (KEK) whose only job is
wrapping per-tenant **data-encryption keys** (DEKs), which live in
`tenant_data_keys` and are managed by `core/connectors/keyring.py`. This module
stays the pure cryptography — it does no I/O and knows nothing about the
database — and gains four primitives for the outer envelope: `generate_dek`,
`wrap_dek`, `_unwrap_dek` and `seal_with`.

A `SealedSecret` says which of the two schemes it used, in `key_id`:

    "dek:3"    sealed under this tenant's DEK version 3
    anything   sealed directly under the KEK (Phase 7 rows, and the PKCE
    else       verifier — see the scope note below)

The distinction is not a hint. `_open_secret` **refuses** a `dek:` secret when
no key is supplied rather than falling back to the KEK: a fallback would open
under the wrong key on a good day and silently succeed on a bad one.

── What the DEK covers, and what it deliberately does not ───────────────────

DEKs cover `connector_accounts.credential_ciphertext` — the OAuth refresh token.
That is the long-lived secret, the one whose leak matters and the one worth
being able to destroy.

The PKCE verifier in `connector_oauth_states` stays under the KEK. It lives for
ten minutes, its row is deleted the moment it is consumed, and `oauth.begin` is
a synchronous function on the redirect path — putting a database round-trip
there to fetch a DEK would slow the one step a user is watching, to protect a
secret that is worthless by the time anyone could use it.
`tests/connectors/test_keyring.py` pins that boundary so it stays a decision
rather than becoming an assumption.

── What this is NOT ─────────────────────────────────────────────────────────

**Still not a KMS or an HSM.** The KEK comes from the process environment. An
attacker with both the database and the environment gets every live DEK and
therefore every live credential; envelope encryption does not change that. What
it changes is everything below the KEK — blast radius, rotation, and the ability
to destroy one tenant's keys without finding every copy of their ciphertext.
Stated the same way in CONNECTOR_MODEL.md and in 030_key_management.sql.
"""
from __future__ import annotations

import base64
import logging
import os
import secrets as _stdlib_secrets
from dataclasses import dataclass, field
from typing import Callable, Final, Optional, TypeVar

logger = logging.getLogger("ira.connectors.secrets")

#: Where the key comes from. One variable, explicitly named for connectors, so
#: it cannot be confused with IRA_SECRET_KEY (which signs sessions) — reusing a
#: signing key for encryption is a classic and quiet mistake.
KEY_ENV: Final[str] = "IRA_CONNECTOR_ENCRYPTION_KEY"
KEY_ID_ENV: Final[str] = "IRA_CONNECTOR_ENCRYPTION_KEY_ID"

#: AES-256. A shorter key is a misconfiguration, not a weaker option.
KEY_BYTES: Final[int] = 32
NONCE_BYTES: Final[int] = 12

#: How a `SealedSecret.key_id` announces "this used a tenant DEK, version N".
#: Read by `_open_secret` to decide whether an unsupplied key is a fallback or a
#: refusal — it is always a refusal.
DEK_KEY_PREFIX: Final[str] = "dek:"

T = TypeVar("T")


class SecretsError(RuntimeError):
    """Base for every credential-handling failure."""


class MissingEncryptionKey(SecretsError):
    """No usable key. Connecting an account must refuse.

    Its own type because it is the one failure an operator can fix, and the
    message says how.
    """


class CredentialTampered(SecretsError):
    """The ciphertext did not authenticate.

    Either the row was modified or the wrong key was used. Both mean the token
    must not be used, and neither should be reported as "expired" — that would
    send an operator looking in the wrong place.
    """


class KeyNotResolved(SecretsError):
    """A `dek:` secret was opened without its data-encryption key.

    Its own type, and a refusal rather than a fallback to the KEK. Falling back
    would mean a bug in key resolution presents as "your Gmail disconnected"
    instead of as an error — and on the day the KEK and a DEK happened to agree
    about some row, as a silent success under the wrong key.
    """


@dataclass(frozen=True, slots=True)
class SealedSecret:
    """An encrypted credential, exactly as the database stores it.

    `ciphertext` is `repr`-suppressed. It is not plaintext, but a ciphertext in
    a traceback is still an artefact that should not be casually copied into a
    bug report.
    """

    ciphertext: bytes = field(repr=False)
    key_id: str
    nonce: bytes = field(repr=False)

    def __str__(self) -> str:  # pragma: no cover - defensive
        return f"SealedSecret(key_id={self.key_id}, {len(self.ciphertext)} bytes)"


def _load_key(env: Optional[dict[str, str]] = None) -> tuple[bytes, str]:
    """The configured key and its id, or refuse.

    Deliberately no default and no generated-on-the-fly key. A generated key
    would make the first run work and every subsequent run fail to decrypt,
    which presents as "Google disconnected me" and sends everyone in the wrong
    direction.
    """
    # P4-10: the KEK may be held by a managed provider. Asked FIRST, because a
    # deployment that selected aws-kms must never quietly fall through to a
    # plaintext key in the environment — that substitution is the one ADR-0012
    # exists to prevent and it would be invisible, encrypting new rows under the
    # wrong key and failing to open the old ones only later.
    from core.crypto import key_provider

    if key_provider.configured(env) == key_provider.AWS_KMS:
        from core.crypto import aws_kms

        return aws_kms.load_kek(env)

    source = os.environ if env is None else env
    raw = (source.get(KEY_ENV) or "").strip()
    if not raw:
        raise MissingEncryptionKey(
            f"{KEY_ENV} is not set, so connector credentials cannot be "
            f"encrypted and no account can be connected. Generate one with: "
            f"python -c \"import base64,os;print(base64.b64encode(os.urandom(32)).decode())\" "
            f"and set it in the environment. Refusing rather than storing a "
            f"refresh token in the clear."
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise MissingEncryptionKey(
            f"{KEY_ENV} is not valid base64: {exc}. Expected {KEY_BYTES} "
            f"base64-encoded random bytes."
        ) from exc
    if len(key) != KEY_BYTES:
        raise MissingEncryptionKey(
            f"{KEY_ENV} decodes to {len(key)} bytes; AES-256-GCM needs exactly "
            f"{KEY_BYTES}. A short key is a misconfiguration, not a weaker option."
        )
    key_id = (source.get(KEY_ID_ENV) or "").strip() or "default"
    return key, key_id


def configured_kek_id(env: Optional[dict[str, str]] = None) -> str:
    """The id of the currently configured key-encryption key.

    Returns the id only, never the key. Callers need it to build the AAD a
    wrapped DEK is bound to, and to record which KEK wrapped a row — neither of
    which is a reason to hand them the material.
    """
    _key, key_id = _load_key(env)
    return key_id


def is_configured(env: Optional[dict[str, str]] = None) -> bool:
    """True when credentials can be sealed. Used to disable the connect button
    rather than to let a user start a flow that cannot finish."""
    try:
        _load_key(env)
        return True
    except MissingEncryptionKey:
        return False


def _aesgcm(key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(key)


def seal(
    plaintext: str,
    *,
    aad: str = "",
    env: Optional[dict[str, str]] = None,
) -> SealedSecret:
    """Encrypt a credential for storage.

    `aad` is additional authenticated data — pass something that binds the
    ciphertext to its row, such as `f"{tenant_id}:{connector}"`. It is
    authenticated but not encrypted, so a ciphertext lifted from one user's row
    and pasted into another's fails to open. Without it, a database-write
    attacker could move a valid credential between accounts.
    """
    if not plaintext:
        raise SecretsError("refusing to seal an empty credential")
    key, key_id = _load_key(env)
    nonce = _stdlib_secrets.token_bytes(NONCE_BYTES)
    ciphertext = _aesgcm(key).encrypt(
        nonce, plaintext.encode("utf-8"), aad.encode("utf-8") if aad else None
    )
    return SealedSecret(ciphertext=ciphertext, key_id=key_id, nonce=nonce)


# ── The outer envelope: keys that encrypt keys ───────────────────────────────


@dataclass(frozen=True, slots=True)
class WrappedKey:
    """A data-encryption key, encrypted under the KEK, exactly as the row stores
    it. `material` is `repr`-suppressed for the same reason a ciphertext is."""

    material: bytes = field(repr=False)
    nonce: bytes = field(repr=False)
    kek_id: str

    def __str__(self) -> str:  # pragma: no cover - defensive
        return f"WrappedKey(kek_id={self.kek_id}, {len(self.material)} bytes)"


def dek_key_id(version: int) -> str:
    """The `key_id` a credential carries when sealed under DEK version N."""
    if int(version) < 1:
        raise SecretsError(f"key versions start at 1, not {version}")
    return f"{DEK_KEY_PREFIX}{int(version)}"


def dek_version_of(key_id: str) -> Optional[int]:
    """The version a `key_id` refers to, or None if it is not a DEK reference.

    None is the honest answer for a Phase 7 row sealed under the KEK, and the
    callers treat it as "legacy", not as an error.
    """
    if not key_id or not key_id.startswith(DEK_KEY_PREFIX):
        return None
    try:
        version = int(key_id[len(DEK_KEY_PREFIX):])
    except ValueError:
        return None
    return version if version >= 1 else None


def generate_dek() -> bytes:
    """A fresh data-encryption key.

    `os.urandom` via the stdlib `secrets` module — the same source the OAuth
    state and PKCE verifier use. A DEK is never derived from anything: deriving
    it from a tenant id would make every deployment's key for a given tenant
    identical given the KEK, which is the property a KDF is supposed to prevent.
    """
    return _stdlib_secrets.token_bytes(KEY_BYTES)


def wrap_dek(
    dek: bytes, *, aad: str, env: Optional[dict[str, str]] = None
) -> WrappedKey:
    """Encrypt a DEK under the configured KEK.

    `aad` binds the wrapped key to the row it belongs in — tenant, purpose and
    version. Without it, a database-write attacker could move tenant A's wrapped
    DEK into tenant B's row and read B's credentials with A's key. RLS already
    blocks that read path; this blocks it a second time, in the cryptography,
    where it does not depend on a session variable being set.
    """
    if len(dek) != KEY_BYTES:
        raise SecretsError(
            f"a data-encryption key is {KEY_BYTES} bytes; got {len(dek)}"
        )
    if not aad:
        raise SecretsError(
            "refusing to wrap a key with no AAD — the binding to its row is "
            "what stops a wrapped key being moved between tenants"
        )
    kek, kek_id = _load_key(env)
    nonce = _stdlib_secrets.token_bytes(NONCE_BYTES)
    material = _aesgcm(kek).encrypt(nonce, dek, aad.encode("utf-8"))
    return WrappedKey(material=material, nonce=nonce, kek_id=kek_id)


def _unwrap_dek(
    wrapped: WrappedKey, *, aad: str, env: Optional[dict[str, str]] = None
) -> bytes:
    """Recover a DEK. PRIVATE, and in the architecture test's forbidden set.

    A caller holding a DEK can decrypt every credential of that tenant and
    version, so this is exactly as sensitive as `_open_secret` and is guarded
    the same way.
    """
    kek, kek_id = _load_key(env)
    if wrapped.kek_id and wrapped.kek_id != kek_id:
        raise CredentialTampered(
            f"This key was wrapped under KEK {wrapped.kek_id!r} and the "
            f"configured KEK is {kek_id!r}. Refusing rather than attempting a "
            f"decrypt that would fail as 'tampered' and send an operator "
            f"looking for an attacker instead of for a misconfigured "
            f"{KEY_ID_ENV}."
        )
    try:
        return _aesgcm(kek).decrypt(wrapped.nonce, wrapped.material, aad.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - InvalidTag and friends
        raise CredentialTampered(
            "A wrapped data-encryption key failed to authenticate. Either the "
            "row was modified, the key was moved between tenants, or the KEK "
            "changed. No credential under this key will be opened."
        ) from exc


def seal_with(
    dek: bytes, plaintext: str, *, aad: str = "", key_id: str
) -> SealedSecret:
    """Encrypt a credential under a tenant's DEK.

    Mirrors `seal`, minus the environment lookup: the key arrives as an
    argument because the caller resolved it from `tenant_data_keys`.
    """
    if not plaintext:
        raise SecretsError("refusing to seal an empty credential")
    if len(dek) != KEY_BYTES:
        raise SecretsError(
            f"a data-encryption key is {KEY_BYTES} bytes; got {len(dek)}"
        )
    if dek_version_of(key_id) is None:
        raise SecretsError(
            f"key_id {key_id!r} does not name a DEK version. A credential "
            f"sealed under a DEK must say so, or nothing will know which key "
            f"opens it."
        )
    nonce = _stdlib_secrets.token_bytes(NONCE_BYTES)
    ciphertext = _aesgcm(dek).encrypt(
        nonce, plaintext.encode("utf-8"), aad.encode("utf-8") if aad else None
    )
    return SealedSecret(ciphertext=ciphertext, key_id=key_id, nonce=nonce)


def _open_secret(
    sealed: SealedSecret,
    *,
    aad: str = "",
    env: Optional[dict[str, str]] = None,
    key: Optional[bytes] = None,
) -> str:
    """Decrypt. PRIVATE — reached only through `use_secret`.

    Underscore-prefixed and absent from `__all__` on purpose. The architecture
    test allows `core/connectors/` to import this module at all; keeping the
    raw decrypt private means even inside that package the callback form is the
    obvious one to reach for.

    `key` is the tenant's DEK when the secret was sealed under one. When
    `sealed.key_id` names a DEK and no key is supplied this **refuses** — see
    `KeyNotResolved` for why a fallback to the KEK would be worse than an error.
    """
    wants_dek = dek_version_of(sealed.key_id) is not None
    if wants_dek and key is None:
        raise KeyNotResolved(
            f"This credential was sealed under data-encryption key "
            f"{sealed.key_id!r} and no key was supplied, so it will not be "
            f"opened. Resolve it through core.connectors.keyring first. "
            f"Falling back to the environment key here would be a decrypt "
            f"under a key that was never meant to open this."
        )
    if key is not None and not wants_dek:
        raise KeyNotResolved(
            f"A data-encryption key was supplied for a credential sealed under "
            f"the environment key ({sealed.key_id!r}). Refusing rather than "
            f"guessing which of the two the caller meant."
        )

    if key is None:
        key, _key_id = _load_key(env)
    try:
        plaintext = _aesgcm(key).decrypt(
            sealed.nonce, sealed.ciphertext, aad.encode("utf-8") if aad else None
        )
    except Exception as exc:  # noqa: BLE001 - InvalidTag and friends
        raise CredentialTampered(
            "A stored connector credential failed to authenticate. Either the "
            "row was modified or the key has changed. The credential will not "
            "be used. This is NOT an expiry — do not report it as one."
        ) from exc
    return plaintext.decode("utf-8")


def use_secret(
    sealed: SealedSecret,
    fn: Callable[[str], T],
    *,
    aad: str = "",
    env: Optional[dict[str, str]] = None,
    key: Optional[bytes] = None,
) -> T:
    """Run `fn` with the decrypted credential and return only `fn`'s result.

    The shape of this function is the point. There is no `get_token()`, because
    a function that returns a credential is one whose return value ends up in a
    log line, a traceback, a metrics label or a model's context — and no
    discipline at the call sites reliably prevents that. Here the token exists
    only as an argument inside `fn`.

    It is not a security boundary in the language sense: `fn` could stash the
    string somewhere. It is a boundary in the REVIEW sense — doing so requires
    writing code that visibly captures a credential, inside
    `core/connectors/`, which is the one place a reviewer is looking for it.

    `key` carries the tenant's DEK for a credential sealed under one. Resolving
    it is `keyring`'s job, not this module's — this module does no I/O.
    """
    token = _open_secret(sealed, aad=aad, env=env, key=key)
    try:
        return fn(token)
    finally:
        # Python cannot reliably wipe a str, so this is not a scrub — it just
        # drops the last local reference promptly rather than leaving it bound
        # in this frame for the lifetime of whatever raised. Claiming a wipe
        # here would be false.
        del token


async def use_secret_async(
    sealed: SealedSecret,
    fn: Callable[[str], "object"],
    *,
    aad: str = "",
    env: Optional[dict[str, str]] = None,
    key: Optional[bytes] = None,
):
    """Async counterpart. `fn` may return an awaitable."""
    token = _open_secret(sealed, aad=aad, env=env, key=key)
    try:
        result = fn(token)
        if hasattr(result, "__await__"):
            return await result  # type: ignore[misc]
        return result
    finally:
        del token


def rewrap(
    sealed: SealedSecret,
    *,
    aad: str = "",
    old_env: Optional[dict[str, str]] = None,
    new_env: Optional[dict[str, str]] = None,
    old_key: Optional[bytes] = None,
    new_key: Optional[bytes] = None,
    new_key_id: Optional[str] = None,
) -> SealedSecret:
    """Open under one key and re-seal under another.

    Phase 7 shipped this with the note *"nothing calls it yet"*, and
    `RELEASE_READINESS.md` R11 counted that as part of blocker B3 — a rotation
    primitive nobody invokes rotates nothing. `keyring.rotate_tenant` is now its
    caller, and `tests/connectors/test_keyring.py` asserts the credential really
    is readable afterwards and really was re-encrypted, rather than trusting
    that a function named `rewrap` rewrapped.

    Four ways to call it, and they compose:

        old_key=None, new_key=None       KEK → KEK   (a KEK rotation)
        old_key=None, new_key=<dek>      KEK → DEK   (migrating a Phase 7 row)
        old_key=<dek>, new_key=<dek2>    DEK → DEK   (an ordinary rotation)
        old_key=<dek>, new_key=None      DEK → KEK   (deliberately possible, so
                                                      a rollback has a path)
    """
    plaintext = _open_secret(sealed, aad=aad, env=old_env, key=old_key)
    try:
        if new_key is not None:
            if not new_key_id:
                raise SecretsError(
                    "re-sealing under a DEK needs the key_id that names its "
                    "version, or nothing will know which key opens the result"
                )
            return seal_with(new_key, plaintext, aad=aad, key_id=new_key_id)
        return seal(plaintext, aad=aad, env=new_env)
    finally:
        del plaintext


def fingerprint(sealed: SealedSecret) -> str:
    """A short, non-reversible label for logs.

    Lets an operator tell "the same credential as before" from "a new one"
    without ever printing either. Derived from the ciphertext, so it changes on
    every re-seal — which is correct: a re-sealed credential is a different
    stored object even when the token is the same.
    """
    import hashlib

    digest = hashlib.sha256(sealed.ciphertext).hexdigest()
    return f"{sealed.key_id}:{digest[:12]}"


__all__ = [
    "DEK_KEY_PREFIX",
    "KEY_BYTES",
    "KEY_ENV",
    "KEY_ID_ENV",
    "CredentialTampered",
    "KeyNotResolved",
    "MissingEncryptionKey",
    "SealedSecret",
    "SecretsError",
    "WrappedKey",
    "configured_kek_id",
    "dek_key_id",
    "dek_version_of",
    "fingerprint",
    "generate_dek",
    "is_configured",
    "rewrap",
    "seal",
    "seal_with",
    "use_secret",
    "use_secret_async",
    "wrap_dek",
]
