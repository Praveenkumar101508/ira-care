"""Phase 7 — the credential seal actually seals.

`test_credential_boundary.py` proves nothing outside `core/connectors/` can
reach a credential. This file proves the sealing is real: that a stored blob is
not the token, that tampering is detected, and that the failure modes are
distinguishable.

The distinction that matters most is the last one. A tampered ciphertext and an
expired grant look identical to a user ("Gmail stopped working") and must not
look identical to an operator — one is a corruption or a key change, the other
is routine. Reporting the first as an expiry sends everybody looking in the
wrong place.
"""
from __future__ import annotations

import base64

import pytest

from core.connectors.secrets import (
    KEY_BYTES,
    CredentialTampered,
    MissingEncryptionKey,
    SealedSecret,
    SecretsError,
    fingerprint,
    is_configured,
    rewrap,
    seal,
    use_secret,
)

TOKEN = "1//0eXaMpLeReFrEsHtOkEnThatLooksRealEnough"
AAD = "oauth:tenant-a:gmail"

KEY_A = base64.b64encode(b"a" * KEY_BYTES).decode()
KEY_B = base64.b64encode(b"b" * KEY_BYTES).decode()
ENV_A = {"IRA_CONNECTOR_ENCRYPTION_KEY": KEY_A, "IRA_CONNECTOR_ENCRYPTION_KEY_ID": "k1"}
ENV_B = {"IRA_CONNECTOR_ENCRYPTION_KEY": KEY_B, "IRA_CONNECTOR_ENCRYPTION_KEY_ID": "k2"}


# ── The seal is real ─────────────────────────────────────────────────────────


def test_a_round_trip_returns_the_token_to_the_callback():
    sealed = seal(TOKEN, aad=AAD, env=ENV_A)
    assert use_secret(sealed, lambda t: t, aad=AAD, env=ENV_A) == TOKEN


def test_the_stored_blob_does_not_contain_the_token():
    """The whole point. If the ciphertext contained the plaintext, every other
    test here would pass while the database held readable refresh tokens."""
    sealed = seal(TOKEN, aad=AAD, env=ENV_A)
    assert TOKEN.encode() not in sealed.ciphertext
    assert not sealed.ciphertext[:8].startswith(b"1//")


def test_the_ciphertext_would_be_accepted_by_the_schema_check():
    """`028_connectors.sql` rejects blobs beginning with a known token prefix.
    Real ciphertext must not trip that alarm, or every connect would fail."""
    for _ in range(20):
        head = seal(TOKEN, aad=AAD, env=ENV_A).ciphertext[:8]
        for prefix in (b"1//", b"ya29.", b"gho_", b"ghp_", b"eyJ"):
            assert not head.startswith(prefix)


def test_encrypting_the_same_token_twice_gives_different_ciphertext():
    """A fresh nonce each time. Identical ciphertext for identical input would
    let an observer of the table tell which users share a token, and GCM nonce
    reuse under one key is catastrophic."""
    a = seal(TOKEN, aad=AAD, env=ENV_A)
    b = seal(TOKEN, aad=AAD, env=ENV_A)
    assert a.ciphertext != b.ciphertext
    assert a.nonce != b.nonce


def test_the_nonce_is_never_accepted_from_a_caller():
    """GCM nonce reuse under the same key is catastrophic, so the nonce is
    generated inside `seal` and there is no parameter to override it."""
    import inspect

    assert "nonce" not in inspect.signature(seal).parameters


def test_the_key_id_is_recorded_so_a_rotation_can_find_its_work():
    assert seal(TOKEN, aad=AAD, env=ENV_A).key_id == "k1"
    assert seal(TOKEN, aad=AAD, env=ENV_B).key_id == "k2"


def test_an_unset_key_id_defaults_rather_than_failing():
    assert seal(TOKEN, env={"IRA_CONNECTOR_ENCRYPTION_KEY": KEY_A}).key_id == "default"


# ── Tampering and misuse are detected, and distinguishable ───────────────────


def test_a_modified_ciphertext_is_detected():
    """AES-GCM is authenticated, so a database-write attacker cannot flip bits
    into a different-but-valid token."""
    sealed = seal(TOKEN, aad=AAD, env=ENV_A)
    corrupted = SealedSecret(
        ciphertext=bytes([sealed.ciphertext[0] ^ 0xFF]) + sealed.ciphertext[1:],
        key_id=sealed.key_id,
        nonce=sealed.nonce,
    )
    with pytest.raises(CredentialTampered):
        use_secret(corrupted, lambda t: t, aad=AAD, env=ENV_A)


def test_the_wrong_key_is_detected_and_not_reported_as_an_expiry():
    """The message matters: a key change and an expired grant look the same to a
    user and must not look the same to an operator."""
    sealed = seal(TOKEN, aad=AAD, env=ENV_A)
    with pytest.raises(CredentialTampered) as exc:
        use_secret(sealed, lambda t: t, aad=AAD, env=ENV_B)
    assert "NOT an expiry" in str(exc.value)


def test_a_credential_cannot_be_moved_between_accounts():
    """The AAD binds a ciphertext to its row. Without it, a database-write
    attacker could paste one user's credential into another user's account."""
    sealed = seal(TOKEN, aad="oauth:tenant-a:gmail", env=ENV_A)
    with pytest.raises(CredentialTampered):
        use_secret(sealed, lambda t: t, aad="oauth:tenant-b:gmail", env=ENV_A)


