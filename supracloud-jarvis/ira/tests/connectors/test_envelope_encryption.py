"""Blocker B3 — the envelope, checked as cryptography rather than as intent.

`RELEASE_READINESS.md` R11 named three faults. This file covers the two that
live in `core/connectors/secrets.py` and need no database:

    a leaked key compromises every stored credential at once
    `rewrap` exists with no caller, so rotating the key re-encrypts nothing

The third — no crypto-shredding — needs a real database and lives in
`tests/tenancy/test_key_management_rls.py`.

Everything here does real AES-256-GCM against real random keys. Nothing is
mocked, because the property under test *is* the cryptography: a test that
asserts `wrap_dek` was called proves the call happened, not that the result is
unopenable by the wrong key.

The discipline throughout: **prove the guard by planting what it exists to
catch.** A test that only shows the happy path passes equally well against a
`seal` that returns its input.
"""
from __future__ import annotations

import base64
import os

import pytest

from core.connectors.secrets import (
    DEK_KEY_PREFIX,
    KEY_BYTES,
    CredentialTampered,
    KeyNotResolved,
    MissingEncryptionKey,
    SealedSecret,
    SecretsError,
    _open_secret,
    _unwrap_dek,
    configured_kek_id,
    dek_key_id,
    dek_version_of,
    generate_dek,
    rewrap,
    seal,
    seal_with,
    use_secret,
    wrap_dek,
)

TOKEN = "1//0gLONG-LOOKING-REFRESH-TOKEN-that-must-never-leak"


def _env(kek: bytes | None = None, key_id: str = "kek-test") -> dict[str, str]:
    material = kek if kek is not None else os.urandom(KEY_BYTES)
    return {
        "IRA_CONNECTOR_ENCRYPTION_KEY": base64.b64encode(material).decode(),
        "IRA_CONNECTOR_ENCRYPTION_KEY_ID": key_id,
    }


ENV = _env()
OTHER_ENV = _env(key_id="kek-other")


# ── Key material ─────────────────────────────────────────────────────────────


def test_a_generated_dek_is_the_right_size_and_not_a_constant():
    """Two DEKs from the same process must differ.

    The failure this catches is a `generate_dek` that derives from the tenant or
    returns a module-level constant — either of which would make every
    deployment's key for a given tenant identical and every "per-tenant key"
    claim false.
    """
    keys = {generate_dek() for _ in range(64)}
    assert len(keys) == 64, "generate_dek repeated itself — it is not random"
    assert all(len(k) == KEY_BYTES for k in keys)


def test_a_dek_round_trips_through_the_wrap():
    dek = generate_dek()
    wrapped = wrap_dek(dek, aad="dek:t:p:v1:kek-test", env=ENV)
    assert wrapped.material != dek, "the 'wrapped' key is the key in plain sight"
    assert _unwrap_dek(wrapped, aad="dek:t:p:v1:kek-test", env=ENV) == dek


def test_wrapping_refuses_a_key_of_the_wrong_length():
    with pytest.raises(SecretsError, match="32 bytes"):
        wrap_dek(b"too short", aad="dek:t:p:v1:kek-test", env=ENV)


def test_wrapping_refuses_an_empty_aad():
    """The AAD is what stops a wrapped key being moved between tenants, so
    wrapping without one is refused rather than quietly allowed."""
    with pytest.raises(SecretsError, match="AAD"):
        wrap_dek(generate_dek(), aad="", env=ENV)


def test_a_wrapped_key_moved_to_another_tenants_row_will_not_unwrap():
    """The attack: a database-write attacker copies tenant A's wrapped DEK into
    tenant B's row. RLS blocks the read path; this blocks it a second time, in
    the cryptography, where it does not depend on a session variable."""
    dek = generate_dek()
    wrapped = wrap_dek(dek, aad="dek:TENANT-A:connector_credentials:v1:kek-test", env=ENV)

    with pytest.raises(CredentialTampered):
        _unwrap_dek(
            wrapped, aad="dek:TENANT-B:connector_credentials:v1:kek-test", env=ENV
        )


def test_a_wrapped_key_moved_to_another_version_will_not_unwrap():
    """Same attack aimed at a rotation: present v1's wrapped key as v2's."""
    dek = generate_dek()
    wrapped = wrap_dek(dek, aad="dek:t:connector_credentials:v1:kek-test", env=ENV)

    with pytest.raises(CredentialTampered):
        _unwrap_dek(wrapped, aad="dek:t:connector_credentials:v2:kek-test", env=ENV)


def test_a_key_wrapped_under_a_different_kek_says_so_rather_than_tampered():
    """A KEK mismatch is a misconfiguration, not an attack, and reporting it as
    tampering sends an operator looking for an intruder instead of for the
    variable they changed."""
    dek = generate_dek()
    wrapped = wrap_dek(dek, aad="dek:t:p:v1:kek-test", env=ENV)

    with pytest.raises(CredentialTampered, match="configured KEK"):
        _unwrap_dek(wrapped, aad="dek:t:p:v1:kek-test", env=OTHER_ENV)


