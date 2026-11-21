#!/usr/bin/env python3
"""Applied migration files are immutable. This proves it.

    python -m scripts.check_migration_integrity            # verify (CI)
    python -m scripts.check_migration_integrity --update    # record new files

Why
---

Phase 3B-3.5 edited two files that had already been applied — `023_tenancy.sql`
and `031_pretenancy_isolation.sql` — to remove `\\set` metacommands that asyncpg
rejects, without which a fresh database could not be installed by the sanctioned
migration path at all.

That was defensible **only** because no production or customer database exists.
It is not a precedent. A file that some databases have applied and others have
not is two different schemas wearing one filename, and nothing in
`schema_migrations` records which one a given database got — it stores the name.

So from now on:

* a **changed** hash fails, unless an approved exception in the record names the
  exact before/after pair;
* a **new** file that is not in the record fails too. Otherwise the record goes
  stale silently, and a stale record is worse than none because it is trusted.

Corrections to an applied migration use a **new numbered migration**. The
exception mechanism exists for the case where that is genuinely impossible —
a fresh install cannot parse the file, so no later migration can repair it —
and it requires the reason to be written down, not merely the hash updated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

IRA_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = IRA_ROOT.parents[1]
POSTGRES_DIR = REPO_ROOT / "supracloud-jarvis" / "postgres"
RECORD = REPO_ROOT / "docs" / "cloud" / "evidence" / "MIGRATION_FILE_HASHES.json"

_NUMBER = re.compile(r"^(\d+)_")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def migration_number(name: str) -> int | None:
    m = _NUMBER.match(name)
    return int(m.group(1)) if m else None


def current_files() -> dict[str, Path]:
    """Top level only — `provisioning/` and `rollback/` are not the chain.

    `provisioning/` is an operator step and `rollback/` is deliberately outside
    the directory `docker-entrypoint-initdb.d` and the migration runner read.
    Hashing them here would make an ordinary rollback edit fail CI.
    """
    return {p.name: p for p in sorted(POSTGRES_DIR.glob("*.sql"))}


def load_record() -> dict:
    if not RECORD.is_file():
        return {"migrations": {}, "exceptions": []}
    return json.loads(RECORD.read_text(encoding="utf-8"))


def verify() -> int:
    record = load_record()
    recorded = record.get("migrations", {})
    exceptions = {e["filename"]: e for e in record.get("exceptions", [])}
    files = current_files()

    changed: list[str] = []
    unrecorded: list[str] = []
    missing: list[str] = []
    inconsistent: list[str] = []

    # The record has two places that hold a hash for the same bytes, and they
    # must agree. The loop below consults `migrations` FIRST and falls through to
    # the exception on a mismatch — so a corrupted `migrations` entry for a file
    # that happens to have an exception is caught by the exception instead, and
    # this reports OK while the entry that is meant to be the authority is
    # silently wrong. Found by planting exactly that.
    for name, exc in exceptions.items():
        entry = recorded.get(name)
        if entry is None:
            inconsistent.append(
                f"{name}: has an approved exception but no entry in `migrations`")
        elif entry["sha256"] != exc.get("current_sha256"):
            inconsistent.append(
                f"{name}: `migrations` records {entry['sha256'][:12]}… and its "
                f"exception names {str(exc.get('current_sha256'))[:12]}… as current")

    for name, path in files.items():
        digest = sha256(path)
        entry = recorded.get(name)
        if entry is None:
            unrecorded.append(name)
            continue
        if entry["sha256"] == digest:
            continue
        # A mismatch is permitted only by an exception that names both hashes,
        # so "approved" cannot mean "somebody re-ran --update".
        exc = exceptions.get(name)
        if exc and exc.get("current_sha256") == digest and exc.get("approved"):
            continue
        changed.append(f"{name}: recorded {entry['sha256'][:12]}… now {digest[:12]}…")

    for name in recorded:
        if name not in files:
            missing.append(name)

    rc = 0
    if changed:
        rc = 1
        print("FAIL: applied migration file(s) changed:", file=sys.stderr)
        for line in changed:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nAn applied migration is immutable. Write a NEW numbered migration.\n"
            "If the change is genuinely unrepairable by a later migration — a fresh\n"
            "install cannot parse the file — add an approved exception to\n"
            "docs/cloud/evidence/MIGRATION_FILE_HASHES.json recording the previous\n"
            "hash, the new hash, the defect and the reason, and explain it in\n"
            "MIGRATION_HISTORY_INTEGRITY_REPORT.md.",
            file=sys.stderr,
        )
    if unrecorded:
        rc = 1
        print(f"FAIL: migration file(s) not in the integrity record: {unrecorded}",
              file=sys.stderr)
        print("Run `python -m scripts.check_migration_integrity --update` and commit "
              "the record with the migration.", file=sys.stderr)
    if missing:
        rc = 1
        print(f"FAIL: recorded migration file(s) no longer present: {missing}",
              file=sys.stderr)
        print("Deleting an applied migration leaves databases that ran it with a "
              "schema nothing in this repository describes.", file=sys.stderr)
    if inconsistent:
        rc = 1
        print("FAIL: the record disagrees with itself:", file=sys.stderr)
        for line in inconsistent:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nThe two entries describe the same bytes. While they disagree, a "
            "corrupted `migrations` entry is masked by the exception and this "
            "check reports OK — so the disagreement has to fail before the "
            "hashes are compared at all.",
            file=sys.stderr,
        )

    if rc == 0:
        print(f"migration integrity OK — {len(files)} files, all hashes match")
    return rc


def update() -> int:
    record = load_record()
    recorded = record.setdefault("migrations", {})
    added, unchanged = [], 0

    for name, path in current_files().items():
        digest = sha256(path)
        entry = recorded.get(name)
        if entry is None:
            recorded[name] = {
                "migration_number": migration_number(name),
                "sha256": digest,
                "status": "applied",
            }
            added.append(name)
        elif entry["sha256"] == digest:
            unchanged += 1
        else:
            # --update deliberately does NOT rewrite an existing hash. If it did,
            # the control would be one command away from being defeated by the
            # person it exists to stop.
            print(f"REFUSING to rewrite the recorded hash for {name}.", file=sys.stderr)
            print("Add an approved exception instead — see the module docstring.",
                  file=sys.stderr)
            return 1

    record["migrations"] = dict(sorted(recorded.items()))
    RECORD.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"recorded {len(added)} new file(s); {unchanged} unchanged")
    for name in added:
        print(f"  + {name}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--update", action="store_true",
                   help="record NEW migration files; never rewrites an existing hash")
    args = p.parse_args()
    return update() if args.update else verify()


if __name__ == "__main__":
    sys.exit(main())