def test_a_truncated_ciphertext_is_detected():
    sealed = seal(TOKEN, aad=AAD, env=ENV_A)
    truncated = SealedSecret(sealed.ciphertext[:-4], sealed.key_id, sealed.nonce)
    with pytest.raises(CredentialTampered):
        use_secret(truncated, lambda t: t, aad=AAD, env=ENV_A)


def test_a_swapped_nonce_is_detected():
    a = seal(TOKEN, aad=AAD, env=ENV_A)
    b = seal(TOKEN, aad=AAD, env=ENV_A)
    with pytest.raises(CredentialTampered):
        use_secret(SealedSecret(a.ciphertext, a.key_id, b.nonce),
                   lambda t: t, aad=AAD, env=ENV_A)


# ── Fail closed on configuration ─────────────────────────────────────────────


def test_no_key_refuses_to_seal():
    with pytest.raises(MissingEncryptionKey):
        seal(TOKEN, env={})


def test_a_short_key_is_a_misconfiguration_not_a_weaker_option():
    env = {"IRA_CONNECTOR_ENCRYPTION_KEY": base64.b64encode(b"short").decode()}
    with pytest.raises(MissingEncryptionKey) as exc:
        seal(TOKEN, env=env)
    assert "32" in str(exc.value)


def test_a_non_base64_key_says_so():
    with pytest.raises(MissingEncryptionKey) as exc:
        seal(TOKEN, env={"IRA_CONNECTOR_ENCRYPTION_KEY": "not!base64!"})
    assert "base64" in str(exc.value)


def test_a_key_is_never_generated_on_the_fly():
    """A generated key would make the first run work and every later run fail to
    decrypt — which presents as "Google disconnected me" and sends everyone in
    the wrong direction."""
    assert is_configured({}) is False
    with pytest.raises(MissingEncryptionKey):
        seal(TOKEN, env={})


def test_an_empty_credential_is_refused():
    """Sealing an empty string would store a valid-looking row with no token,
    which fails later at the first API call."""
    with pytest.raises(SecretsError):
        seal("", env=ENV_A)


def test_the_error_tells_an_operator_how_to_fix_it():
    with pytest.raises(MissingEncryptionKey) as exc:
        seal(TOKEN, env={})
    assert "base64" in str(exc.value) and "urandom(32)" in str(exc.value)


# ── Nothing leaks through repr ────────────────────────────────────────────────


def test_the_sealed_secret_does_not_print_its_ciphertext():
    """Not plaintext, but a ciphertext in a bug report is still an artefact
    nobody should be casually copying around."""
    sealed = seal(TOKEN, aad=AAD, env=ENV_A)
    assert str(sealed.ciphertext) not in repr(sealed)
    assert sealed.key_id in repr(sealed)


def test_the_fingerprint_identifies_without_revealing():
    """Lets an operator tell "same credential as before" from "a new one"
    without printing either."""
    sealed = seal(TOKEN, aad=AAD, env=ENV_A)
    label = fingerprint(sealed)
    assert TOKEN not in label
    assert label.startswith("k1:")
    assert fingerprint(sealed) == label
    assert fingerprint(seal(TOKEN, aad=AAD, env=ENV_A)) != label, (
        "a re-sealed credential is a different stored object and should look it"
    )


# ── Rotation exists and is honest about being unused ─────────────────────────


def test_rewrap_moves_a_credential_to_the_current_key():
    sealed = seal(TOKEN, aad=AAD, env=ENV_A)
    moved = rewrap(sealed, aad=AAD, old_env=ENV_A, new_env=ENV_B)
    assert moved.key_id == "k2"
    assert use_secret(moved, lambda t: t, aad=AAD, env=ENV_B) == TOKEN
    with pytest.raises(CredentialTampered):
        use_secret(moved, lambda t: t, aad=AAD, env=ENV_A)


def test_nothing_calls_rewrap_yet_and_the_docstring_says_so():
    """There is no rotation job in this phase. Stated plainly rather than
    implied by the function existing."""
    doc = (rewrap.__doc__ or "").lower()
    assert "nothing calls it yet" in doc


# ── The callback shape ───────────────────────────────────────────────────────


def test_only_the_callbacks_result_is_returned():
    sealed = seal(TOKEN, aad=AAD, env=ENV_A)
    assert use_secret(sealed, lambda t: len(t), aad=AAD, env=ENV_A) == len(TOKEN)


def test_an_exception_inside_the_callback_propagates():
    """Swallowing it would turn a connector bug into a silent no-op."""
    sealed = seal(TOKEN, aad=AAD, env=ENV_A)
    with pytest.raises(ValueError):
        use_secret(sealed, lambda t: (_ for _ in ()).throw(ValueError("boom")),
                   aad=AAD, env=ENV_A)


@pytest.mark.asyncio
async def test_the_async_form_works_for_awaitable_callbacks():
    from core.connectors.secrets import use_secret_async

    sealed = seal(TOKEN, aad=AAD, env=ENV_A)

    async def fetch(token: str) -> int:
        return len(token)

    assert await use_secret_async(sealed, fetch, aad=AAD, env=ENV_A) == len(TOKEN)
