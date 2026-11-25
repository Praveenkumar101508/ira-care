#!/usr/bin/env python3
"""The production API image's lock: requirements-api.lock, derived from requirements.lock.

The API image (deploy/Dockerfile.api) used to install requirements-test.txt, so pytest, moto
and moto's own dependencies (Werkzeug, responses, xmltodict) shipped in production, and the
packages nobody pinned floated to whatever PyPI served on the day of the build. It now installs
requirements-api.lock with ``--no-deps --require-hashes``: every artefact named by a hash.

requirements.lock is the whole server's tree (torch-dependent model stack, browser automation
and the rest), which the care server does not load. requirements-api.txt names what it does
load; uv resolves that subset with every version constrained to requirements.lock, and this
script then copies requirements.lock's hashes onto each package. The result is a strict subset
of requirements.lock: same versions, same artefacts.

    python scripts/api_image_lock.py --write   # needs PyPI (uv resolves the subset)
    python scripts/api_image_lock.py --check   # offline: what tests/architecture runs

The check fails when a package in the API lock is absent from requirements.lock or differs in
version or hashes, when a requirement of requirements-api.txt is missing or at another
version, when a test-only package (named in requirements-test.txt, not in requirements.txt)
or PyAV is in it, and when a package has no parent in the tree (left behind by an edit).
Whether the installed tree is complete is checked where it is installed: the image build runs
``pip check`` and fails on anything but faster-whisper's PyAV (docs/security/LICENCES.md).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

IRA_ROOT = Path(__file__).resolve().parents[1]
LOCK = IRA_ROOT / "requirements.lock"
API_REQS = IRA_ROOT / "requirements-api.txt"
API_LOCK = IRA_ROOT / "requirements-api.lock"
REQS = IRA_ROOT / "requirements.txt"
TEST_REQS = IRA_ROOT / "requirements-test.txt"

HEADER = """\
# requirements-api.lock: what the production API image installs (deploy/Dockerfile.api).
#
# GENERATED. Do not hand-edit. Regenerate with:
#
#   python scripts/api_image_lock.py --write
#
# Input: requirements-api.txt. Every version is constrained to requirements.lock and every
# hash is copied from it, so this file is a strict subset of requirements.lock
# (scripts/api_image_lock.py --check, tests/architecture/test_api_image_lock.py).
#
# Install it with `--no-deps --require-hashes`: the lock is the whole tree. Then install
# requirements-nodeps.txt the same way (faster-whisper without PyAV).
"""

_BLOCK = re.compile(r"(?ms)^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+) \\\n(.*?)(?=^[A-Za-z0-9]|\Z)")
_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")
_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(==\s*([^\s;#]+))?")


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parents(body: str) -> set[str]:
    """The ``# via`` entries of a block: package names, or ``-r <file>`` for a top-level
    requirement. Constraint files (``-c``) are not parents."""
    out = set()
    for line in body.splitlines():
        s = line.strip()
        if not s.startswith("#"):
            continue
        s = s[1:].strip()
        s = s[3:].strip() if s.startswith("via") else s
        if s and not s.startswith("-c"):
            out.add(s)
    return out


def blocks(text: str) -> dict[str, dict]:
    """``{name: {"name", "version", "hashes", "parents", "text"}}`` for each package block."""
    out = {}
    for m in _BLOCK.finditer(text):
        body = m.group(3)
        out[norm(m.group(1))] = {
            "name": m.group(1), "version": m.group(2), "text": m.group(0),
            "hashes": set(_HASH.findall(body)),
            "parents": _parents(body),
        }
    return out


def requirements(path: Path) -> dict[str, str | None]:
    """``{name: exact version or None}`` for each requirement line of a manifest."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "-")):
            continue
        m = _REQ.match(s)
        if m:
            out[norm(m.group(1))] = m.group(4)
    return out


