"""Tests for master-key verification (stdlib) and AES-256-GCM body cipher."""
import pytest

from armory import vault_encryption as venc


# ── verifier (stdlib PBKDF2, always available) ────────────────────────────────

def test_hash_and_verify_roundtrip():
    stored = venc.hash_master_key("correct horse battery", iterations=1000)
    assert stored.startswith("pbkdf2_sha256$")
    assert venc.verify_master_key("correct horse battery", stored) is True
    assert venc.verify_master_key("wrong key", stored) is False


def test_verifier_contains_no_plaintext():
    stored = venc.hash_master_key("s3cr3t-master-key", iterations=1000)
    assert "s3cr3t-master-key" not in stored


def test_verify_is_fail_closed_on_garbage():
    assert venc.verify_master_key("k", "") is False
    assert venc.verify_master_key("k", "not$a$valid$verifier$extra") is False
    assert venc.verify_master_key("", "pbkdf2_sha256$1000$AAAA$BBBB") is False


def test_empty_master_key_rejected():
    with pytest.raises(ValueError):
        venc.hash_master_key("   ")


# ── AES-256-GCM body (requires cryptography) ──────────────────────────────────

def test_encrypt_decrypt_roundtrip():
    pytest.importorskip("cryptography")
    blob = venc.encrypt(b"manifest + whitelist bytes", "master-key", iterations=1000)
    assert venc.looks_encrypted(blob) is True
    assert b"manifest" not in blob  # sealed body is ciphertext
    assert venc.decrypt(blob, "master-key") == b"manifest + whitelist bytes"


def test_wrong_key_fails_to_decrypt():
    pytest.importorskip("cryptography")
    blob = venc.encrypt(b"secret", "right-key", iterations=1000)
    with pytest.raises(Exception):
        venc.decrypt(blob, "wrong-key")


def test_tamper_is_detected():
    pytest.importorskip("cryptography")
    blob = bytearray(venc.encrypt(b"secret", "k", iterations=1000))
    blob[-1] ^= 0x01  # flip a ciphertext bit
    with pytest.raises(Exception):
        venc.decrypt(bytes(blob), "k")


def test_looks_encrypted_false_for_plaintext():
    assert venc.looks_encrypted(b"just some plaintext, not a vault blob") is False
