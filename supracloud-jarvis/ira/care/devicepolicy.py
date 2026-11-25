"""Signed deployment-administrator instructions for the watch's device policy (E2.m, E2.o).

On IRA CARE OS the watch keeps developer options, factory reset and safe-mode boot locked
(watch/app/src/main/java/care/ira/watch/devicepolicy/DevicePolicy.kt). A deployment
administrator (D-085; care/deviceadmin.py decides who that is) can send one watch one
instruction from a closed list:

    MAINTENANCE_WINDOW      lift the developer-options restriction until the instruction
                            expires (at most 15 minutes); the watch restores it by itself
    RESTORE_RESTRICTIONS    put every restriction back now (ends a window early)
    FACTORY_RESET           wipe the watch (DevicePolicyManager.wipeData)

Nothing else can be expressed: there is no command string, no argument and no
"restriction name" field. The instruction is signed for one device with ECDSA P-256 over
SHA-256 (every Android version verifies it; Ed25519, which the OTA manifests use, is not
available on every Android release the device-owner app may run on), carries a single-use
nonce, and expires at most 15 minutes after it was issued. The watch refuses anything else.

The bytes signed are exactly `Instruction.payload`; DevicePolicy.kt parses the same string,
and watch/app/src/test/resources/device_policy_instruction.json (written by
scripts/os_device_policy_fixture.py) proves the two agree.

Key handling follows care/ota.py: one environment variable, `IRA_DEVICE_POLICY_KEY`, holds
base64 of a 32-byte P-256 private scalar; no key configured means no instruction is issued
(the route answers 503), never an unsigned one. It is a separate key from the OTA manifest
key, so a leak of one cannot vouch for the other.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from dataclasses import dataclass
from typing import Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ACTIONS = ("MAINTENANCE_WINDOW", "RESTORE_RESTRICTIONS", "FACTORY_RESET")
MAX_WINDOW_MS = 15 * 60_000
PAYLOAD_VERSION = "ira-device-policy-v1"
ALGORITHM = "ECDSA-P256-SHA256"
KEY_ENV = "IRA_DEVICE_POLICY_KEY"   # base64 of a 32-byte P-256 private scalar


class KeyNotConfigured(RuntimeError):
    """No device-policy key: the server must not issue an instruction at all."""


@dataclass(frozen=True)
class Instruction:
    device_id: str
    action: str
    issued_at_ms: int
    expires_at_ms: int
    nonce: str

    @property
    def payload(self) -> str:
        return (f"{PAYLOAD_VERSION}|{self.device_id}|{self.action}|{self.issued_at_ms}|"
                f"{self.expires_at_ms}|{self.nonce}")


def new_instruction(device_id: str, action: str, *, now_ms: int, window_ms: int = 10 * 60_000) -> Instruction:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    if not 0 < window_ms <= MAX_WINDOW_MS:
        raise ValueError("window must be between 1 ms and 15 minutes")
    if not device_id or "|" in device_id:
        raise ValueError("device id is malformed")
    return Instruction(device_id, action, now_ms, now_ms + window_ms, secrets.token_hex(16))


def sign(instruction: Instruction, key: ec.EllipticCurvePrivateKey) -> str:
    """Base64 of the DER signature over the payload (what Java's SHA256withECDSA verifies)."""
    return base64.b64encode(key.sign(instruction.payload.encode("utf-8"), ec.ECDSA(hashes.SHA256()))).decode()


def public_key_b64(key: ec.EllipticCurvePrivateKey) -> str:
    """The X.509 SubjectPublicKeyInfo the watch is built with (KeyFactory "EC")."""
    return base64.b64encode(key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).decode()


def key_id(key: ec.EllipticCurvePrivateKey) -> str:
    spki = key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(spki).hexdigest()[:16]


def load_key(env: Optional[dict] = None) -> ec.EllipticCurvePrivateKey:
    raw = (env if env is not None else os.environ).get(KEY_ENV, "")
    if not raw:
        raise KeyNotConfigured(f"{KEY_ENV} is not set")
    try:
        scalar = base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001 - any decoding fault is the same refusal
        raise KeyNotConfigured(f"{KEY_ENV} is not base64") from exc
    if len(scalar) != 32:
        raise KeyNotConfigured(f"{KEY_ENV} must be a 32-byte P-256 private scalar")
    try:
        return ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1())
    except ValueError as exc:
        raise KeyNotConfigured(f"{KEY_ENV} is not a valid P-256 private scalar") from exc


def signed(instruction: Instruction, key: ec.EllipticCurvePrivateKey) -> dict:
    """What travels to the watch: the payload string itself (never re-serialised) and its signature."""
    return {"payload": instruction.payload, "signature": sign(instruction, key),
            "key_id": key_id(key), "algorithm": ALGORITHM}