def test_unwrapping_with_no_kek_configured_refuses():
    """"Missing encryption key" is on the brief's fail-closed list."""
    dek = generate_dek()
    wrapped = wrap_dek(dek, aad="dek:t:p:v1:kek-test", env=ENV)

    with pytest.raises(MissingEncryptionKey):
        _unwrap_dek(wrapped, aad="dek:t:p:v1:kek-test", env={})


def test_the_kek_id_is_readable_without_the_kek():
    """Callers need the id to build an AAD. That is not a reason to hand them
    the key, so the accessor returns one and not the other."""
    assert configured_kek_id(ENV) == "kek-test"


# ── Key ids ──────────────────────────────────────────────────────────────────


def test_a_dek_key_id_round_trips():
    assert dek_key_id(3) == f"{DEK_KEY_PREFIX}3"
    assert dek_version_of(dek_key_id(3)) == 3


@pytest.mark.parametrize(
    "key_id", ["", "default", "kek-1", "dek:", "dek:x", "dek:0", "dek:-1", "3"]
)
def test_a_non_dek_key_id_reads_as_legacy_not_as_an_error(key_id):
    """Phase 7 rows carry ids like "default". None is the honest answer for
    them; raising would turn every pre-B3 credential into an error."""
    assert dek_version_of(key_id) is None


def test_versions_start_at_one():
    with pytest.raises(SecretsError):
        dek_key_id(0)


# ── Sealing under a DEK ──────────────────────────────────────────────────────


def test_a_credential_sealed_under_a_dek_round_trips():
    dek = generate_dek()
    sealed = seal_with(dek, TOKEN, aad="tenant:gmail", key_id=dek_key_id(1))

    assert TOKEN.encode() not in sealed.ciphertext, "the token is in the ciphertext"
    assert sealed.key_id == "dek:1"
    assert _open_secret(sealed, aad="tenant:gmail", key=dek) == TOKEN


def test_another_tenants_dek_cannot_open_it():
    """The blast-radius claim, made concrete. Before B3 there was one key, so
    this test could not have been written."""
    mine, theirs = generate_dek(), generate_dek()
    sealed = seal_with(mine, TOKEN, aad="tenant-a:gmail", key_id=dek_key_id(1))

    with pytest.raises(CredentialTampered):
        _open_secret(sealed, aad="tenant-a:gmail", key=theirs)


def test_the_kek_cannot_open_a_credential_sealed_under_a_dek():
    """The KEK's job is now wrapping keys and nothing else. If it could still
    open credentials directly, the envelope would be decoration."""
    dek = generate_dek()
    sealed = seal_with(dek, TOKEN, aad="tenant:gmail", key_id=dek_key_id(1))
    kek = base64.b64decode(ENV["IRA_CONNECTOR_ENCRYPTION_KEY"])

    with pytest.raises(CredentialTampered):
        _open_secret(sealed, aad="tenant:gmail", key=kek)


def test_sealing_under_a_dek_refuses_a_key_id_that_does_not_name_a_version():
    """A credential sealed under a DEK must record which one, or nothing can
    ever find the key again."""
    with pytest.raises(SecretsError, match="does not name a DEK version"):
        seal_with(generate_dek(), TOKEN, aad="a", key_id="default")


def test_sealing_refuses_an_empty_credential():
    with pytest.raises(SecretsError):
        seal_with(generate_dek(), "", aad="a", key_id=dek_key_id(1))


def test_two_seals_of_the_same_token_differ():
    """Fresh nonce per encryption. Identical ciphertext for identical input
    would leak "these two accounts share a token"."""
    dek = generate_dek()
    a = seal_with(dek, TOKEN, aad="x", key_id=dek_key_id(1))
    b = seal_with(dek, TOKEN, aad="x", key_id=dek_key_id(1))
    assert a.ciphertext != b.ciphertext
    assert a.nonce != b.nonce


# ── Fail closed: the two refusals that must not become fallbacks ─────────────


def test_opening_a_dek_credential_without_its_key_refuses():
    """The most important test in this file.

    If this fell back to the KEK, a bug in key resolution would present as
    "tampered" on a good day — and on the day some row happened to open, as a
    silent success under a key nobody chose.
    """
    dek = generate_dek()
    sealed = seal_with(dek, TOKEN, aad="tenant:gmail", key_id=dek_key_id(1))

    with pytest.raises(KeyNotResolved, match="keyring"):
        _open_secret(sealed, aad="tenant:gmail", env=ENV)


def test_supplying_a_dek_for_a_legacy_credential_refuses():
    """The mirror. A caller who supplies a key for a KEK-sealed row has
    confused the two schemes, and guessing which they meant is how one of them
    silently stops being used."""
    legacy = seal(TOKEN, aad="tenant:gmail", env=ENV)
    assert dek_version_of(legacy.key_id) is None

    with pytest.raises(KeyNotResolved, match="Refusing rather than guessing"):
        _open_secret(legacy, aad="tenant:gmail", env=ENV, key=generate_dek())