def test_only() -> set[str]:
    """Packages the tests install and the product does not: in requirements-test.txt, not in
    requirements.txt. Read from the files, so a new test tool is covered without an edit here."""
    return set(requirements(TEST_REQS)) - set(requirements(REQS))


def problems(api_lock_text: str, lock_text: str, api_reqs: dict[str, str | None],
             dev_only: set[str]) -> list[str]:
    found: list[str] = []
    api, full = blocks(api_lock_text), blocks(lock_text)
    if not api:
        return ["the API lock names no package"]
    for name, b in sorted(api.items()):
        f = full.get(name)
        if f is None:
            found.append(f"{name} is in the API lock but not in requirements.lock")
            continue
        if b["version"] != f["version"]:
            found.append(f"{name}: the API lock pins {b['version']}, requirements.lock "
                         f"{f['version']}")
        if not b["hashes"]:
            found.append(f"{name} carries no hash")
        elif b["hashes"] != f["hashes"]:
            found.append(f"{name}: its hashes differ from requirements.lock's")
        if name in dev_only:
            found.append(f"{name} is a test-only package (requirements-test.txt) and must not "
                         "ship in the API image")
        if name == "av":
            found.append("av (PyAV) is in the API lock; it does not ship "
                         "(docs/security/LICENCES.md)")
        parents = {norm(p) for p in b["parents"] if not p.startswith("-r")}
        top = any(p.startswith("-r") for p in b["parents"])
        if not top and not parents & set(api):
            found.append(f"{name} has no parent in the API lock: nothing installed needs it")
    for name, version in sorted(api_reqs.items()):
        if name not in api:
            found.append(f"{name} (requirements-api.txt) is not in the API lock")
        elif version and api[name]["version"] != version:
            found.append(f"{name}: requirements-api.txt pins {version}, the API lock "
                         f"{api[name]['version']}")
    return found


def check() -> list[str]:
    if not API_LOCK.is_file():
        return [f"{API_LOCK.name} is missing (python scripts/api_image_lock.py --write)"]
    return problems(API_LOCK.read_text(encoding="utf-8"), LOCK.read_text(encoding="utf-8"),
                    requirements(API_REQS), test_only())


def write() -> list[str]:
    full = blocks(LOCK.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        constraints = Path(tmp) / "constraints.txt"
        constraints.write_text("".join(f"{b['name']}=={b['version']}\n" for b in full.values()),
                               encoding="utf-8")
        out = Path(tmp) / "api.lock"
        subprocess.run(["uv", "pip", "compile", API_REQS.name, "-c", str(constraints),
                        "--generate-hashes", "-p", "3.12", "--no-header", "-q", "-o", str(out)],
                       cwd=IRA_ROOT, check=True)
        resolved = blocks(out.read_text(encoding="utf-8"))
    parts = [HEADER]
    for name, b in sorted(resolved.items()):
        f = full.get(name)
        if f is None or f["version"] != b["version"]:
            return [f"{name}=={b['version']} resolved outside requirements.lock; regenerate "
                    "requirements.lock first (see its header)"]
        hashes = sorted(f["hashes"])
        parents = sorted(b["parents"])
        via = (f"\n    # via {parents[0]}" if len(parents) == 1 else
               "\n    # via" + "".join(f"\n    #   {p}" for p in parents))
        lines = [f"{b['name']}=={b['version']} \\"]
        lines += [f"    --hash=sha256:{h} \\" for h in hashes[:-1]]
        lines.append(f"    --hash=sha256:{hashes[-1]}")
        parts.append("\n".join(lines) + via + "\n")
    API_LOCK.write_text("".join(parts), encoding="utf-8")
    print(f"wrote {API_LOCK.name}: {len(resolved)} packages")
    return check()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    found = write() if a.write else check()
    for p in found:
        print(f"problem: {p}")
    if not found:
        n = len(blocks(API_LOCK.read_text(encoding="utf-8")))
        print(f"{API_LOCK.name}: {n} packages, a strict subset of {LOCK.name}, no test-only "
              "package")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
