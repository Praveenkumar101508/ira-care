"""Pairing codes and device tokens (care/devices.py)."""
from __future__ import annotations

import uuid

from care import devices

T = uuid.UUID("aaaaaaaa-0000-4000-8000-00000000000a")
W = uuid.UUID("bbbbbbbb-0000-4000-8000-00000000000b")


def test_a_pairing_code_round_trips_its_scope():
    code = devices.new_pairing_code(T, W)
    parsed = devices.parse(code.encode())
    assert parsed is not None
    assert (parsed.tenant_id, parsed.workspace_id, parsed.kind) == (T, W, "pairing")
    assert parsed.secret == code.secret


def test_a_device_token_round_trips_its_scope_and_is_not_a_pairing_code():
    token = devices.new_device_token(T, W)
    parsed = devices.parse(token.encode())
    assert parsed is not None and parsed.kind == "token"
    assert parsed.tenant_id == T and parsed.workspace_id == W


def test_two_codes_for_the_same_scope_differ():
    a, b = devices.new_pairing_code(T, W), devices.new_pairing_code(T, W)
    assert a.secret != b.secret


def test_pairing_secrets_avoid_ambiguous_characters():
    for _ in range(50):
        secret = devices.new_pairing_code(T, W).secret
        assert len(secret) == 8
        assert not set(secret) & set("01IO")


def test_the_stored_form_is_a_hex_sha256_of_the_whole_string():
    encoded = devices.new_device_token(T, W).encode()
    d = devices.digest(encoded)
    assert len(d) == 64 and int(d, 16) >= 0
    assert d != devices.digest(encoded + "x")


def test_malformed_values_parse_to_none_not_an_exception():
    for bad in ("", "cd1", "cd1.x", "cd1.notbase64!!.secret", "zz9.AAAA.secret",
                "cd1..secret", "cd1.AAAA.", "cd1." + "A" * 10 + ".s", 12345, None,
                "cd1." + "A" * 43 + ".secret.extra", "x" * 400):
        assert devices.parse(bad) is None, bad


def test_a_tampered_scope_does_not_decode_to_the_original():
    """Changing the scope changes the tenant the credential claims; the hash
    check inside that other scope is what then fails (repository tests)."""
    encoded = devices.new_device_token(T, W).encode()
    prefix, scope, secret = encoded.split(".")
    other = devices.parse(f"{prefix}.{scope[:-2]}AA.{secret}")
    assert other is None or (other.tenant_id, other.workspace_id) != (T, W)


def test_the_pairing_window_is_short():
    assert devices.PAIRING_CODE_TTL.total_seconds() <= 15 * 60
