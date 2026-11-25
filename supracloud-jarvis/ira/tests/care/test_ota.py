"""The OTA update rules (care/ota.py, E4.d), without a database.

A downgrade is never offered; withdrawn and current images are not offered; the staged
rollout is fixed per device; the answer is signed and a tampered, foreign or expired manifest
fails the device's check; no key means no answer."""
from __future__ import annotations

import base64
import datetime as dt
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from care import ota

NOW = dt.datetime(2026, 11, 25, 12, 0, tzinfo=dt.timezone.utc)


def rel(rid: str, index: int, *, pct: int = 100, withdrawn: bool = False, fp: str | None = None,
        when: dt.datetime = NOW) -> ota.Release:
    return ota.Release(id=rid, version=f"1.0.{index}-{rid}", build_fingerprint=fp or f"ira/ira_watch_cf/{rid}:user",
                       rollback_index=index, payload_url=f"https://updates.example.invalid/{rid}.zip",
                       payload_sha256="a" * 64, payload_size=1000, rollout_percent=pct, withdrawn=withdrawn,
                       published_at=when)


def test_a_release_with_a_lower_rollback_index_is_never_offered():
    old, cur = rel("old", 2), rel("cur", 5)
    assert ota.choose([old], device_id="d1", build_fingerprint="x" * 10, rollback_index=5) is None
    assert ota.choose([old, cur], device_id="d1", build_fingerprint="x" * 10, rollback_index=5) == cur
    # The newest offerable one wins, and an equal index is allowed (same security level, new build).
    newer = rel("new", 5, when=NOW + dt.timedelta(days=1))
    assert ota.choose([cur, newer], device_id="d1", build_fingerprint="x" * 10, rollback_index=5) == newer


def test_the_running_image_and_withdrawn_images_are_not_offered():
    cur = rel("cur", 5)
    assert ota.choose([cur], device_id="d1", build_fingerprint=cur.build_fingerprint, rollback_index=5) is None
    assert ota.choose([rel("w", 9, withdrawn=True)], device_id="d1", build_fingerprint="x" * 10, rollback_index=0) is None


def test_the_rollout_bucket_is_fixed_per_device_and_spreads_devices():
    assert ota.rollout_bucket("r1", "d1") == ota.rollout_bucket("r1", "d1")
    buckets = [ota.rollout_bucket("r1", f"device-{i}") for i in range(2000)]
    assert all(0 <= b < 100 for b in buckets)
    share = sum(b < 10 for b in buckets) / len(buckets)
    assert 0.07 < share < 0.13, share
    # Widening a stage never drops a device that was in it.
    r10, r50 = rel("r1", 3, pct=10), rel("r1", 3, pct=50)
    for i in range(500):
        if ota.in_rollout(r10, f"device-{i}"):
            assert ota.in_rollout(r50, f"device-{i}")
    assert not ota.in_rollout(rel("r1", 3, pct=0), "any")


def _key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


def test_the_manifest_is_signed_and_the_device_check_accepts_it():
    signed = ota.sign(ota.manifest_for(rel("r1", 3), device_id="d1", now=NOW), _key())
    got = ota.verify(signed, _key().public_key(), device_id="d1", now=NOW)
    assert got["rollback_index"] == 3 and got["payload"]["sha256"] == "a" * 64
    assert signed["algorithm"] == "Ed25519" and len(signed["key_id"]) == 16


@pytest.mark.parametrize("tamper", ["rollback", "url", "signature"])
def test_a_tampered_manifest_fails_the_device_check(tamper):
    signed = ota.sign(ota.manifest_for(rel("r1", 3), device_id="d1", now=NOW), _key())
    if tamper == "signature":
        sig = bytearray(base64.b64decode(signed["signature"]))
        sig[0] ^= 1
        signed["signature"] = base64.b64encode(bytes(sig)).decode()
    else:
        body = json.loads(signed["manifest"])
        if tamper == "rollback":
            body["rollback_index"] = 0
        else:
            body["payload"]["url"] = "https://attacker.example.invalid/evil.zip"
        signed["manifest"] = ota.canonical(body).decode()
    with pytest.raises(ValueError, match="signature"):
        ota.verify(signed, _key().public_key(), device_id="d1", now=NOW)


def test_another_key_another_device_or_an_old_manifest_fails():
    signed = ota.sign(ota.manifest_for(rel("r1", 3), device_id="d1", now=NOW), _key())
    other = Ed25519PrivateKey.generate().public_key()
    with pytest.raises(ValueError, match="signature"):
        ota.verify(signed, other, device_id="d1", now=NOW)
    with pytest.raises(ValueError, match="another device"):
        ota.verify(signed, _key().public_key(), device_id="d2", now=NOW)
    with pytest.raises(ValueError, match="expired"):
        ota.verify(signed, _key().public_key(), device_id="d1", now=NOW + dt.timedelta(hours=2))


@pytest.mark.parametrize("env", [{}, {ota.SIGNING_KEY_ENV: "not base64!"},
                                 {ota.SIGNING_KEY_ENV: base64.b64encode(b"short").decode()}])
def test_no_valid_signing_key_means_no_answer(env):
    with pytest.raises(ota.SigningNotConfigured):
        ota.load_signing_key(env)


def test_keygen_produces_a_key_the_server_loads():
    from scripts.os_release import keygen
    private, public = keygen()
    key = ota.load_signing_key({ota.SIGNING_KEY_ENV: private})
    assert base64.b64decode(public) == key.public_key().public_bytes_raw()