def test_use_secret_carries_the_key_through():
    """The callback form must work under a DEK too, or every call site would
    have to fall back to the raw decrypt."""
    dek = generate_dek()
    sealed = seal_with(dek, TOKEN, aad="a", key_id=dek_key_id(1))
    assert use_secret(sealed, lambda t: t.upper(), aad="a", key=dek) == TOKEN.upper()


def test_a_tampered_ciphertext_is_still_detected_under_a_dek():
    """GCM's authentication, still doing its job in the new scheme."""
    dek = generate_dek()
    sealed = seal_with(dek, TOKEN, aad="a", key_id=dek_key_id(1))
    corrupted = SealedSecret(
        ciphertext=bytes([sealed.ciphertext[0] ^ 0xFF]) + sealed.ciphertext[1:],
        key_id=sealed.key_id,
        nonce=sealed.nonce,
    )
    with pytest.raises(CredentialTampered):
        _open_secret(corrupted, aad="a", key=dek)


# ── rewrap: the function R11 said had no caller ──────────────────────────────


def test_rewrap_migrates_a_phase_7_credential_onto_a_dek():
    """The upgrade path. A credential sealed before B3 must survive."""
    legacy = seal(TOKEN, aad="tenant:gmail", env=ENV)
    dek = generate_dek()

    moved = rewrap(
        legacy, aad="tenant:gmail", old_env=ENV,
        new_key=dek, new_key_id=dek_key_id(1),
    )

    assert moved.key_id == "dek:1"
    assert moved.ciphertext != legacy.ciphertext, "rewrap returned the input"
    assert _open_secret(moved, aad="tenant:gmail", key=dek) == TOKEN


def test_rewrap_rotates_between_two_deks_and_the_old_key_stops_working():
    """What a rotation must actually do, asserted in both directions.

    The second half is the one that matters: a `rewrap` that returned its input
    would pass "the new key opens it" — because the old key would too.
    """
    old, new = generate_dek(), generate_dek()
    sealed = seal_with(old, TOKEN, aad="tenant:gmail", key_id=dek_key_id(1))

    rotated = rewrap(
        sealed, aad="tenant:gmail",
        old_key=old, new_key=new, new_key_id=dek_key_id(2),
    )

    assert rotated.key_id == "dek:2"
    assert _open_secret(rotated, aad="tenant:gmail", key=new) == TOKEN
    with pytest.raises(CredentialTampered):
        _open_secret(rotated, aad="tenant:gmail", key=old)


def test_rewrap_can_move_a_credential_back_off_a_dek():
    """DEK → KEK, so a rollback has a path that is not "reconnect everything"."""
    dek = generate_dek()
    sealed = seal_with(dek, TOKEN, aad="a", key_id=dek_key_id(1))

    back = rewrap(sealed, aad="a", old_key=dek, new_env=ENV)

    assert dek_version_of(back.key_id) is None
    assert _open_secret(back, aad="a", env=ENV) == TOKEN


def test_rewrap_onto_a_dek_without_a_key_id_refuses():
    with pytest.raises(SecretsError, match="key_id"):
        rewrap(
            seal(TOKEN, aad="a", env=ENV), aad="a",
            old_env=ENV, new_key=generate_dek(),
        )


def test_rewrap_under_the_wrong_old_key_refuses_rather_than_producing_garbage():
    """An interrupted rotation that re-sealed garbage would be undetectable
    later — the row would look fine and open to nonsense."""
    sealed = seal_with(generate_dek(), TOKEN, aad="a", key_id=dek_key_id(1))
    with pytest.raises(CredentialTampered):
        rewrap(
            sealed, aad="a", old_key=generate_dek(),
            new_key=generate_dek(), new_key_id=dek_key_id(2),
        )


# ── The scope boundary, pinned so it stays a decision ────────────────────────


def test_the_pkce_verifier_is_still_sealed_under_the_kek():
    """`oauth.begin` seals the PKCE verifier directly under the environment key,
    NOT under a DEK. That is deliberate — the verifier lives ten minutes, its
    row is deleted on consumption, and `begin` is a synchronous function on the
    redirect path where a database round-trip would be felt.

    Pinned here so the boundary stays a decision somebody made rather than an
    assumption somebody later relies on. If the verifier ever moves onto a DEK,
    this test should be deleted in the same commit that moves it.
    """
    from core.connectors.oauth import OAuthClient, begin

    client = OAuthClient(
        client_id="cid", client_secret="csec",
        redirect_uri="https://example.test/callback",
    )
    pending = begin(
        connector="gmail", operations=("search",),
        tenant_id="11111111-1111-1111-1111-111111111111",
        user_id="22222222-2222-4000-8000-222222222222",
        client=client, env=dict(ENV),
    )

    assert dek_version_of(pending.sealed_verifier.key_id) is None, (
        "the PKCE verifier is now sealed under a DEK. That may be the right "
        "call, but it changes the oauth.begin latency story and this test "
        "should be removed deliberately rather than updated to match."
    )
