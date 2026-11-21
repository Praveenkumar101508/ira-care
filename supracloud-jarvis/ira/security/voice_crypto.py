"""Envelope encryption for voice templates (Phase 4 batch 02).

    environment KEK  (core.connectors.secrets)
         ↓  wraps
    a per-RECORD voice DEK      voice_profiles.wrapped_dek
         ↓  encrypts
    the voiceprint template     voice_profiles.template_ciphertext

**No new cryptography.** Every primitive comes from `core.connectors.secrets` —
`generate_dek`, `wrap_dek`, `_unwrap_dek`, `seal_with`, `use_secret_async`. This
module supplies the key *hierarchy* and the AAD, not the algorithms. That is the
same division `security/mfa_crypto.py` observes, and for the same reason:
CLOUD-ADR-0012 requires encryption to be reachable only through a narrowly
scoped service, and two services inventing their own AES calls is two places to
get GCM wrong.

── Per RECORD, and why that differs from MFA ───────────────────────────────

`mfa_crypto` mints one DEK per USER, held in `user_mfa_keys`. That is
defensible for a TOTP seed, of which a person has exactly one — per-user and
per-record coincide.

They do not coincide here. `037` gave `voice_profiles` an `enrollment_version`
and a per-row `deletion_status` whose values include `crypto_shredded` as
distinct from `hard_deleted`, so one person may hold several enrolments and
revoke one of them. CLOUD-ADR-0012 says "a fresh data-encryption key for every
record" and gives exactly this reason: *"the per-record key is what makes
crypto-shredding a single enrolment possible."* A per-user key cannot express
it — destroying it would take every enrolment the person has.

So the key lives on the row, and shredding one enrolment is `wrapped_dek =
NULL` on that row and nothing else. `062`'s `voice_profiles_shredded_holds_no_key`
is what stops that being a claim rather than a fact.

── Why the environment KEK and not `tenant_data_keys` ──────────────────────

ADR-0012 names the `030` registry as the KEK layer, and for connector
credentials it is. For a biometric template it cannot be: `tenant_data_keys`
carries a tenant-only RLS policy, so any tenant-bound connection can read the
material that opens it — handing a tenant administrator the capability `037`'s
policies exist to withhold, and letting a tenant deletion cryptographically
destroy a former member's enrolment. D-17's analysis is the long form of this;
`mfa_crypto` reached the same conclusion. The hierarchy is unchanged, and the
layer holding the KEK simply sits outside the database, which is where ADR-0012
wants key material anyway.

── The AAD binds the ROW, not the person ───────────────────────────────────

`voice_aad` and `voice_wrap_aad` name the profile id and the enrolment version,
and deliberately NOT `user_id`.

Binding the user would look stronger and would be a defect. `037` backfills
`user_id` through the identity map *after* rows already exist, and Phase 12 will
attach ownership to enrolments that have none — so a template sealed against a
`user_id` would stop opening the moment ownership was resolved, which is a
silent, permanent loss of a biometric enrolment caused by an administrative
backfill.

`id` is the primary key and never changes. It is also sufficient for the threat
the AAD exists to stop: an attacker who can write to the table cannot move
Alice's ciphertext and wrapped key onto Bob's row, because Bob's row has a
different id and the unwrap fails in the cryptography rather than relying on a
session variable having been set.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from core.connectors.secrets import (
    KEY_BYTES,
    SealedSecret,
    SecretsError,
    WrappedKey,
    _unwrap_dek,
    dek_key_id,
    generate_dek,
    seal_with,
    use_secret_async,
    wrap_dek,
)

logger = logging.getLogger("ira.security.voice_crypto")

#: The key version stamped into `encryption_key_version`. One, because a
#: per-record key has no rotation of its own — rotating means re-wrapping the
#: record key under a new KEK, which changes `kek_id` and leaves this alone.
#: `dek_key_id` needs a version to build an id `seal_with` will accept.
VOICE_KEY_VERSION: int = 1


def voice_wrap_aad(profile_id: Any, enrollment_version: int) -> str:
    """Wrapping AAD for a record's voice DEK.

    Both components load-bearing: drop the id and a wrapped key can be moved
    between rows by anyone who can write to the table; drop the version and it
    can be moved across a re-enrolment, so a revoked template's key would open
    its replacement.
    """
    return f"voice-dek:{profile_id}:v{int(enrollment_version)}"


def voice_aad(profile_id: Any, enrollment_version: int) -> str:
    """AAD for the sealed template itself.

    Distinct from the wrapping AAD so a wrapped key and a sealed template can
    never be confused for one another by a decryption routine.
    """
    return f"voice-template:{profile_id}:v{int(enrollment_version)}"


class VoiceKeyUnavailable(Exception):
    """The template could not be opened — no key, a destroyed key, or one that
    does not fit.

    Deliberately distinct from "this person has no enrolment". A caller must not
    read "I cannot open the template" as "there is no voiceprint to match
    against", because the second answer lets verification proceed to whatever it
    does when nobody is enrolled. That is the fail-open direction, and on a
    biometric check it is the whole risk.
    """


def seal_template(
    profile_id: Any,
    enrollment_version: int,
    template: str,
    *,
    env: Optional[dict] = None,
) -> dict:
    """Seal one voice template under a freshly minted per-record key.

    Returns exactly the six columns `voice_profiles` stores:
    `template_ciphertext`, `template_nonce`, `wrapped_dek`, `wrap_nonce`,
    `kek_id`, `encryption_key_version`.

    The DEK is generated here and never returned. It exists in this frame and in
    the wrapped blob, and nowhere else — in particular not in the database,
    which is ADR-0012's "key material never lives in PostgreSQL".
    """
    if not template:
        raise SecretsError("refusing to seal an empty voice template")
    if profile_id is None:
        raise SecretsError(
            "refusing to seal a template with no profile id — the id is the AAD, "
            "and without it the ciphertext can be moved to another row")

    dek = generate_dek()
    try:
        wrapped: WrappedKey = wrap_dek(
            dek, aad=voice_wrap_aad(profile_id, enrollment_version), env=env)
        sealed: SealedSecret = seal_with(
            dek, template,
            aad=voice_aad(profile_id, enrollment_version),
            key_id=dek_key_id(VOICE_KEY_VERSION))
    finally:
        # Drops the last local reference promptly rather than leaving it bound
        # in this frame for the lifetime of whatever raised. Python cannot wipe
        # the bytes, and `secrets.py` says so rather than claiming otherwise —
        # the same honesty applies here.
        del dek

    return {
        "template_ciphertext": sealed.ciphertext,
        "template_nonce": sealed.nonce,
        "wrapped_dek": wrapped.material,
        "wrap_nonce": wrapped.nonce,
        "kek_id": wrapped.kek_id,
        "encryption_key_version": VOICE_KEY_VERSION,
    }


async def use_template(row, fn, *, env: Optional[dict] = None):
    """Open a sealed template, hand it to `fn`, and drop it.

    `row` is anything with the six columns above — an asyncpg Record or a dict.
    The plaintext is never returned; it is passed to `fn` and released, which is
    ADR-0012's "decrypted values are cached in process memory only, for the
    minimum duration". A function that returned the template would make every
    caller a place it can be logged.
    """
    for column in ("id", "enrollment_version", "template_ciphertext",
                   "template_nonce", "wrapped_dek", "wrap_nonce", "kek_id"):
        if row[column] is None:
            raise VoiceKeyUnavailable(
                f"this enrolment cannot be opened: {column} is absent. A row "
                f"marked ENCRYPTED that is missing any of these is ciphertext "
                f"nobody can read — `062`'s completeness constraint exists to "
                f"make that unrepresentable, so reaching here means the row "
                f"predates it or was written around it.")

    version = int(row["enrollment_version"])
    try:
        dek = _unwrap_dek(
            WrappedKey(material=bytes(row["wrapped_dek"]),
                       nonce=bytes(row["wrap_nonce"]),
                       kek_id=row["kek_id"]),
            aad=voice_wrap_aad(row["id"], version),
            env=env)
    except Exception as exc:  # noqa: BLE001
        # The message names no key material and no template. It also does not
        # distinguish "wrong KEK" from "AAD mismatch": the second means somebody
        # moved the row, and telling them which of their two attempts was closer
        # is a decryption oracle.
        raise VoiceKeyUnavailable(
            "this voice enrolment could not be opened") from exc

    try:
        return await use_secret_async(
            SealedSecret(ciphertext=bytes(row["template_ciphertext"]),
                         key_id=dek_key_id(int(row["encryption_key_version"]
                                               or VOICE_KEY_VERSION)),
                         nonce=bytes(row["template_nonce"])),
            fn,
            aad=voice_aad(row["id"], version),
            key=dek)
    except VoiceKeyUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise VoiceKeyUnavailable(
            "this voice enrolment could not be opened") from exc
    finally:
        del dek


__all__ = [
    "VOICE_KEY_VERSION",
    "VoiceKeyUnavailable",
    "rewrap_tenant_voice_keys",
    "rewrap_voice_key",
    "seal_template",
    "use_template",
    "voice_aad",
    "voice_wrap_aad",
]


async def rewrap_voice_key(conn, row, *, old_env: Optional[dict] = None,
                           new_env: Optional[dict] = None) -> bool:
    """Re-wrap ONE enrolment's record key under a new KEK. **No template opens.**

    The operation a KEK rotation needs, and the one `062` shipped without.

    ── Why this was missing, and what it would have cost ───────────────────

    `mfa_crypto`'s docstring states the hazard exactly: *"what WOULD strand them
    is a KEK rotation with nothing to follow it: every wrapped DEK names the KEK
    it was wrapped under, and `_unwrap_dek` refuses a mismatch."* P4-02 built a
    second population of KEK-wrapped keys — one per voice enrolment — and did
    not add the follow-up, so `rotate_tenant` would have rotated the KEK and
    left every voice template permanently unopenable.

    After the contract stage that is unrecoverable: `063` drops the plaintext,
    so the ciphertext is the only copy and its key no longer unwraps. A routine
    key rotation would have destroyed every enrolled voiceprint in the
    deployment, and reported success.

    ── Why two envs ────────────────────────────────────────────────────────

    `_unwrap_dek` REFUSES when the row's `kek_id` is not the configured one —
    correctly, since that turns a misconfigured key into a clear message rather
    than a forged-ciphertext alarm. But it means a single-env re-wrap can only
    ever re-wrap under the same KEK, which is exactly not the case rotation
    exists for.

    The record DEK and the enrolment version are unchanged, so the sealed
    template stays readable and nothing is decrypted.
    """
    if row["wrapped_dek"] is None:
        return False   # shredded, or never sealed — nothing to re-wrap

    aad = voice_wrap_aad(row["id"], row["enrollment_version"])
    dek = _unwrap_dek(
        WrappedKey(material=bytes(row["wrapped_dek"]),
                   nonce=bytes(row["wrap_nonce"]),
                   kek_id=row["kek_id"]),
        aad=aad, env=old_env)
    try:
        rewrapped = wrap_dek(dek, aad=aad, env=new_env)
    finally:
        del dek

    # The AAD is unchanged because the enrolment version is unchanged.
    # Re-wrapping is not a re-enrolment; a row that gained a version here would
    # strand the template sealed under the old one.
    await conn.execute(
        "UPDATE voice_profiles SET wrapped_dek = $2, wrap_nonce = $3, kek_id = $4 "
        " WHERE id = $1",
        row["id"], rewrapped.material, rewrapped.nonce, rewrapped.kek_id)
    return True


async def rewrap_tenant_voice_keys(
    principal, *, old_env: Optional[dict] = None, new_env: Optional[dict] = None,
) -> tuple[int, list[str]]:
    """Re-wrap every live enrolment this tenant can reach. Returns (count, failures).

    Failures are COLLECTED rather than raised. One unopenable row must not stop
    the rest of the rotation: leaving the others wrapped under a KEK that is
    about to be retired would turn one broken enrolment into all of them.

    Identifiers only in the failure list — never a template, never a key.
    """
    from core.identity.db_context import tenant_connection

    done: int = 0
    failures: list[str] = []
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            "SELECT id, enrollment_version, wrapped_dek, wrap_nonce, kek_id "
            "  FROM voice_profiles "
            " WHERE wrapped_dek IS NOT NULL AND deletion_status = 'live' "
            " ORDER BY created_at, id")
        for row in rows:
            try:
                if await rewrap_voice_key(conn, row, old_env=old_env,
                                          new_env=new_env):
                    done += 1
            except Exception:  # noqa: BLE001
                failures.append(str(row["id"]))
    return done, failures
