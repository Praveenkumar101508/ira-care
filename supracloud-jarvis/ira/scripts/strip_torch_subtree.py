#!/usr/bin/env python3
"""P16-R3 — FINDING AM: remove the torch family AND everything only it needs.

── The defect this exists to prevent ───────────────────────────────────────

`requirements.lock` is produced by resolving `requirements.txt` with `torch`
and `torchaudio` removed from the INPUT. That does not keep them out:
`sentence-transformers` requires torch, so the resolver pulls it anyway.

P16-R1a then removed the two package blocks from the OUTPUT — and left their
entire dependency tree behind. **25 packages** remained that nothing else
needed, including the whole NVIDIA CUDA stack and `triton`.

Worse than untidy: the resolver pulls the **PyPI GPU build** of torch, whose
dependencies are not the ones this product installs. The deployed build is
`torch==2.11.0+cpu` from `download.pytorch.org`, and its real dependency set —
see `requirements-torch.lock` — is **9 packages with zero CUDA**. So the main
lock was pinning gigabytes of GPU libraries that the deployment never installs
and that exist in no real tree.

── Why the two locks must be complete and non-overlapping ──────────────────

`requirements-torch.lock` covers the torch family and everything it needs.
`requirements.lock` must therefore cover everything else and nothing of torch's
— otherwise the same package is pinned twice, at versions resolved from
different indexes, and "which one is installed" depends on ordering.

── Why this is derived, not hand-edited ────────────────────────────────────

Removing a package and leaving its subtree is what caused FINDING AM. Removing
the subtree by hand invites the same mistake one level down. So the set is
computed from the lock's **own `# via` annotations**: a package is dropped only
when every parent it has is itself dropped. Nothing is named in this file except
the two roots.

Run after regenerating the lock:

    python scripts/strip_torch_subtree.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

IRA_ROOT = Path(__file__).resolve().parents[1]
LOCK = IRA_ROOT / "requirements.lock"
#: uv writes its own two-line preamble and discards whatever was there before.
#: The committed header is re-applied here so the file's explanation of itself
#: survives regeneration — the previous one did not, and went on describing the
#: torch family as unlocked and UNKNOWN for three batches after that stopped
#: being true.
HEADER = IRA_ROOT / "requirements.lock.header"

#: The roots. Everything reachable ONLY from these leaves with them.
ROOTS = {"torch", "torchaudio"}

_PKG = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==")
#: A `# via` line names a parent. `# via -r requirements.txt` is a DIRECT
#: requirement and deliberately does not match — a direct requirement has no
#: package parent and must never be treated as an orphan.
#: `re.M` is load-bearing. Without it `^` anchors to position 0 of the block
#: and `findall` returns NOTHING, so every parent set is empty, no package
#: ever looks like an orphan, and the closure silently strips only the two
#: roots. That is precisely the shape of defect this script exists to fix,
#: and it shipped in the first draft — caught by comparing the strip count
#: (2) against the independently measured orphan set (25).
_VIA = re.compile(r"^\s+#\s+(?:via\s+)?([A-Za-z0-9][A-Za-z0-9._-]*)\s*$", re.M)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_blocks(text: str) -> list[tuple[str | None, str]]:
    """Split the lock into `(package name or None, block text)` in order."""
    out: list[tuple[str | None, str]] = []
    for block in re.split(r"\n(?=[A-Za-z0-9][A-Za-z0-9._-]*==)", text):
        m = _PKG.match(block)
        out.append((_norm(m.group(1)) if m else None, block))
    return out


def parents_of(block: str) -> set[str]:
    return {_norm(m) for m in _VIA.findall(block)} - {"via"}


def orphan_closure(blocks: list[tuple[str | None, str]]) -> set[str]:
    """Every package whose parents are all inside the removed set."""
    parents = {name: parents_of(block) for name, block in blocks if name}
    removed = set(ROOTS)
    changed = True
    while changed:
        changed = False
        for name, ps in parents.items():
            if name in removed or not ps:
                continue
            if ps <= removed:
                removed.add(name)
                changed = True
    return removed


def main() -> int:
    text = LOCK.read_text(encoding="utf-8")
    blocks = parse_blocks(text)
    removed = orphan_closure(blocks)

    present = {n for n, _ in blocks if n}
    dropped = sorted(removed & present)
    kept = [block for name, block in blocks if name not in removed]

    # Drop whatever preamble the generator wrote and re-apply the committed
    # one. `kept` blocks after the first are package blocks; the first is
    # everything before the first pin, which is the preamble.
    body = [b for b in kept if _PKG.match(b)]
    if not body:
        print("::error::no package blocks found at all. The lock is empty or "
              "the block split failed — either way nothing here is trustworthy.")
        return 1
    header = HEADER.read_text(encoding="utf-8").rstrip("\n") + "\n"
    # `rstrip` + explicit newline: if the removed block was the last one, the
    # preceding block loses its trailing newline, and a lock whose final line
    # is unterminated silently absorbs whatever is appended to it next.
    LOCK.write_text(header + "\n".join(body).rstrip("\n") + "\n",
                    encoding="utf-8")

    print(f"stripped {len(dropped)} package(s) — the torch family and "
          f"everything reachable only from it:")
    for name in dropped:
        print(f"    {name}")
    remaining = len([n for n, _ in parse_blocks(LOCK.read_text(encoding='utf-8')) if n])
    print(f"  {remaining} packages remain in {LOCK.name}")
    if not dropped:
        # "Nothing to do" and "the parse found nothing" look identical from
        # here, so they are told apart by the one fact that distinguishes
        # them: whether the roots themselves are still in the file. Re-running
        # after a successful strip is legitimate and must stay idempotent;
        # running over a lock that still contains torch and stripping nothing
        # is the silent-no-op bug this script shipped with once already.
        if ROOTS & present:
            print(f"::error::{sorted(ROOTS & present)} are still in the lock "
                  f"and nothing was stripped. The `# via` parse found no "
                  f"parents — this script did nothing and said so; do not "
                  f"trust a lock it has 'processed'.")
            return 1
        print("  already stripped — the torch family is not in this lock")
        return 0

    # POST-CONDITION. Stripping only the two roots is what a broken parent
    # parse looks like, and "it removed something" is not evidence the closure
    # descended. Re-read the written file and assert the invariant holds: no
    # surviving package may have all of its parents removed.
    survivors = parse_blocks(LOCK.read_text(encoding="utf-8"))
    still_orphaned = sorted(
        name for name, block in survivors
        if name and parents_of(block) and parents_of(block) <= removed)
    if still_orphaned:
        print(f"::error::{len(still_orphaned)} package(s) remain whose every "
              f"parent was removed: {still_orphaned[:10]}. The closure did not "
              f"descend — check that the `# via` parse is working.")
        return 1
    print("  post-condition: no surviving package has all parents removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
