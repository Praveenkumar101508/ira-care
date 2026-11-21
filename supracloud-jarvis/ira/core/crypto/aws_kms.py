"""AWS KMS as the custodian of the key-encryption key (Phase 4 batch 10).

P4-04 named where the KEK comes from and made production refuse the development
provider. It implemented no managed provider, and said so. This is that
provider.

── What it takes custody of, and what it does not ──────────────────────────

CLOUD-ADR-0012's hierarchy is:

    cloud KMS master key  →  tenant KEK  →  per-record DEK  →  ciphertext

The DEKs are minted per record by `secrets.py` and stay exactly where they are.
What changes is the middle link: instead of the KEK arriving as base64 in
`IRA_CONNECTOR_ENCRYPTION_KEY`, it arrives as **ciphertext** that only KMS can
open, and the plaintext exists in this process's memory and nowhere else.

That is deliberately the smallest possible change. Every wrap, unwrap, AAD bind
and rotation path built in P2-05, P4-02 and P4-05 keeps working unmodified,
because they all take a KEK and none of them care where it came from. A design
that called `kms.Encrypt` per record instead would have been a network round
trip on every voice authentication and a rewrite of five modules — more code,
more failure surface, and no more custody than this.

── Why the KEK is a KMS data key and not a KMS "encrypt this blob" call ────

`GenerateDataKey` returns a plaintext key and its ciphertext together. The
plaintext is used and dropped; the ciphertext is what the operator stores. This
is the pattern KMS is built for, it costs one API call per process rather than
one per record, and it means a KMS outage degrades to "processes that are
already running keep working" rather than "everything stops at once".

That last property is a trade-off and it is stated rather than hidden: a
compromised process keeps its KEK until it restarts. The alternative — fetching
per operation — makes KMS a hard dependency of every read, which for an
authentication path means a KMS blip logs everybody out.

── Fail closed ─────────────────────────────────────────────────────────────

Every failure raises. There is no path where a KMS problem results in the
development KEK being used instead, because that is the exact substitution
ADR-0012 exists to prevent, and it would be invisible: the application would
start, encrypt new rows under the wrong key, and fail to open the old ones only
later.

── Nothing logs key material, and that took more than not logging it ───────

This module logs the KMS key id and the operation, never the key. That is not
sufficient on its own: **botocore logs the full HTTP response body at DEBUG**,
and the body of a KMS `Decrypt` response contains

    {"Plaintext": "<the key-encryption key, base64>", "KeyId": "..."}

So a deployment that turned on debug logging for troubleshooting would write the
KEK into its logs, where it is shipped to aggregators and copied into bug
reports. Nothing in this repository would have said so.

`_quiet_botocore()` raises the level of the botocore loggers that serialise
bodies, for the duration of a KMS call only. It is a context manager rather than
a startup-time configuration because a startup setting is one
`logging.basicConfig` away from being undone, and the undo would be silent.

Found by `test_nothing_logs_key_material`, which failed the first time it ran.
"""
from __future__ import annotations

import base64
import logging
import os
import threading
import time
from typing import Final, Optional

logger = logging.getLogger("ira.crypto.aws_kms")

#: The KMS key that wraps the KEK. An ARN, a key id, or an `alias/...`.
KEY_ID_ENV: Final[str] = "IRA_KMS_KEY_ID"

#: The KEK, encrypted under the KMS key above, base64-encoded. This is what an
#: operator stores in configuration — it is not a secret in the sense the plain
#: KEK is, because it is useless without KMS permission to decrypt it, but it is
#: still treated as one: never logged, never returned.
CIPHERTEXT_ENV: Final[str] = "IRA_KEK_CIPHERTEXT"

REGION_ENV: Final[str] = "AWS_REGION"

#: Encryption context. KMS binds it to the ciphertext and refuses a decrypt that
#: presents a different one, which makes a KEK ciphertext lifted from one
#: deployment useless in another even by somebody holding both.
CONTEXT: Final[dict[str, str]] = {"application": "supracloud-ira", "purpose": "kek"}

#: 256-bit, matching what `secrets.py` requires of a KEK.
KEY_SPEC: Final[str] = "AES_256"

