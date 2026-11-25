"""Write the cross-language fixture for the device policy (E2.m).

The server signs administrator instructions in Python (care/devicepolicy.py); the watch verifies
them in Kotlin (DevicePolicy.kt). This writes instructions signed by a FIXED test key (a fixture,
never a deployment key) to watch/app/src/test/resources/device_policy_instruction.json, so the
JVM test proves the watch accepts exactly what the server produces. Deterministic apart from the
ECDSA signatures themselves, which are randomised; the test only needs them valid.

    cd supracloud-jarvis/ira && python -m scripts.os_device_policy_fixture
"""
from __future__ import annotations

import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec

from care import devicepolicy

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "watch/app/src/test/resources/device_policy_instruction.json"
# A fixed scalar: a TEST key, published here on purpose so the fixture is reproducible.
TEST_SCALAR = int("1f" * 32, 16)


def main() -> None:
    key = ec.derive_private_key(TEST_SCALAR, ec.SECP256R1())
    issued = 1_764_072_000_000  # 2025-11-25T12:00:00Z, fixed
    cases = []
    for action in devicepolicy.ACTIONS:
        ins = devicepolicy.Instruction("device-fixture-1", action, issued, issued + 10 * 60_000,
                                       f"fixturenonce{action.lower()}00")
        cases.append({"device_id": ins.device_id, "action": ins.action, "issued_at_ms": ins.issued_at_ms,
                      "expires_at_ms": ins.expires_at_ms, "nonce": ins.nonce, "payload": ins.payload,
                      "signature": devicepolicy.sign(ins, key)})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"public_key": devicepolicy.public_key_b64(key), "cases": cases}, indent=1) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
