#!/usr/bin/env python3
"""Rebuild the table of published credential digests from this repository's history (D-004).

    python3 scripts/security/published_credential_digests.py OUT.json
    python3 scripts/security/published_credential_digests.py OUT.json --include-inherited
    python3 scripts/security/published_credential_digests.py OUT.json --repo /path/to/clone

A credential whose value was ever committed is published: every clone has it, so an install
still using it is open to everyone who has, or had, a copy. `core.runtime.published_credentials`
holds a salted scrypt digest of each such value (never the value) and IRA refuses to start while
one is in use. This script is what rebuilds that table from the evidence, so the table can be
checked against the history rather than trusted.

What it scans: every committed version of every environment file (`.env`, `.env.*`, `*.env`,
`*.env.example`) on every ref the repository has (`git log --all`), deleted versions included,
for the six variable names the table covers. A value counts as published when it is not empty
and not a placeholder in the sense of `core.runtime.environment.is_placeholder` (the same rule
the start-up validation applies: `CHANGE_ME`, `change-me`, `<your-...>`, the CI test values
`ci-test-secret-key` and `ci-test-password`, and so on). Each distinct published value is
digested with `published_credentials.digest(name, value)`.

What it prints: provenance only. Variable names, file paths, short commit ids and counts. No
value, no part of a value and no digest ever reaches stdout or stderr; the digests go to the
JSON file named on the command line, and that file holds digests only.

`--include-inherited` merges the module's current table into the output. IRA CARE was split
from the IRA project on 21 November 2026 with one placeholder `.env.example`; the six digests
the module ships were computed from the source project's history (`docs/cloud` of that project,
July 2026) and are provenance this repository inherits rather than something its own history
shows. Without the flag the output is what THIS history publishes; with it, the full table.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IRA = ROOT / "supracloud-jarvis" / "ira"
if str(IRA) not in sys.path:
    sys.path.insert(0, str(IRA))

from core.runtime import published_credentials as pc  # noqa: E402
from core.runtime.environment import is_placeholder  # noqa: E402

#: Files that may carry credentials: a tracked `.env`, its variants and example templates.
ENV_FILE = re.compile(r"(^|/)(\.env(\..*)?|[^/]*\.env(\.example)?)$")
_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*)$")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, errors="replace").stdout


def env_files_in_history(repo: Path) -> list[tuple[str, str]]:
    """Every (commit, path) at which an environment file was added or changed, on any ref."""
    out = _git(repo, "log", "--all", "--format=%x00%h", "--name-only", "--diff-filter=AMCR")
    found: list[tuple[str, str]] = []
    commit = ""
    for line in out.splitlines():
        if line.startswith("\x00"):
            commit = line[1:]
            continue
        if line and ENV_FILE.search(line):
            found.append((commit, line))
    return found


def value_of(raw: str) -> str:
    """The value of one `NAME=value` line, without quotes or a trailing comment."""
    v = raw.strip()
    if v[:1] in ("'", '"'):
        end = v.find(v[0], 1)
        if end > 0:
            return v[1:end]
    return v.split(" #", 1)[0].split("\t#", 1)[0].strip()


def parse_env(text: str, names: frozenset[str]) -> dict[str, str]:
    """The values of the named variables in one environment file version."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _ASSIGN.match(line)
        if m and m.group(1) in names:
            out[m.group(1)] = value_of(m.group(2))
    return out


def scan(repo: Path, names: frozenset[str]) -> tuple[dict[str, set[str]], list[dict]]:
    """(digests by name, provenance records). Provenance never carries a value or a digest."""
    digests: dict[str, set[str]] = {n: set() for n in names}
    seen: dict[str, set[str]] = {n: set() for n in names}   # values, kept in memory only
    provenance: list[dict] = []
    versions = env_files_in_history(repo)
    for commit, path in versions:
        try:
            text = _git(repo, "show", f"{commit}:{path}")
        except subprocess.CalledProcessError:
            continue                                     # a rename's old path
        values = parse_env(text, names)
        rec = {"commit": commit, "path": path, "names_present": sorted(values),
               "published": [], "placeholders": []}
        for name, value in values.items():
            if is_placeholder(value):
                rec["placeholders"].append(name)
                continue
            rec["published"].append(name)
            if value not in seen[name]:
                seen[name].add(value)
                digests[name].add(pc.digest(name, value))
        provenance.append(rec)
    return digests, provenance


def merged_with_inherited(digests: dict[str, set[str]]) -> dict[str, set[str]]:
    return {name: set(digests.get(name, set())) | set(pc.PUBLISHED_DIGESTS.get(name, ()))
            for name in set(digests) | set(pc.PUBLISHED_DIGESTS)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("out", type=Path, help="where to write {name: [digest, ...]} (digests only)")
    ap.add_argument("--repo", type=Path, default=ROOT, help="repository to scan (default: this one)")
    ap.add_argument("--names", default=",".join((*pc.REFUSED, *pc.WARNED)),
                    help="comma-separated variable names (default: the six the table covers)")
    ap.add_argument("--include-inherited", action="store_true",
                    help="merge the module's current table (the source project's provenance)")
    a = ap.parse_args(argv)
    names = frozenset(n for n in a.names.split(",") if n)
    digests, provenance = scan(a.repo.resolve(), names)
    table = merged_with_inherited(digests) if a.include_inherited else digests
    a.out.write_text(json.dumps({n: sorted(d) for n, d in sorted(table.items())}, indent=2) + "\n")
    versions = len(provenance)
    with_published = [p for p in provenance if p["published"]]
    print(f"scanned {versions} environment-file version(s) across all refs of {a.repo.resolve()}")
    for p in provenance:
        tag = "PUBLISHED " + ",".join(p["published"]) if p["published"] else "placeholders only"
        print(f"  {p['commit']} {p['path']}: {tag}")
    print(f"versions with a published value: {len(with_published)}")
    for name in sorted(names):
        print(f"  {name}: {len(digests.get(name, ()))} distinct published value(s) in this history"
              + (f", {len(table.get(name, ()))} in the written table" if a.include_inherited else ""))
    print(f"wrote digests for {sum(1 for d in table.values() if d)} name(s) to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