_RETRIABLE: Final[frozenset[str]] = frozenset({
    "ThrottlingException", "KMSInternalException", "RequestTimeout",
    "ServiceUnavailable", "InternalFailure", "TooManyRequestsException",
})

#: Not retried, ever. These are configuration and authorisation failures: the
#: same call will fail the same way in 200ms, and retrying turns a clear error
#: into a slow one while adding load to a service that is refusing on purpose.
_TERMINAL: Final[frozenset[str]] = frozenset({
    "AccessDeniedException", "NotFoundException", "DisabledException",
    "KMSInvalidStateException", "InvalidCiphertextException",
    "IncorrectKeyException", "UnrecognizedClientException",
})

_ATTEMPTS: Final[int] = 4
_BACKOFF: Final[float] = 0.2

_lock = threading.Lock()
_cached: Optional[tuple[bytes, str]] = None

#: The botocore loggers that emit request and response bodies. `parsers` is the
#: one that matters — it logs the decrypted `Plaintext` — but `endpoint` and
#: `httpsession` carry request bodies and headers and cost nothing to include.
_CHATTY: Final[tuple[str, ...]] = (
    "botocore.parsers", "botocore.endpoint", "botocore.httpsession",
    "botocore.hooks", "urllib3.connectionpool",
)


class _quiet_botocore:
    """Suppress body-level botocore logging for the duration of a KMS call.

    Scoped to the call, and restored in a `finally`, so an exception inside KMS
    handling cannot leave the application's logging permanently altered — a
    "fix" that silently disabled everyone's HTTP debugging would be its own
    defect.
    """

    def __init__(self) -> None:
        self._previous: dict[str, int] = {}

    def __enter__(self) -> "_quiet_botocore":
        for name in _CHATTY:
            log = logging.getLogger(name)
            self._previous[name] = log.level
            if log.level < logging.INFO:
                log.setLevel(logging.INFO)
        return self

    def __exit__(self, *exc) -> None:
        for name, level in self._previous.items():
            logging.getLogger(name).setLevel(level)
        return None


class KmsUnavailable(RuntimeError):
    """KMS could not answer, or refused. Never a fallback — always a refusal."""


def _client(env: Optional[dict[str, str]] = None):
    source = os.environ if env is None else env
    region = (source.get(REGION_ENV) or "").strip()
    if not region:
        raise KmsUnavailable(
            f"{REGION_ENV} is not set, so the KMS client has no endpoint to "
            f"talk to. This is configuration, not a transient fault.")
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - packaging
        raise KmsUnavailable(
            "boto3 is not installed, so the aws-kms key provider cannot be "
            "used. Install it or select a different IRA_KEK_PROVIDER — there "
            "is no fallback to the development key."
        ) from exc
    return boto3.client("kms", region_name=region)


