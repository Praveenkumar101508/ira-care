"""Server side of the watch's device policy (E2.m, E2.o, care/devicepolicy.py).

Instructions are for one device, one action from a closed list, at most 15 minutes, single-use
nonce, signed with ECDSA P-256; the committed fixture the watch's JVM test verifies is what this
module produces; the key comes from IRA_DEVICE_POLICY_KEY and nothing is signed without it."""
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
    ins = devicepolicy.new_instruction("dev-1", "MAINTENANCE_WINDOW", now_ms=1_000)
    assert ins.expires_at_ms - ins.issued_at_ms == 10 * 60_000 and len(ins.nonce) == 32
    sig = base64.b64decode(devicepolicy.sign(ins, key))
    key.public_key().verify(sig, ins.payload.encode(), ec.ECDSA(hashes.SHA256()))
    assert ins.payload == f"ira-device-policy-v1|dev-1|MAINTENANCE_WINDOW|1000|{1000 + 600000}|{ins.nonce}"


@pytest.mark.parametrize("action,window", [("UNLOCK_EVERYTHING", 60_000), ("RUN_SHELL", 60_000),
                                           ("maintenance_window", 60_000), ("LIFT_DEBUGGING", 60_000),
                                           ("MAINTENANCE_WINDOW", 15 * 60_000 + 1), ("FACTORY_RESET", 0)])
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


def test_a_device_id_cannot_smuggle_a_field_into_the_payload():
    with pytest.raises(ValueError):
        devicepolicy.new_instruction("dev-1|FACTORY_RESET", "MAINTENANCE_WINDOW", now_ms=0)


def test_the_key_comes_from_the_environment_and_nothing_is_signed_without_it():
    with pytest.raises(devicepolicy.KeyNotConfigured):
        devicepolicy.load_key({})
    with pytest.raises(devicepolicy.KeyNotConfigured):
        devicepolicy.load_key({devicepolicy.KEY_ENV: "not base64!"})
    with pytest.raises(devicepolicy.KeyNotConfigured):
        devicepolicy.load_key({devicepolicy.KEY_ENV: base64.b64encode(b"short").decode()})
    with pytest.raises(devicepolicy.KeyNotConfigured):
        devicepolicy.load_key({devicepolicy.KEY_ENV: base64.b64encode(bytes(32)).decode()})  # scalar 0
    raw = bytes(range(1, 33))
    key = devicepolicy.load_key({devicepolicy.KEY_ENV: base64.b64encode(raw).decode()})
    ins = devicepolicy.new_instruction("dev-1", "RESTORE_RESTRICTIONS", now_ms=5)
    out = devicepolicy.signed(ins, key)
    assert out["payload"] == ins.payload and out["algorithm"] == "ECDSA-P256-SHA256"
    assert out["key_id"] == devicepolicy.key_id(key) and len(out["key_id"]) == 16
    key.public_key().verify(base64.b64decode(out["signature"]), out["payload"].encode(), ec.ECDSA(hashes.SHA256()))


def test_the_operator_keygen_makes_a_key_the_server_loads():
    from scripts.deployment_admin import keygen
    private, public = keygen()
    key = devicepolicy.load_key({devicepolicy.KEY_ENV: private})
    assert devicepolicy.public_key_b64(key) == public
