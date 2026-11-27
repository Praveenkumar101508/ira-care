#!/usr/bin/env python3
"""Record a figshare dataset version's licence and file checksums, without keeping the data.

    python3 scripts/legal/dataset_provenance.py --article 4214283 --version 8 \\
        --expect-licence "CC BY 4.0" --out provenance.json

Reads the version's record from figshare's API (title, DOI, licence, files with figshare's own MD5),
downloads each file to a temporary folder outside the repository, checks it against figshare's MD5,
computes SHA-256, deletes it, and writes only the record. Exit status 1 if the licence differs from
`--expect-licence` or any checksum differs. Used for UMAFall version 8 (register F1.g), which
figshare's website refuses to automated clients on the build machine; the workflow
`.github/workflows/dataset-provenance.yml` runs it on a GitHub runner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

API = "https://api.figshare.com/v2"
UA = "ira-care-dataset-provenance/1.0"


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - figshare's public API
        return json.loads(r.read())


def summarise(record: dict) -> dict:
    """The parts of a figshare version record that provenance needs (pure, tested)."""
    lic = record.get("license") or {}
    return {"title": record.get("title"), "doi": record.get("doi"), "version": record.get("version"),
            "published": record.get("published_date"), "licence": lic.get("name"), "licence_url": lic.get("url"),
            "files": [{"name": f.get("name"), "size": f.get("size"), "md5": f.get("computed_md5") or f.get("supplied_md5"),
                       "download_url": f.get("download_url")} for f in record.get("files") or []]}


def verdict(summary: dict, expect_licence: str, checked: list[dict]) -> dict:
    """Pass only if the licence is the expected one and every file matched figshare's MD5 (pure, tested)."""
    licence_ok = (summary.get("licence") or "").strip().lower() == expect_licence.strip().lower()
    files_ok = bool(checked) and all(c.get("md5_matches") for c in checked)
    return {"licence_ok": licence_ok, "files_ok": files_ok, "passed": licence_ok and files_ok}


def download_and_hash(url: str, folder: Path) -> tuple[str, str, int]:
    path = folder / "f"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    md5, sha, n = hashlib.md5(), hashlib.sha256(), 0  # noqa: S324 - matching figshare's own checksum
    with urllib.request.urlopen(req, timeout=600) as r, open(path, "wb") as out:  # noqa: S310
        while chunk := r.read(1 << 20):
            md5.update(chunk); sha.update(chunk); n += len(chunk); out.write(chunk)
    path.unlink()
    return md5.hexdigest(), sha.hexdigest(), n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--article", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--expect-licence", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    summary = summarise(get_json(f"{API}/articles/{a.article}/versions/{a.version}"))
    checked = []
    with tempfile.TemporaryDirectory() as tmp:
        for f in summary["files"]:
            md5, sha, n = download_and_hash(f["download_url"], Path(tmp))
            checked.append({"name": f["name"], "bytes": n, "sha256": sha, "md5": md5,
                            "md5_matches": bool(f["md5"]) and md5 == f["md5"]})
    out = {"source": f"{API}/articles/{a.article}/versions/{a.version}", "read_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           **{k: v for k, v in summary.items() if k != "files"}, "files": checked,
           "verdict": verdict(summary, a.expect_licence, checked), "data_kept": False}
    Path(a.out).write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out, indent=1))
    return 0 if out["verdict"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