def _call(operation, *, what: str, **kwargs):
    """One KMS call, with bounded retries on transient faults only.

    Terminal errors are re-raised immediately and named. Distinguishing them
    matters operationally: `AccessDeniedException` means fix the IAM policy and
    `ThrottlingException` means wait, and a loop that treats both the same turns
    the first into a four-attempt delay before the same failure.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    last: Optional[Exception] = None
    for attempt in range(_ATTEMPTS):
        try:
            with _quiet_botocore():
                return operation(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _TERMINAL or code not in _RETRIABLE:
                raise KmsUnavailable(
                    f"KMS refused to {what}: {code}. This is not retried — it "
                    f"is a configuration or permission failure and the next "
                    f"attempt fails identically."
                ) from exc
            last = exc
        except BotoCoreError as exc:
            last = exc
        if attempt < _ATTEMPTS - 1:
            delay = _BACKOFF * (2 ** attempt)
            logger.warning("KMS %s failed, retrying in %.1fs (attempt %d/%d)",
                           what, delay, attempt + 1, _ATTEMPTS)
            time.sleep(delay)
    raise KmsUnavailable(
        f"KMS could not {what} after {_ATTEMPTS} attempts. Refusing rather "
        f"than continuing without the key-encryption key."
    ) from last


def create_kek(env: Optional[dict[str, str]] = None) -> str:
    """Mint a KEK under the configured KMS key. Returns its CIPHERTEXT, base64.

    Operator tooling only — nothing in the request path calls this. The
    plaintext is dropped before returning: a function that handed both back
    would make the caller a place the KEK can be logged, and the caller does not
    need it. To use the new KEK, set `IRA_KEK_CIPHERTEXT` to what this returns.
    """
    source = os.environ if env is None else env
    key_id = (source.get(KEY_ID_ENV) or "").strip()
    if not key_id:
        raise KmsUnavailable(f"{KEY_ID_ENV} is not set")

    client = _client(env)
    response = _call(client.generate_data_key, what="generate a data key",
                     KeyId=key_id, KeySpec=KEY_SPEC, EncryptionContext=CONTEXT)
    plaintext = response["Plaintext"]
    try:
        if len(plaintext) != 32:  # pragma: no cover - KMS contract
            raise KmsUnavailable(
                f"KMS returned a {len(plaintext)}-byte data key; AES-256-GCM "
                f"needs 32. Refusing rather than padding or truncating.")
        logger.info("minted a new KEK under KMS key %s", key_id)
        return base64.b64encode(response["CiphertextBlob"]).decode()
    finally:
        # Best effort: Python cannot guarantee erasure, but not holding a
        # reference is the difference between "collected soon" and "alive for
        # the life of the process".
        del plaintext


def load_kek(env: Optional[dict[str, str]] = None) -> tuple[bytes, str]:
    """The plaintext KEK and its id, decrypted through KMS. Cached per process.

    The id is the KMS key ARN KMS reports for the ciphertext — NOT the
    configured value, which may be an alias. Recording the alias would mean a
    row wrapped before an alias moved and one wrapped after it are
    indistinguishable, and `_unwrap_dek` refuses on a `kek_id` mismatch, so the
    two must not be conflated.
    """
    global _cached

    with _lock:
        if _cached is not None:
            return _cached

        source = os.environ if env is None else env
        blob = (source.get(CIPHERTEXT_ENV) or "").strip()
        if not blob:
            raise KmsUnavailable(
                f"{CIPHERTEXT_ENV} is not set. The aws-kms provider needs the "
                f"KEK ciphertext to decrypt; there is no fallback to a "
                f"plaintext key in the environment, because silently using one "
                f"is exactly what selecting this provider rejects.")
        try:
            ciphertext = base64.b64decode(blob, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise KmsUnavailable(
                f"{CIPHERTEXT_ENV} is not valid base64.") from exc

        client = _client(env)
        response = _call(client.decrypt, what="decrypt the KEK",
                         CiphertextBlob=ciphertext, EncryptionContext=CONTEXT)
        key = response["Plaintext"]
        if len(key) != 32:
            raise KmsUnavailable(
                f"the decrypted KEK is {len(key)} bytes; AES-256-GCM needs 32.")
        key_id = response.get("KeyId") or ""
        if not key_id:  # pragma: no cover - KMS contract
            raise KmsUnavailable("KMS did not report which key it used")

        logger.info("KEK loaded from KMS key %s", key_id)
        _cached = (key, key_id)
        return _cached


def reset_cache() -> None:
    """Drop the cached KEK. For rotation and for tests.

    Rotation needs it because the point of rotating is that the next load gets a
    different key; without this the old one survives until the process does.
    """
    global _cached

    with _lock:
        _cached = None


def health(env: Optional[dict[str, str]] = None) -> tuple[bool, str]:
    """Whether KMS can currently open the KEK. For `/readyz`.

    Returns a reason, never key material, and deliberately does a real decrypt
    rather than a `DescribeKey`: permission to describe a key is not permission
    to use it, and a readiness probe that passes while decryption is denied is
    the probe reporting the wrong thing.
    """
    try:
        reset_cache()
        load_kek(env)
        return True, "kms: the key-encryption key was decrypted"
    except KmsUnavailable as exc:
        return False, f"kms: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"kms: unexpected {type(exc).__name__}"


__all__ = ["KmsUnavailable", "create_kek", "load_kek", "reset_cache", "health",
           "KEY_ID_ENV", "CIPHERTEXT_ENV", "REGION_ENV", "CONTEXT"]
