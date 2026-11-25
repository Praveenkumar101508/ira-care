"""Signed administrator instructions for the watch's device policy (requirement E2.m).

On IRA CARE OS the watch keeps developer options, factory reset and safe-mode boot locked
(watch/app/src/main/java/care/ira/watch/devicepolicy/DevicePolicy.kt). An IRA administrator
lifts developer options for a short window, or factory-resets a watch, with an instruction
this module signs for one device: ECDSA P-256 over SHA-256 (every Android version verifies
it), at most 15 minutes long, with a single-use nonce. The watch refuses anything else.

The bytes signed are exactly `payload(...)`; DevicePolicy.kt builds the same string, and
watch/app/src/test/resources/device_policy_instruction.json (written by
scripts/os_device_policy_fixture.py) proves the two agree.

Not here yet: an administrator route to request an instruction. That needs the deployment's
administrator role, which a care workspace role must never imply; it is recorded as follow-up.
"""
from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ACTIONS = ("LIFT_DEBUGGING", "FACTORY_RESET")
MAX_WINDOW_MS = 15 * 60_000


@dataclass(frozen=True)
class Instruction:
    device_id: str
    action: str
    issued_at_ms: int
    expires_at_ms: int
    nonce: str

    @property
    def payload(self) -> str:
        return (f"ira-device-policy-v1|{self.device_id}|{self.action}|{self.issued_at_ms}|"
                f"{self.expires_at_ms}|{self.nonce}")


def new_instruction(device_id: str, action: str, *, now_ms: int, window_ms: int = 10 * 60_000) -> Instruction:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    if not 0 < window_ms <= MAX_WINDOW_MS:
        raise ValueError("window must be between 1 ms and 15 minutes")
    return Instruction(device_id, action, now_ms, now_ms + window_ms, secrets.token_hex(16))


def sign(instruction: Instruction, key: ec.EllipticCurvePrivateKey) -> str:
    """Base64 of the DER signature over the payload (what Java's SHA256withECDSA verifies)."""
    return base64.b64encode(key.sign(instruction.payload.encode("utf-8"), ec.ECDSA(hashes.SHA256()))).decode()


def public_key_b64(key: ec.EllipticCurvePrivateKey) -> str:
    """The X.509 SubjectPublicKeyInfo the watch is built with (KeyFactory "EC")."""
    return base64.b64encode(key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).decode()
