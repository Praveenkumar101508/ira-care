"""Server side of the watch's device policy (E2.m, care/devicepolicy.py).

Instructions are for one device, one action, at most 15 minutes, single-use nonce, signed with
ECDSA P-256; the committed fixture the watch's JVM test verifies is what this module produces."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from care import devicepolicy

ROOT = Path(__file__).resolve().parents[4]
FIXTURE = ROOT / "watch/app/src/test/resources/device_policy_instruction.json"


def test_an_instruction_is_bounded_and_signed():
    key = ec.generate_private_key(ec.SECP256R1())
    ins = devicepolicy.new_instruction("dev-1", "LIFT_DEBUGGING", now_ms=1_000)
    assert ins.expires_at_ms - ins.issued_at_ms == 10 * 60_000 and len(ins.nonce) == 32
    sig = base64.b64decode(devicepolicy.sign(ins, key))
    key.public_key().verify(sig, ins.payload.encode(), ec.ECDSA(hashes.SHA256()))
    assert ins.payload == f"ira-device-policy-v1|dev-1|LIFT_DEBUGGING|1000|{1000 + 600000}|{ins.nonce}"


@pytest.mark.parametrize("action,window", [("UNLOCK_EVERYTHING", 60_000), ("LIFT_DEBUGGING", 15 * 60_000 + 1),
                                           ("FACTORY_RESET", 0)])
def test_unknown_actions_and_unbounded_windows_are_refused(action, window):
    with pytest.raises(ValueError):
        devicepolicy.new_instruction("dev-1", action, now_ms=0, window_ms=window)


def test_the_watch_fixture_is_what_this_module_produces():
    from scripts.os_device_policy_fixture import TEST_SCALAR
    data = json.loads(FIXTURE.read_text())
    key = ec.derive_private_key(TEST_SCALAR, ec.SECP256R1())
    assert data["public_key"] == devicepolicy.public_key_b64(key)
    assert {c["action"] for c in data["cases"]} == set(devicepolicy.ACTIONS)
    for c in data["cases"]:
        ins = devicepolicy.Instruction(c["device_id"], c["action"], c["issued_at_ms"], c["expires_at_ms"], c["nonce"])
        assert c["payload"] == ins.payload
        key.public_key().verify(base64.b64decode(c["signature"]), ins.payload.encode(), ec.ECDSA(hashes.SHA256()))
