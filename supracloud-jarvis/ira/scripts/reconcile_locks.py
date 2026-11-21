#!/usr/bin/env python3
"""P16-R3 — FINDING AN: the two locks pinned the same packages at different
versions.

── The defect ──────────────────────────────────────────────────────────────

FINDING AM split the dependency set in two: `requirements.lock` (resolved from
PyPI) and `requirements-torch.lock` (resolved from the PyTorch CPU index, so
that cross-index dependency confusion is structurally impossible rather than
merely disabled).

Two independent resolutions of overlapping trees do not agree. After the strip,
three packages were pinned in BOTH files at DIFFERENT versions:

    filelock           main 3.32.2      torch 3.29.0
    fsspec             main 2026.7.0    torch 2026.4.0
    typing-extensions  main 4.16.0      torch 4.15.0

The PyTorch index carries older copies and does not offer the newer ones, so
the disagreement cannot be resolved by re-pinning the torch side. Left alone it
is not cosmetic:

* `pip install --require-hashes -r requirements.lock -r requirements-torch.lock`
  **fails** — the same name is pinned twice at conflicting versions.
* Installed one file at a time, whichever runs second silently replaces the
  other's package, so "what is installed" depends on command ordering while
  both locks claim to describe it.
* The advisory and licence gates read the locks. Two answers for one package
  means at least one gate is auditing a version that is not deployed.

── The resolution ──────────────────────────────────────────────────────────

**The two locks are made disjoint, and the main lock owns every shared name.**
`filelock`, `fsspec` and `typing-extensions` have parents outside the torch
family (huggingface-hub, pydantic, and others), so the main lock has to carry
them regardless; the torch lock does not have to.

This is safe only if the surviving version satisfies what the pinned torch
wheel actually requires — so that is checked, not assumed, against the wheel's
own `Requires-Dist`, obtained from the PEP 658 metadata sidecar published
beside the exact artifact the lock pins. **Not** from PyPI's `torch 2.11.0`
metadata: that describes the GPU build, which declares a CUDA stack the `+cpu`
wheel does not have. Trusting it is precisely the mistake that produced FINDING
AM's 16 phantom NVIDIA pins.

`scripts/check_torch_supply_chain.py` then enforces both halves permanently:
the locks must stay disjoint, and every requirement the pinned wheel declares
must be pinned in exactly one of them at a satisfying version.

Run after regenerating either lock:

    python scripts/reconcile_locks.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

IRA_ROOT = Path(__file__).resolve().parents[1]
MAIN_LOCK = IRA_ROOT / "requirements.lock"
TORCH_LOCK = IRA_ROOT / "requirements-torch.lock"

_PKG = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==")
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;#\\]+)")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_blocks(text: str) -> list[tuple[str | None, str]]:
    out: list[tuple[str | None, str]] = []
    for block in re.split(r"\n(?=[A-Za-z0-9][A-Za-z0-9._-]*==)", text):
        m = _PKG.match(block)
        out.append((_norm(m.group(1)) if m else None, block))
    return out


def pins(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or line.startswith(" "):
            continue
        m = _PIN.match(line)
        if m:
            out[_norm(m.group(1))] = m.group(2)
    return out


def main() -> int:
    main_pins = pins(MAIN_LOCK)
    torch_pins = pins(TORCH_LOCK)
    shared = sorted(set(main_pins) & set(torch_pins))

    if not shared:
        print(f"the two locks are already disjoint — "
              f"{len(main_pins)} + {len(torch_pins)} packages, no overlap")
        return 0

    print(f"{len(shared)} package(s) are pinned in BOTH locks. The main lock "
          f"owns them; removing the torch lock's copy:")
    for name in shared:
        agree = main_pins[name] == torch_pins[name]
        print(f"    {name:<20} main={main_pins[name]:<12} "
              f"torch={torch_pins[name]:<12}"
              f"{'' if agree else '   <-- DISAGREE'}")

    blocks = parse_blocks(TORCH_LOCK.read_text(encoding="utf-8"))
    kept = [block for name, block in blocks if name not in set(shared)]
    # `rstrip` + explicit newline, because dropping the LAST block leaves the
    # preceding one without its trailing newline — and a lock whose final line
    # is unterminated silently swallows whatever gets appended to it next.
    TORCH_LOCK.write_text("\n".join(kept).rstrip("\n") + "\n", encoding="utf-8")

    # POST-CONDITION. "It rewrote the file" is not evidence it removed the
    # right blocks, and a regex that matches nothing writes the file back
    # unchanged without complaining.
    after = pins(TORCH_LOCK)
    still = sorted(set(main_pins) & set(after))
    if still:
        print(f"::error::{still} are still in both locks after the rewrite. "
              f"The block split did not match — check the parse before "
              f"trusting this.")
        return 1
    if len(after) != len(torch_pins) - len(shared):
        print(f"::error::expected {len(torch_pins) - len(shared)} packages to "
              f"remain in {TORCH_LOCK.name}, found {len(after)}. The rewrite "
              f"removed the wrong thing.")
        return 1

    print(f"  {len(after)} packages remain in {TORCH_LOCK.name}, "
          f"{len(main_pins)} in {MAIN_LOCK.name}, disjoint")
    print("  the surviving versions are checked against the pinned wheel's own "
          "Requires-Dist by scripts/check_torch_supply_chain.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
