"""What the distributed artefacts actually contain: image scans, the watch APK's contents,
CycloneDX SBOMs and the third-party notices (requirement K.d, owner directive section 7).

    # scan a built image (what is really inside it), apply the licence policy, write the
    # committed summary docs/security/sbom/<artefact>.inventory.json
    python3 scripts/supply_chain.py image api ira-care/api:ci --base python:3.12-slim --write
    python3 scripts/supply_chain.py image api ira-care/api:ci --base python:3.12-slim --check \\
        --out /tmp/api.inventory.json        # CI: policy on the fresh scan, file kept as artifact

    # the watch APKs: native libraries (with the AAR each came from), model files, and a check
    # that the androidx libraries inside match the resolved classpath
    python3 scripts/supply_chain.py apk --write | --check

    # copyright lines from the installed packages (python lock venv, node_modules, Gradle cache)
    python3 scripts/supply_chain.py copyrights --write [--only npm-mobile ...]

    # CycloneDX SBOMs and notices, rendered from the committed inventories (no network, no build)
    python3 scripts/supply_chain.py render --write | --check

The licence policy is the one in scripts/licence_check.py (docs/security/LICENCES.md). Packages
of the operating system in a base image (Debian, Alpine) are recorded, not judged by that
policy: a GPL program such as bash shipped beside our code is aggregation. What is judged is
what our own code links: for each OS package the scan records which of our components load a
library from it directly (ELF DT_NEEDED), and a GPL-only library linked that way is reported.

This is an engineering record against a written policy. It is not legal advice.
"""
from __future__ import annotations

import argparse
import email
import hashlib
import io
import json
import re
import struct
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import licence_check as lc  # noqa: E402

ROOT = lc.ROOT
SBOM_DIR = ROOT / "docs" / "security" / "sbom"
NATIVE_REVIEW = ROOT / "docs" / "security" / "native_libraries.json"
COPYRIGHTS = ROOT / "docs" / "legal" / "copyright_notices.json"
NOTICES_DIR = ROOT / "docs" / "legal" / "notices"
NOTICES_INDEX = ROOT / "docs" / "legal" / "THIRD_PARTY_NOTICES.md"
LICENCE_TEXTS = ROOT / "docs" / "legal" / "licences"
APK_CONTENTS = SBOM_DIR / "watch-apk-contents.json"
DATASETS = ROOT / "docs" / "DATASETS.md"
WATCH = ROOT / "watch"

#: The images CI builds, the Dockerfile, the base image (for "base" versus "added") and the
#: inventory ecosystem whose reviewed exceptions apply to the language packages inside.
IMAGES = {
    "api": {"dockerfile": "deploy/Dockerfile.api", "base": "python:3.12-slim",
            "exceptions_from": {"python": "python", "npm": "npm-dashboard"},
            # Installed with --no-deps --require-hashes: the image's own Python packages are
            # exactly these, so any difference from them (or from the snapshot) is a failure.
            "python_locks": ["supracloud-jarvis/ira/requirements-api.lock",
                             "supracloud-jarvis/ira/requirements-nodeps.txt"]},
    "dashboard": {"dockerfile": "deploy/Dockerfile.dashboard", "base": "node:22-alpine",
                  "exceptions_from": {"python": "python", "npm": "npm-dashboard"}},
    "backup": {"dockerfile": "deploy/Dockerfile.backup", "base": "postgres:16-alpine",
               "exceptions_from": {"python": "python", "npm": "npm-dashboard"}},
}


def dumps(data) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------------------------
# ELF: the shared libraries a binary asks for (DT_NEEDED) and its own name (DT_SONAME)
# ---------------------------------------------------------------------------------------------

def elf_dynamic(data: bytes) -> tuple[str, list[str]] | None:
    """(soname or "", [needed]) for an ELF file, or None when it is not ELF or has no dynamic
    section. Reads the program headers only, so stripped binaries are fine."""
    if data[:4] != b"\x7fELF" or len(data) < 64:
        return None
    is64, end = data[4] == 2, "<" if data[5] == 1 else ">"
    try:
        if is64:
            phoff, = struct.unpack_from(end + "Q", data, 32)
            phentsize, phnum = struct.unpack_from(end + "HH", data, 54)
        else:
            phoff, = struct.unpack_from(end + "I", data, 28)
            phentsize, phnum = struct.unpack_from(end + "HH", data, 42)
        loads, dyn = [], None
        for i in range(phnum):
            o = phoff + i * phentsize
            if is64:
                ptype, _, off, vaddr, _, filesz = struct.unpack_from(end + "IIQQQQ", data, o)
            else:
                ptype, off, vaddr, _, filesz = struct.unpack_from(end + "IIIII", data, o)
            if ptype == 1:
                loads.append((vaddr, off, filesz))
            elif ptype == 2:
                dyn = (off, filesz)
        if dyn is None:
            return None
        size = 16 if is64 else 8
        fmt = end + ("qQ" if is64 else "iI")
        needed, strtab, soname = [], None, None
        for o in range(dyn[0], dyn[0] + dyn[1], size):
            tag, val = struct.unpack_from(fmt, data, o)
            if tag == 0:
                break
            if tag == 1:
                needed.append(val)
            elif tag == 5:
                strtab = val
            elif tag == 14:
                soname = val
        if strtab is None:
            return None
        base = next((off + strtab - va for va, off, sz in loads if va <= strtab < va + sz), None)
        if base is None:
            return None

        def string(at: int) -> str:
            j = data.index(b"\0", base + at)
            return data[base + at:j].decode("utf-8", "replace")
        return (string(soname) if soname is not None else ""), [string(n) for n in needed]
    except (struct.error, ValueError):
        return None


# ---------------------------------------------------------------------------------------------
# Copyright lines and licence files
# ---------------------------------------------------------------------------------------------

_COPYRIGHT = re.compile(r"^\s*(?:copyright\b|\(c\)\s|©)", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"[\[<{]|yyyy|name of (copyright|author)|\byear\b.*\bname\b", re.I)
LICENCE_FILE = re.compile(r"(^|/)(licen[cs]e|copying|notice|copyright)([._-][^/]*)?$", re.I)


_DISCLAIMER = re.compile(r"\b(liable|warrant|holders? (and|or|be)\b|owner or contributors|"
                         r"notice and this permission|notices?,? (this|and)\b)", re.I)
_YEAR = re.compile(r"\b(19|20)\d\d\b")


def copyright_lines(text: str, limit: int = 6) -> list[str]:
    """Copyright statements in a licence file. Licence prose that merely starts with the word
    ("copyright license to reproduce", "(c) You must retain") is skipped."""
    out: list[str] = []
    for line in text.splitlines()[:400]:
        if not _COPYRIGHT.match(line):
            continue
        clean = " ".join(line.replace("\t", " ").split()).strip(" *#/;")
        after = re.sub(r"^(copyright|\(c\)|©)\s*", "", clean, flags=re.I)
        if after[:1].islower() or (not clean.lower().startswith("copyright")
                                   and not _YEAR.search(clean)):
            continue
        if _DISCLAIMER.search(clean):
            continue
        if len(clean) < 12 or len(clean) > 200 or _PLACEHOLDER.search(clean):
            continue
        if clean.lower().startswith("copyright notice") or clean.lower() == "copyright":
            continue
        if clean not in out:
            out.append(clean)
        if len(out) >= limit:
            break
    return out


def debian_copyright(text: str) -> tuple[list[str], list[str]]:
    """(licence names, copyright lines) from a Debian copyright file (DEP-5 or free form)."""
    licences, holders = [], []
    field = None
    for line in text.splitlines():
        if line[:1] not in (" ", "\t") and ":" in line:
            field, value = line.split(":", 1)
            field = field.strip().lower()
            value = value.strip()
            if field == "license" and value:
                name = value.split()[0] if value else ""
                if name and name not in licences:
                    licences.append(name)
            elif field == "copyright" and value:
                holders.append(value)
        elif field == "copyright" and line.strip() and line.strip() != ".":
            holders.append(line.strip())
        elif not line.strip():
            field = None
    if not holders:
        holders = copyright_lines(text)
    clean = []
    for h in holders:
        h = " ".join(h.split())
        if h and not _PLACEHOLDER.search(h) and h not in clean:
            clean.append(h if h.lower().startswith(("copyright", "(c)", "©")) else f"Copyright {h}")
    return licences, clean[:6]


# ---------------------------------------------------------------------------------------------
# Image scan: one pass over `docker export` of a created (never started) container
# ---------------------------------------------------------------------------------------------

def _canon(path: str) -> str:
    p = "/" + path.lstrip("./")
    for d in ("/usr/lib/", "/usr/bin/", "/usr/sbin/", "/usr/lib64/"):
        if p.startswith(d):
            return p[4:]
    return p


def _rel(path: str) -> str:
    return path.lstrip("./")


def export_stream(image: str):
    cid = subprocess.run(["docker", "create", image], capture_output=True, text=True,
                         check=True).stdout.strip()
    proc = subprocess.Popen(["docker", "export", cid], stdout=subprocess.PIPE)
    return cid, proc


#: Model weights and model data, wherever they sit in an image (a wheel can carry one).
MODEL_FILE = re.compile(r"\.(onnx|tflite|safetensors|gguf|pt|ckpt|h5|pb)$", re.I)
_DIST = re.compile(r"^(?P<site>.*/(?:site|dist)-packages)/(?P<dist>[^/]+)\.dist-info/(?P<rest>.+)$")
_NPM = re.compile(r"^(?P<dir>(?:.*/)?node_modules/(?:@[^/]+/)?[^/@.][^/]*)/(?P<rest>[^/]+)$")


def scan_tar(fileobj) -> dict:
    """Facts from an image's file system, read in one streaming pass."""
    facts: dict = {"elf": {}, "links": {}, "dpkg_status": "", "dpkg_lists": {},
                   "deb_copyright": {}, "apk_db": "", "dists": {}, "npm": {}, "os_release": ""}
    with tarfile.open(fileobj=fileobj, mode="r|") as tar:
        for m in tar:
            name = _rel(m.name)
            if m.issym():
                facts["links"][_canon(name)] = m.linkname
                continue
            if not m.isfile():
                continue
            f = tar.extractfile(m)
            if f is None:
                continue
            want_text = None
            if name == "var/lib/dpkg/status":
                want_text = "dpkg_status"
            elif name.startswith("var/lib/dpkg/info/") and name.endswith(".list"):
                pkg = name.rsplit("/", 1)[1][:-5].split(":")[0]
                facts["dpkg_lists"][pkg] = [_canon(x) for x in
                                            f.read().decode("utf-8", "replace").splitlines() if x]
                continue
            elif re.match(r"usr/share/doc/[^/]+/copyright$", name):
                facts["deb_copyright"][name.split("/")[3]] = f.read().decode("utf-8", "replace")
                continue
            elif name in ("lib/apk/db/installed", "usr/lib/apk/db/installed"):
                want_text = "apk_db"
            elif name in ("etc/os-release", "usr/lib/os-release") and not facts["os_release"]:
                want_text = "os_release"
            if want_text:
                facts[want_text] = f.read().decode("utf-8", "replace")
                continue
            d = _DIST.match("/" + name)
            if d:
                key = (d.group("site"), d.group("dist"))
                rest = d.group("rest")
                dist = facts["dists"].setdefault(key, {"licence_files": {}})
                if rest == "METADATA":
                    dist["metadata"] = f.read().decode("utf-8", "replace")
                elif rest == "RECORD":
                    dist["record"] = f.read().decode("utf-8", "replace")
                elif LICENCE_FILE.search(rest) or rest.startswith(("licenses/", "license_files/")):
                    dist["licence_files"][rest] = f.read(200_000).decode("utf-8", "replace")
                continue
            n = _NPM.match("/" + name)
            if n and (n.group("rest") == "package.json" or LICENCE_FILE.search(n.group("rest"))):
                pkg = facts["npm"].setdefault(n.group("dir"), {"licence_files": {}})
                if n.group("rest") == "package.json":
                    try:
                        pkg["manifest"] = json.loads(f.read().decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        pkg["manifest"] = {}
                else:
                    pkg["licence_files"][n.group("rest")] = f.read(200_000).decode("utf-8",
                                                                                  "replace")
                continue
            if MODEL_FILE.search(name) or name.startswith("app/care/models/"):
                data = f.read()
                facts.setdefault("models", {})[_canon(name)] = {"sha256": sha256(data),
                                                                "size": len(data)}
                continue
            head = f.read(4)
            if head == b"\x7fELF":
                data = head + f.read()
                dyn = elf_dynamic(data)
                facts["elf"][_canon(name)] = {"sha256": sha256(data), "size": len(data),
                                              "soname": dyn[0] if dyn else "",
                                              "needed": dyn[1] if dyn else []}
    return facts


def scan_image(image: str) -> dict:
    cid, proc = export_stream(image)
    try:
        facts = scan_tar(proc.stdout)
    finally:
        proc.stdout.close()
        proc.wait()
        subprocess.run(["docker", "rm", cid], capture_output=True, check=False)
    if proc.returncode:
        raise RuntimeError(f"docker export of {image} failed")
    return facts


def _dpkg_packages(status: str) -> dict[str, str]:
    out = {}
    for block in status.split("\n\n"):
        fields = dict(re.findall(r"^([A-Za-z-]+): (.*)$", block, re.M))
        if fields.get("Package") and "installed" in fields.get("Status", ""):
            out[fields["Package"]] = fields.get("Version", "")
    return out


def _apk_packages(db: str) -> tuple[dict[str, dict], dict[str, str]]:
    pkgs, owner = {}, {}
    for block in db.split("\n\n"):
        cur, folder = {}, ""
        for line in block.splitlines():
            if line[:2] == "P:":
                cur["name"] = line[2:]
            elif line[:2] == "V:":
                cur["version"] = line[2:]
            elif line[:2] == "L:":
                cur["licence"] = line[2:]
            elif line[:2] == "o:":
                cur["origin"] = line[2:]
            elif line[:2] == "F:":
                folder = line[2:]
            elif line[:2] == "R:" and cur.get("name"):
                owner[_canon(f"{folder}/{line[2:]}")] = cur["name"]
        if cur.get("name"):
            pkgs[cur["name"]] = cur
    return pkgs, owner


def _dist_files(site: str, record: str) -> list[str]:
    out = []
    for line in record.splitlines():
        rel = line.rsplit(",", 2)[0] if line.count(",") >= 2 else line
        if rel:
            out.append(_canon(str(PurePosixPath(site.lstrip("/")) / rel)))
    return out


def _norm_posix(p: str) -> str:
    parts: list[str] = []
    for part in p.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part not in ("", "."):
            parts.append(part)
    return "/" + "/".join(parts)


def load_native_review() -> tuple[dict, dict]:
    """(bundled libraries by stem, linked OS packages by name), each {licence, evidence}."""
    data = json.loads(NATIVE_REVIEW.read_text(encoding="utf-8"))
    return data["bundled"], data["os_linked"]


def native_stem(filename: str) -> str:
    """libgfortran-040039e1.so.5.0.0 -> libgfortran; libopenblas64_p-r0-0cf96a72.3.23.dev.so
    -> libopenblas64_p; libtensorflowlite_jni.so -> libtensorflowlite_jni."""
    return re.split(r"-|\.so", filename, maxsplit=1)[0]


def image_inventory(artefact: str, facts: dict, base_facts: dict | None,
                    exceptions: list[dict], review: dict, review_os: dict,
                    note: str = "") -> dict:
    """The committed summary of one image: language packages with the policy applied, OS
    packages with their licences and who links them, and native libraries bundled in wheels."""
    conf = IMAGES[artefact]
    base_pkgs: set[str] = set()
    if base_facts is not None:
        base_pkgs = set(_dpkg_packages(base_facts["dpkg_status"])) | set(
            _apk_packages(base_facts["apk_db"])[0])

    # Who owns each file.
    owner: dict[str, tuple[str, str]] = {}
    os_pkgs: dict[str, dict] = {}
    if facts["dpkg_status"]:
        for name, version in _dpkg_packages(facts["dpkg_status"]).items():
            lic, holders = debian_copyright(facts["deb_copyright"].get(name, ""))
            os_pkgs[name] = {"version": version, "kind": "deb", "licences": sorted(lic),
                             "copyright": holders}
            for p in facts["dpkg_lists"].get(name, []):
                owner[p] = ("os", name)
    if facts["apk_db"]:
        apks, apk_owner = _apk_packages(facts["apk_db"])
        for name, p in apks.items():
            os_pkgs[name] = {"version": p.get("version", ""), "kind": "apk",
                             "licences": [p.get("licence", "")] if p.get("licence") else [],
                             "copyright": []}
        for path, name in apk_owner.items():
            owner[path] = ("os", name)
    for name, p in os_pkgs.items():
        p["origin"] = ("base image" if name in base_pkgs else "added by the Dockerfile") \
            if base_facts is not None else "not compared with the base image"

    base_python: set[str] = set()
    if base_facts is not None:
        for d in base_facts["dists"].values():
            if "metadata" in d:
                meta = email.message_from_string(d["metadata"])
                base_python.add(f"{lc.canonical(meta['Name'] or '')}@{meta['Version'] or ''}")
    python: dict[str, dict] = {}
    py_files: dict[str, str] = {}
    for (site, _dist), d in sorted(facts["dists"].items()):
        if "metadata" not in d:
            continue
        meta = email.message_from_string(d["metadata"])
        name, version = lc.canonical(meta["Name"] or _dist), meta["Version"] or ""
        declared, source = lc.python_declared(meta)
        e = _image_entry(artefact, conf["exceptions_from"]["python"], name, version, declared,
                         source, exceptions)
        e["location"] = site
        e["copyright"] = _holders(d["licence_files"]) or _author(meta)
        e["origin"] = ("base image" if f"{name}@{version}" in base_python
                       else "added by the Dockerfile") if base_facts is not None \
            else "not compared with the base image"
        python[f"{name}@{version}"] = e
        for p in _dist_files(site, d.get("record", "")):
            py_files[p] = f"{name}@{version}"
    for p, k in py_files.items():
        owner.setdefault(p, ("python", k))

    npm: dict[str, dict] = {}
    npm_dirs: list[tuple[str, str]] = []
    for pdir, d in sorted(facts["npm"].items()):
        m = d.get("manifest") or {}
        if not m.get("name") or not m.get("version"):
            continue
        declared, source = lc.npm_declared(m)
        e = _image_entry(artefact, conf["exceptions_from"]["npm"], m["name"], m["version"],
                         declared, source, exceptions)
        key = f"{m['name']}@{m['version']}"
        e["location"] = pdir if key not in npm else min(pdir, npm[key]["location"])
        e["copyright"] = _holders(d["licence_files"]) or _author_npm(m)
        npm[key] = e
        npm_dirs.append((_canon(pdir) + "/", key))
    npm_dirs.sort(key=lambda x: -len(x[0]))

    def who(path: str) -> tuple[str, str]:
        if path in owner:
            return owner[path]
        for prefix, key in npm_dirs:
            if path.startswith(prefix):
                return ("npm", key)
        return ("file", path)

    # Resolve each ELF's DT_NEEDED to a file in the image, then to its owner.
    by_name: dict[str, list[str]] = {}
    for path, info in facts["elf"].items():
        by_name.setdefault(PurePosixPath(path).name, []).append(path)
        if info["soname"]:
            by_name.setdefault(info["soname"], []).append(path)
    for link in facts["links"]:
        by_name.setdefault(PurePosixPath(link).name, []).append(link)
    sysdirs = ("/lib/x86_64-linux-gnu/", "/lib/aarch64-linux-gnu/", "/usr/local/lib/", "/lib/")

    def resolve(path: str, depth: int = 0) -> str:
        target = facts["links"].get(path)
        if target is None or depth > 8:
            return path
        nxt = _norm_posix(target if target.startswith("/")
                          else str(PurePosixPath(path).parent / target))
        return resolve(_canon(nxt), depth + 1)

    def find(soname: str, wanted_by: str) -> str | None:
        cands = by_name.get(soname, [])
        if not cands:
            return None
        here = str(PurePosixPath(wanted_by).parent) + "/"
        ranked = sorted(set(cands), key=lambda c: (
            not c.startswith(here),
            not (".libs/" in c and c.split(".libs/")[0].rsplit("/", 1)[0]
                 == wanted_by.rsplit("/", 2)[0]),
            next((i for i, d in enumerate(sysdirs) if c.startswith(d)), 9), c))
        return resolve(ranked[0])

    linked_by: dict[str, set[str]] = {}
    bundled: dict[str, dict] = {}
    for path, info in sorted(facts["elf"].items()):
        kind, comp = who(path)
        if kind == "os":
            continue
        label = comp if kind != "file" else _file_label(path)
        for soname in info["needed"]:
            target = find(soname, path)
            if target is None:
                continue
            tkind, tcomp = who(target)
            if tkind == "os":
                linked_by.setdefault(tcomp, set()).add(label)
        if kind == "python" and re.search(r"\.libs/[^/]+$", path):
            stem = native_stem(PurePosixPath(path).name)
            rev = review.get(stem)
            declared = rev["licence"] if rev else ""
            e = lc.entry("native-libraries", stem, _so_version(PurePosixPath(path).name),
                         declared, rev["evidence"] if rev else "not reviewed", exceptions)
            e.update(file=path, shipped_in=comp, sha256=info["sha256"],
                     copyright=(rev or {}).get("copyright", []))
            if not rev:
                e["status"] = "forbidden"
                e["reason"] = (f"{stem}: bundled native library not in "
                               f"{NATIVE_REVIEW.relative_to(ROOT)}")
            bundled[path] = e
    linked: dict[str, dict] = {}
    for name, p in os_pkgs.items():
        p["linked_by"] = sorted(linked_by.get(name, ()))
        # Debian's copyright file covers the whole source package (programs, tests, build
        # scripts), so a GPL name here is a pointer, not the library's licence.
        p["gpl_named_in_copyright_file"] = any(re.match(r"^A?GPL", x) for x in p["licences"])
        if p["linked_by"]:
            rev = review_os.get(name)
            e = lc.entry("os-linked", name, p["version"], rev["licence"] if rev else "",
                         rev["evidence"] if rev else "not reviewed", exceptions)
            e["linked_by"] = p["linked_by"]
            if not rev:
                e["status"] = "forbidden"
                e["reason"] = (f"{name}: an OS library linked by our components, not in "
                               f"{NATIVE_REVIEW.relative_to(ROOT)}")
            linked[f"{name}@{p['version']}"] = e
    other_elf = sorted(_file_label(p) for p in facts["elf"] if who(p)[0] == "file")
    os_name = dict(re.findall(r'^(ID|VERSION_ID)="?([^"\n]*)"?$', facts["os_release"], re.M))
    return {
        "artefact": f"{artefact}-image", "dockerfile": conf["dockerfile"],
        "base_image": conf["base"], "operating_system": f"{os_name.get('ID', '')} "
                                                        f"{os_name.get('VERSION_ID', '')}".strip(),
        "note": note,
        "python": dict(sorted(python.items())), "npm": dict(sorted(npm.items())),
        "os_packages": {f"{n}@{p['version']}": p for n, p in sorted(os_pkgs.items())},
        "bundled_native_libraries": dict(sorted(bundled.items())),
        "linked_os_libraries": dict(sorted(linked.items())),
        "model_files": {p: {**m, "owner": _owner_label(who(p))}
                        for p, m in sorted(facts.get("models", {}).items())},
        "other_binaries": sorted(set(other_elf)),
    }


def _image_entry(artefact: str, shared_eco: str, name: str, version: str, declared: str,
                 source: str, exceptions: list[dict]) -> dict:
    """The policy for a package inside an image. A reviewed exception for the same package and
    licence in the source inventory (the lock, the dashboard's tree) applies; one specific to
    the image is named with the ecosystem image-<artefact>."""
    e = lc.entry(shared_eco, name, version, declared, source, exceptions)
    if e["status"] == "forbidden":
        e = lc.entry(f"image-{artefact}", name, version, declared, source, exceptions)
    return e


def _owner_label(owner: tuple[str, str]) -> str:
    kind, comp = owner
    return comp if kind in ("python", "npm") else ("OS package " + comp if kind == "os"
                                                   else "IRA CARE (copied by the Dockerfile)")


def _so_version(filename: str) -> str:
    m = re.search(r"\.so\.([0-9][0-9.]*)$", filename)
    return m.group(1) if m else "unversioned"


def _file_label(path: str) -> str:
    if re.match(r"^/usr/local/(bin/python|lib/libpython|lib/python3\.\d+/(lib-dynload|config-)/?)", path):
        return "CPython (base image, built from source)"
    if path.startswith("/usr/local/bin/node"):
        return "Node.js (base image)"
    if re.match(r"^/usr/local/(bin/(pg_|postgres|psql|initdb|ecpg|clusterdb|createdb|createuser|"
                r"dropdb|dropuser|oid2name|pgbench|reindexdb|vacuum)|lib/(postgresql/|libpq|"
                r"libecpg|libpgtypes))", path):
        return "PostgreSQL (base image, built from source)"
    if path == "/usr/local/bin/gosu":
        return "gosu (base image)"
    return path


def _holders(files: dict[str, str]) -> list[str]:
    out: list[str] = []
    for _name, text in sorted(files.items()):
        for line in copyright_lines(text):
            if line not in out:
                out.append(line)
    return out[:6]


def _author(meta) -> list[str]:
    who = (meta.get("Author") or meta.get("Maintainer") or "").strip()
    if not who:
        ae = meta.get("Author-email") or meta.get("Maintainer-email") or ""
        who = re.sub(r"\s*<[^>]*>", "", ae).strip().strip('"')
    return [f"Author (package metadata): {' '.join(who.split())}"] if who else []


def _author_npm(manifest: dict) -> list[str]:
    a = manifest.get("author")
    if isinstance(a, dict):
        a = a.get("name")
    if isinstance(a, str) and a.strip():
        name = re.sub(r"\s*[<(][^>)]*[>)]", "", a).strip()
        return [f"Author (package.json): {name}"]
    return []


def image_problems(inv: dict, exceptions: list[dict] | None = None) -> list[str]:
    """Forbidden language packages, bundled native libraries and OS libraries that our own
    components link (each without a reviewed exception). With `exceptions`, every recorded
    verdict is also worked out again, so a hand-edited status or a dropped exception shows."""
    out = []
    artefact = inv["artefact"].removesuffix("-image")
    conf = IMAGES[artefact]["exceptions_from"]
    for sec in ("python", "npm", "bundled_native_libraries", "linked_os_libraries"):
        for key, e in inv[sec].items():
            status = e["status"]
            if exceptions is not None:
                args = (e["name"], e["version"], e["declared"], e["source"], exceptions)
                fresh = (_image_entry(artefact, conf[sec], *args) if sec in ("python", "npm")
                         else lc.entry("native-libraries" if sec.startswith("bundled")
                                       else "os-linked", *args))
                if fresh["status"] != status and not (status == "forbidden"
                                                      and "not in" in e.get("reason", "")):
                    out.append(f"{inv['artefact']} {sec} {key}: recorded {status}, the policy "
                               f"says {fresh['status']}")
                    status = fresh["status"]
            if status == "forbidden":
                out.append(f"{inv['artefact']} {sec} {key}: FORBIDDEN {e['licence']!r}: "
                           f"{e.get('reason', '')}")
    return out


# ---------------------------------------------------------------------------------------------
# The watch APKs: what is really inside, against the resolved classpath
# ---------------------------------------------------------------------------------------------

#: Model and data files in the watch app, with the rows of docs/ai/MODEL_WEIGHTS.md and
#: docs/DATASETS.md they correspond to. The release fall model's hash is pinned there too.
WATCH_ASSETS = {
    "assets/fall_model.tflite": "IRA fall model v3 (BITS-2 and UCI HAR, CC BY 4.0)",
    "assets/fall_model.json": "IRA fall model v3 metadata",
    "assets/activity_model.json": "IRA Care Activity model (WISDM, CC BY 4.0)",
    "assets/NOTICE-activity-model.txt": "attribution for the activity model's training data",
    "assets/THIRD_PARTY_NOTICES.txt": "the third-party notices of this build (About, "
                                      "Open-source licences)",
}
RELEASE_FALL_MODEL_SHA256 = "2cab7c9bcbf60de51d7c42f5690ad549bcbf4f13eb6266c35bf5d0a59b063278"


def locked_python(artefact: str) -> dict[str, str]:
    """``{name@version: lock file}`` for every package an image installs from its hashed locks."""
    out = {}
    for rel in IMAGES[artefact].get("python_locks", []):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for name, version in re.findall(r"(?m)^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+) \\$", text):
            out[f"{lc.canonical(name)}@{version}"] = rel
    return out


def python_lock_problems(inv: dict, locked: dict[str, str]) -> list[str]:
    """The image's own Python packages are exactly its locks: nothing added (a test tool, a
    package that floated in), nothing missing, no other version. Packages of the base image
    (its pip) are the base image's."""
    art = inv["artefact"]
    if not locked:
        return []
    ours = {k for k, e in inv["python"].items() if e.get("origin") != "base image"}
    out = [f"{art}: Python package {k} is in the image but not in its locks"
           for k in sorted(ours - set(locked))]
    out += [f"{art}: {k} from {locked[k]} is not installed in the image"
            for k in sorted(set(locked) - set(inv["python"]))]
    if any(e.get("origin") == "not compared with the base image" for e in inv["python"].values()):
        out.append(f"{art}: scanned without --base, so the base image's packages cannot be told "
                   "from the Dockerfile's")
    return out


def watch_apks() -> dict[str, Path]:
    out = {}
    for flavour in lc.GRADLE_PROJECTS["gradle-watch"][2]:
        hits = sorted((WATCH / "app" / "build" / "outputs" / "apk" / flavour / "release")
                      .glob("*.apk"))
        if hits:
            out[flavour] = hits[0]
    return out


def _module_archives(key: str) -> list[Path]:
    """The AAR or JAR files of one inventory key (group:name@version) in the Gradle cache."""
    ga, version = key.rsplit("@", 1)
    group, name = ga.split(":")
    base = lc.gradle_user_home() / "caches" / "modules-2" / "files-2.1" / group / name / version
    return sorted(p for p in base.glob(f"*/{name}-{version}.*") if p.suffix in (".aar", ".jar"))


def version_file_matches(stem: str, text: str, known: set[str]) -> bool:
    """META-INF/<stem>.version names a library on the classpath. androidx writes
    <group>_<artifact>.version with the version inside (one androidx build writes a Gradle task
    name instead of the version: then the module alone must match). kotlinx writes
    kotlinx_coroutines_core.version for org.jetbrains.kotlinx:kotlinx-coroutines-core."""
    version_ok = bool(re.fullmatch(r"[0-9][0-9A-Za-z.+-]*", text))
    if stem.startswith("androidx."):
        group, _, artifact = stem.rpartition("_")
        if version_ok:
            return f"{group}:{artifact}@{text}" in known
        return any(k.startswith(f"{group}:{artifact}@") for k in known)
    artifact = stem.replace("_", "-")
    return any(k.split(":", 1)[1].startswith(f"{artifact}@" if not version_ok else artifact)
               and (not version_ok or k.endswith(f"@{text}")) for k in known)


def apk_contents(inventory: dict) -> tuple[dict, list[str]]:
    """{flavour: contents} for the built release APKs, and the problems found."""
    sec = inventory["ecosystems"]["gradle-watch"]
    out, problems = {}, []
    apks = watch_apks()
    for flavour in sorted(lc.GRADLE_PROJECTS["gradle-watch"][2]):
        if flavour not in apks:
            problems.append(f"watch {flavour}: no release APK built (assemble"
                            f"{flavour.capitalize()}Release first)")
            continue
        keys = sec["flavours"][flavour]
        jni: dict[tuple[str, str], list[str]] = {}
        for key in keys:
            for archive in _module_archives(key):
                with zipfile.ZipFile(archive) as z:
                    for n in z.namelist():
                        m = re.match(r"^jni/([^/]+)/([^/]+\.so)$", n)
                        if m:
                            jni.setdefault((m.group(1), m.group(2)), []).append(key)
        with zipfile.ZipFile(apks[flavour]) as z:
            names = sorted(z.namelist())
            natives = []
            for n in names:
                m = re.match(r"^lib/([^/]+)/([^/]+\.so)$", n)
                if not m:
                    continue
                origin = sorted(set(jni.get((m.group(1), m.group(2)), [])))
                if not origin:
                    problems.append(f"watch {flavour}: {n} comes from no module on the classpath")
                for o in origin:
                    if sec["packages"][o]["status"] == "forbidden":
                        problems.append(f"watch {flavour}: {n} comes from {o}, whose licence is "
                                        "forbidden")
                natives.append({"abi": m.group(1), "file": m.group(2), "from": origin,
                                "licence": " AND ".join(sorted({sec["packages"][o]["licence"]
                                                                for o in origin})) or "unknown"})
            assets = []
            for n in names:
                if n.startswith("assets/") and not n.startswith("assets/dexopt/"):
                    data = z.read(n)
                    assets.append({"file": n, "sha256": sha256(data), "size": len(data),
                                   "what": WATCH_ASSETS.get(n, "not described")})
                    if n not in WATCH_ASSETS:
                        problems.append(f"watch {flavour}: asset {n} is not described in "
                                        "WATCH_ASSETS (scripts/supply_chain.py)")
                    if n == "assets/fall_model.tflite" and sha256(data) != RELEASE_FALL_MODEL_SHA256:
                        problems.append(f"watch {flavour}: the release APK's fall model is not "
                                        "the v3 model")
            versions, unmatched = 0, []
            known = set(keys) | set(sec.get("no_code_modules", []))
            for n in names:
                m = re.match(r"^META-INF/([^/]+)\.version$", n)
                if not m:
                    continue
                versions += 1
                v = z.read(n).decode("utf-8", "replace").strip()
                if not version_file_matches(m.group(1), v, known):
                    unmatched.append(f"{m.group(1)}.version ({v[:40]})")
            if unmatched:
                problems.append(f"watch {flavour}: library version files in the APK name modules "
                                f"that are not on the resolved classpath: {unmatched[:5]}")
        out[flavour] = {"apk": str(apks[flavour].relative_to(ROOT)), "native_libraries": natives,
                        "assets": assets, "library_version_files_checked": versions}
    return out, problems


# ---------------------------------------------------------------------------------------------
# Copyright lines of the inventory packages, read from what is installed here
# ---------------------------------------------------------------------------------------------

def python_copyrights() -> dict[str, list[str]]:
    from importlib import metadata
    out = {}
    for name, version in sorted(lc.locked().items()):
        dist = metadata.distribution(name)
        texts = {}
        for f in dist.files or []:
            if ".dist-info/" in str(f) and LICENCE_FILE.search(str(f)):
                try:
                    texts[str(f)] = Path(dist.locate_file(f)).read_text(encoding="utf-8",
                                                                        errors="replace")
                except OSError:
                    pass
        out[f"{name}@{version}"] = _holders(texts) or _author(dist.metadata)
    return out


def npm_copyrights(ecosystem: str) -> dict[str, list[str]]:
    project = lc.NPM_PROJECTS[ecosystem]
    out: dict[str, list[str]] = {}

    def walk(node: dict) -> None:
        for child in (node.get("dependencies") or {}).values():
            if child.get("extraneous") or child.get("missing"):
                continue
            path = child.get("path")
            if path:
                d = Path(path)
                m = json.loads((d / "package.json").read_text(encoding="utf-8"))
                texts = {f.name: f.read_text(encoding="utf-8", errors="replace")
                         for f in sorted(d.iterdir())
                         if f.is_file() and LICENCE_FILE.search(f.name)}
                out[f"{m['name']}@{m['version']}"] = _holders(texts) or _author_npm(m)
            walk(child)

    walk(lc.npm_tree(project))
    return dict(sorted(out.items()))


def gradle_copyrights(inventory: dict) -> dict[str, list[str]]:
    out = {}
    for key in inventory["ecosystems"]["gradle-watch"]["packages"]:
        texts = {}
        for archive in _module_archives(key):
            with zipfile.ZipFile(archive) as z:
                inner = [z]
                if "classes.jar" in z.namelist():
                    inner.append(zipfile.ZipFile(io.BytesIO(z.read("classes.jar"))))
                for zz in inner:
                    for n in zz.namelist():
                        if re.match(r"^(META-INF/)?[^/]*$", n) and LICENCE_FILE.search(n):
                            texts[n] = zz.read(n).decode("utf-8", "replace")
        lines = _holders(texts)
        if not lines:
            ga, version = key.rsplit("@", 1)
            path = lc.pom_path(*ga.split(":"), version)
            if path is not None:
                root = lc._pom_root(path)
                org = lc._pom_text(root.find(lc._POM_NS + "organization"), "name")
                devs = root.find(lc._POM_NS + "developers")
                names = [lc._pom_text(d, "organization") or lc._pom_text(d, "name")
                         for d in (list(devs) if devs is not None else [])]
                who = org or next((n for n in names if n), "")
                if who:
                    lines = [f"Publisher (POM): {who}"]
        out[key] = lines
    return out


# ---------------------------------------------------------------------------------------------
# Licence texts (SPDX licence list data, committed under docs/legal/licences/)
# ---------------------------------------------------------------------------------------------

SPDX_TEXTS = "https://raw.githubusercontent.com/spdx/license-list-data/v3.25.0/text/{}.txt"
#: Texts that are ours, for LicenseRef ids (no SPDX text exists).
OWN_TEXTS = {
    "LicenseRef-public-domain": "The authors placed this work in the public domain. No licence "
                                "conditions apply; the notice is kept for attribution.\n",
    "BSD": "The packages listed under \"BSD\" declare a BSD licence without saying which "
           "variant (two-clause, three-clause). Each package's own licence file is the governing "
           "text, with its copyright line; in a container image it is inside the package's "
           "installed files (for Python, the .dist-info directory). Every BSD variant asks that "
           "the copyright notice, the list of conditions and the disclaimer be reproduced with "
           "binary distributions.\n",
}


#: Declared ids whose text is filed under another name: deprecated SPDX ids, and spellings
#: whose meaning was checked in the package (huggingface-hub's "Apache" is the 2.0 text).
TEXT_ALIASES = {"GPL-2.0": "GPL-2.0-only", "LGPL-3.0": "LGPL-3.0-only", "GPL-3.0": "GPL-3.0-only",
                "Apache": "Apache-2.0", "Public-Domain": "LicenseRef-public-domain"}


def licence_ids(expression: str) -> list[str]:
    """Every licence and exception id in an SPDX expression, by the name its text is filed
    under."""
    toks = re.findall(r"[^\s()]+", expression or "")
    return sorted({TEXT_ALIASES.get(t, t) for t in toks if t.upper() not in ("AND", "OR", "WITH")
                   and t != lc.NOASSERTION})


def fetch_licence_texts(ids: set[str]) -> list[str]:
    import urllib.request
    LICENCE_TEXTS.mkdir(parents=True, exist_ok=True)
    missing = []
    for i in sorted(ids):
        dest = LICENCE_TEXTS / f"{i}.txt"
        if dest.exists():
            continue
        if i in OWN_TEXTS:
            dest.write_text(OWN_TEXTS[i], encoding="utf-8")
            continue
        try:
            with urllib.request.urlopen(SPDX_TEXTS.format(i), timeout=30) as r:
                text = r.read().decode("utf-8")
        except Exception:  # noqa: BLE001 - reported below
            missing.append(i)
            continue
        dest.write_text(text.rstrip() + "\n", encoding="utf-8")
    return missing


# ---------------------------------------------------------------------------------------------
# Artefacts: what each distributed thing contains, assembled from the committed inventories
# ---------------------------------------------------------------------------------------------

OWN = "Our own weights (trained by IRA CARE; no pretrained start)"

#: The models each artefact carries, with the datasets (rows of docs/DATASETS.md) whose terms
#: carry over. Licences and attribution as in docs/ai/MODEL_WEIGHTS.md.
MODELS = {
    "watch": [
        {"name": "IRA fall model v3", "file": "assets/fall_model.tflite", "licence": OWN,
         "attribution": "", "datasets": ["BITS-2 Dataset for Fall Detection",
                                          "UCI HAR (smartphones)"]},
        {"name": "IRA Care Activity model", "file": "assets/activity_model.json",
         "licence": OWN, "attribution": "",
         "datasets": ["WISDM Smartphone and Smartwatch Activity and Biometrics"]},
    ],
    "api": [
        {"name": "sentence-transformers/all-MiniLM-L6-v2 (ONNX encoder for the answer "
                 "classifier)", "file": "/app/care/models/encoder/model.onnx",
         "licence": "Apache-2.0", "datasets": [],
         "attribution": "sentence-transformers (N. Reimers and contributors). Base model "
                        "nreimers/MiniLM-L6-H384-uncased, MIT."},
        {"name": "Answer-classifier heads", "file": "/app/care/models/classifier_heads.json",
         "licence": OWN, "attribution": "", "datasets": []},
        {"name": "IRA Care Heart selector", "file": "/app/care/models/heart_selector.json",
         "licence": OWN, "attribution": "", "datasets": ["PPG-DaLiA"]},
        {"name": "IRA Care Activity model", "file": "/app/care/models/activity_model.json",
         "licence": OWN, "attribution": "",
         "datasets": ["WISDM Smartphone and Smartwatch Activity and Biometrics"]},
        {"name": "Silero VAD v6 (voice activity detector, shipped inside the faster-whisper "
                 "wheel)",
         "file": "/usr/local/lib/python3.12/site-packages/faster_whisper/assets/"
                 "silero_vad_v6.onnx",
         "licence": "MIT", "datasets": [],
         "attribution": "Copyright (c) 2020-present Silero Team (snakers4/silero-vad, MIT; "
                        "its LICENSE read on 25 November 2026). Distributed inside the "
                        "faster-whisper wheel (SYSTRAN, MIT)."},
    ],
}

#: Downloaded when IRA CARE is installed, not inside any image we build.
DOWNLOADED = [
    ("Gemma 4 (gemma4:e4b, gemma4:e2b, gemma4:12b), pulled through Ollama by deploy/"
     "docker-compose.yml and deploy/compose.gpu.yml", "Apache-2.0",
     "Google DeepMind, Gemma 4. Keep the Apache 2.0 licence and any NOTICE with "
     "redistributed weights."),
]

ARTEFACTS = {
    "watch-wearos": "IRA CARE watch app, Wear OS build (release APK)",
    "watch-aosp": "IRA CARE watch app, plain Android (AOSP) build (release APK)",
    "phone-app": "IRA CARE family app (Expo, Android and iOS)",
    "dashboard-image": "IRA CARE carer dashboard container image (deploy/Dockerfile.dashboard)",
    "api-image": "IRA CARE server and worker container image (deploy/Dockerfile.api)",
    "backup-image": "IRA CARE backup container image (deploy/Dockerfile.backup)",
}


def dataset_rows() -> dict[str, dict]:
    """{dataset name: {licence, citation}} from the tables in docs/DATASETS.md."""
    out = {}
    for line in DATASETS.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| ") or line.startswith("| ---") or line.startswith("| Dataset"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        if len(cells) == 8:
            out[cells[0]] = {"licence": cells[3], "citation": cells[7]}
    return out


def _purl(eco: str, name: str, version: str) -> str:
    if eco == "pypi":
        return f"pkg:pypi/{name}@{version}"
    if eco == "npm":
        return "pkg:npm/" + name.replace("@", "%40", 1) + f"@{version}"
    if eco == "maven":
        group, artifact = name.split(":")
        return f"pkg:maven/{group}/{artifact}@{version}"
    if eco in ("deb", "apk"):
        distro = "debian" if eco == "deb" else "alpine"
        return f"pkg:{eco}/{distro}/{name}@{version}"
    return f"pkg:generic/{name}@{version}"


def _comp(kind: str, eco: str, name: str, version: str, licence: str, status: str,
          copyright_: list[str], where: str, **extra) -> dict:
    c = {"kind": kind, "eco": eco, "name": name, "version": version, "licence": licence,
         "status": status, "copyright": copyright_, "where": where}
    c.update(extra)
    c["ref"] = extra.get("ref") or _purl(eco, name, version)
    return c


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def image_inventory_path(artefact: str) -> Path:
    return SBOM_DIR / f"{artefact}-image.inventory.json"


def artefact_components(artefact: str) -> list[dict]:
    inv = lc.load_inventory()["ecosystems"]
    cr = load_json(COPYRIGHTS) if COPYRIGHTS.is_file() else {}
    comps: list[dict] = []
    if artefact.startswith("watch-"):
        flavour = artefact.split("-", 1)[1]
        sec = inv["gradle-watch"]
        both = set(sec["flavours"]["wearos"]) & set(sec["flavours"]["aosp"])
        for key in sec["flavours"][flavour]:
            e = sec["packages"][key]
            comps.append(_comp("library", "maven", e["name"], e["version"], e["licence"],
                               e["status"], cr.get("gradle-watch", {}).get(key, []),
                               "release runtime classpath" + ("" if key in both else
                                                              f" ({flavour} only)")))
        apk = load_json(APK_CONTENTS)[flavour]
        for n in apk["native_libraries"]:
            comps.append(_comp("native", "generic", n["file"], n["abi"], n["licence"],
                               "allowed" if n["from"] else "forbidden", [],
                               "lib/" + n["abi"] + " in the APK, from " + ", ".join(n["from"]),
                               ref=f"apk:lib/{n['abi']}/{n['file']}"))
        for m in MODELS["watch"]:
            asset = next(a for a in apk["assets"] if a["file"] == m["file"])
            comps.append(_comp("model", "generic", m["name"], asset["sha256"][:12],
                               m["licence"], "own", [], m["file"], sha256=asset["sha256"],
                               datasets=m["datasets"], attribution=m["attribution"],
                               ref=f"model:{m['file']}"))
    elif artefact == "phone-app":
        for key, e in inv["npm-mobile"]["packages"].items():
            comps.append(_comp("library", "npm", e["name"], e["version"], e["licence"],
                               e["status"], cr.get("npm-mobile", {}).get(key, []),
                               "production dependency tree"))
    else:
        name = artefact.removesuffix("-image")
        img = load_json(image_inventory_path(name))
        for key, e in img["python"].items():
            comps.append(_comp("library", "pypi", e["name"], e["version"], e["licence"],
                               e["status"], e["copyright"], e["location"]))
        seen = set()
        for key, e in img["npm"].items():
            seen.add(key)
            comps.append(_comp("library", "npm", e["name"], e["version"], e["licence"],
                               e["status"], e["copyright"], e["location"]))
        if name == "dashboard":
            # Next.js bundles the production tree into the built JavaScript; most of it is not
            # in node_modules in the image, but it ships all the same.
            for key, e in inv["npm-dashboard"]["packages"].items():
                if key not in seen:
                    comps.append(_comp("library", "npm", e["name"], e["version"], e["licence"],
                                       e["status"],
                                       cr.get("npm-dashboard", {}).get(key, []),
                                       "bundled into the built JavaScript (.next)"))
        for path, e in img["bundled_native_libraries"].items():
            comps.append(_comp("native", "generic", e["name"], e["version"], e["licence"],
                               e["status"], e["copyright"], f"{path} (in {e['shipped_in']})",
                               sha256=e["sha256"], ref=f"file:{path}"))
        for key, p in img["os_packages"].items():
            n, v = key.split("@", 1)
            linked = img["linked_os_libraries"].get(key)
            comps.append(_comp("os", p["kind"], n, v,
                               linked["licence"] if linked else " AND ".join(p["licences"]),
                               linked["status"] if linked else "os-package", p["copyright"],
                               p["origin"], os_licences=p["licences"],
                               linked_by=p["linked_by"]))
        for label in img["other_binaries"]:
            comps.append(_comp("binary", "generic", label, "base image",
                               BASE_BINARIES.get(label, ("NOASSERTION",))[0], "base-image", [],
                               BASE_BINARIES.get(label, ("", "not identified"))[1],
                               ref=f"binary:{label}"))
        for m in MODELS.get(name, []):
            f = img["model_files"].get(m["file"])
            if f is None:
                continue
            comps.append(_comp("model", "generic", m["name"], f["sha256"][:12], m["licence"],
                               "own" if m["licence"] == OWN else "allowed", [], m["file"],
                               sha256=f["sha256"], datasets=m["datasets"],
                               attribution=m["attribution"], ref=f"model:{m['file']}"))
    return sorted(comps, key=lambda c: (c["kind"], c["ref"]))


#: Programs that come with a base image and are not in a package manager's database.
BASE_BINARIES = {
    "CPython (base image, built from source)": (
        "PSF-2.0", "/usr/local (python:3.12-slim); its licence is /usr/local/lib/python3.12/"
                   "LICENSE.txt in the image"),
    "Node.js (base image)": ("MIT", "/usr/local/bin/node (node:22-alpine). Node.js bundles "
                                    "V8, libuv, OpenSSL, ICU and others under their own "
                                    "permissive licences"),
    "PostgreSQL (base image, built from source)": ("PostgreSQL", "/usr/local (postgres:16-"
                                                                 "alpine)"),
    "gosu (base image)": ("Apache-2.0", "/usr/local/bin/gosu (postgres:16-alpine)"),
}


# ---------------------------------------------------------------------------------------------
# CycloneDX
# ---------------------------------------------------------------------------------------------

def _cdx_licences(c: dict) -> list[dict]:
    if c["kind"] == "os" and c["eco"] == "deb" and c["status"] == "os-package":
        return [{"license": {"name": n}} for n in c.get("os_licences", [])]
    lic = c["licence"]
    if not lic or lic == lc.NOASSERTION or lic == OWN:
        return [{"license": {"name": lic or lc.NOASSERTION}}]
    return [{"expression": lic}] if lc.normalise(lic) is not None else [
        {"license": {"name": lic}}]


def cyclonedx(artefact: str, comps: list[dict]) -> dict:
    out = []
    for c in comps:
        props = [{"name": "ira:licence-status", "value": c["status"]},
                 {"name": "ira:where", "value": c["where"]}]
        for d in c.get("datasets", []):
            props.append({"name": "ira:trained-on", "value": d})
        if c.get("linked_by"):
            props.append({"name": "ira:linked-by", "value": ", ".join(c["linked_by"])})
        item = {"bom-ref": c["ref"], "name": c["name"], "version": c["version"],
                "type": {"model": "machine-learning-model", "os": "library",
                         "binary": "application"}.get(c["kind"], "library"),
                "licenses": _cdx_licences(c), "properties": props}
        if c["eco"] in ("pypi", "npm", "maven", "deb", "apk"):
            item["purl"] = c["ref"]
        if c.get("sha256"):
            item["hashes"] = [{"alg": "SHA-256", "content": c["sha256"]}]
        if c["copyright"]:
            item["copyright"] = "; ".join(c["copyright"])
        out.append(item)
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
            "metadata": {"component": {"type": "application" if not artefact.endswith("image")
                                       else "container", "name": artefact,
                                       "bom-ref": f"artefact:{artefact}",
                                       "description": ARTEFACTS[artefact]},
                         "properties": [{"name": "ira:generated-by",
                                         "value": "scripts/supply_chain.py render"}]},
            "components": out,
            "dependencies": [{"ref": f"artefact:{artefact}",
                              "dependsOn": [c["ref"] for c in comps]}]}


# ---------------------------------------------------------------------------------------------
# Notices
# ---------------------------------------------------------------------------------------------

def notices_text(artefact: str, comps: list[dict], datasets: dict[str, dict]) -> str:
    w = []
    title = ARTEFACTS[artefact]
    w += [f"THIRD-PARTY NOTICES: {title}", "",
          "Generated by scripts/supply_chain.py from the committed inventories",
          "(docs/security/LICENCES.json, docs/security/sbom/). Do not edit by hand.",
          "Where no copyright line is given, the package's files carry none; its licence text",
          "below applies as written. This is a record of what the artefact contains and the",
          "terms it comes under, not legal advice.", ""]
    models = [c for c in comps if c["kind"] == "model"]
    if models:
        w += ["=" * 78, "MODELS AND THE DATA THEY WERE TRAINED ON", "=" * 78, ""]
        for m in models:
            w += [m["name"], f"  File: {m['where']} (SHA-256 {m['sha256']})",
                  f"  Licence: {m['licence']}"]
            if m.get("attribution"):
                w.append(f"  Attribution: {m['attribution']}")
            for d in m.get("datasets", []):
                row = datasets[d]
                w += [f"  Trained on: {d}. Licence: {row['licence']}.",
                      "  " + re.sub(r"\*", "", row["citation"])]
                if "CC BY 4.0" in row["licence"]:
                    w.append("  The dataset is used under the Creative Commons Attribution 4.0 "
                             "International licence (https://creativecommons.org/licenses/by/"
                             "4.0/). The model is IRA CARE's own work derived from it; the "
                             "dataset's creators do not endorse IRA CARE.")
            w.append("")
    w += ["=" * 78, "THIRD-PARTY SOFTWARE", "=" * 78, ""]
    texts: set[str] = set()
    for c in comps:
        if c["kind"] == "model":
            continue
        w.append(f"{c['name']} {c['version']}".rstrip())
        if c["kind"] == "os" and c["eco"] == "deb":
            w.append(f"  Licence: {', '.join(c.get('os_licences', [])) or 'see below'}; the "
                     f"full licence and copyright are in /usr/share/doc/{c['name']}/copyright "
                     "in the image.")
        else:
            w.append(f"  Licence: {c['licence']}")
            if c["licence"] != OWN:
                texts.update(licence_ids(c["licence"]))
        if c["kind"] != "library" or "only" in c["where"]:
            w.append(f"  Where: {c['where']}")
        for line in c["copyright"]:
            w.append(f"  {line}")
        w.append("")
    w += ["=" * 78, "LICENCE TEXTS", "=" * 78, ""]
    for t in sorted(texts):
        path = LICENCE_TEXTS / f"{t}.txt"
        w += [f"----- {t} -----", ""]
        w.append(path.read_text(encoding="utf-8").rstrip() if path.exists()
                 else f"(text missing: {path.relative_to(ROOT)})")
        w.append("")
    return "\n".join(w).rstrip() + "\n"


def needed_licence_texts(comps: list[dict]) -> set[str]:
    out: set[str] = set()
    for c in comps:
        if c["kind"] == "model" or (c["kind"] == "os" and c["eco"] == "deb"):
            continue
        if c["licence"] != OWN:
            out.update(licence_ids(c["licence"]))
    return out


#: Where each image carries its notices file (each Dockerfile copies it there).
IMAGE_NOTICES_PATH = "/usr/share/doc/ira-care/THIRD_PARTY_NOTICES.txt"

#: The copy of a notices file each app bundles and shows on its licences screen. Written by
#: `render --write` and checked by `render --check` like the notices themselves.
APP_COPIES = {
    "dashboard-image": ROOT / "dashboard" / "src" / "legal" / "THIRD_PARTY_NOTICES.txt",
    "phone-app": ROOT / "mobile" / "assets" / "legal" / "THIRD_PARTY_NOTICES.json",
    "watch-wearos": WATCH / "app" / "src" / "wearos" / "assets" / "THIRD_PARTY_NOTICES.txt",
    "watch-aosp": WATCH / "app" / "src" / "aosp" / "assets" / "THIRD_PARTY_NOTICES.txt",
}


def app_copy_text(artefact: str, notices: str) -> str:
    """What the app's bundled copy holds: the notices file itself, or (for the phone app, whose
    bundler takes JSON as a module) the same text in a JSON object."""
    if APP_COPIES[artefact].suffix == ".json":
        return dumps({"artefact": artefact, "source": f"docs/legal/notices/{artefact}.txt",
                      "text": notices})
    return notices


CARRY = {
    "watch-wearos": "In the APK (assets/THIRD_PARTY_NOTICES.txt, from watch/app/src/wearos/"
                    "assets/), shown on the watch under About, Open-source licences. In the store "
                    "listing: a link to it.",
    "watch-aosp": "In the APK (assets/THIRD_PARTY_NOTICES.txt, from watch/app/src/aosp/assets/), "
                  "shown on the watch under About, Open-source licences, and beside the build "
                  "wherever it is handed over.",
    "phone-app": "Bundled in the app (mobile/assets/legal/THIRD_PARTY_NOTICES.json) and shown "
                 "under Settings, Open-source licences; beside each store build.",
    "dashboard-image": f"Inside the image ({IMAGE_NOTICES_PATH}), shown by the dashboard at "
                       "/licences (from dashboard/src/legal/), and beside any copy of the image "
                       "handed to someone else.",
    "api-image": f"Inside the image ({IMAGE_NOTICES_PATH}) and beside any copy of the image "
                 "handed to someone else. Debian packages also carry their own notices in "
                 "/usr/share/doc.",
    "backup-image": f"Inside the image ({IMAGE_NOTICES_PATH}) and beside any copy handed to "
                    "someone else. Alpine packages carry no licence texts of their own, so this "
                    "file is the notice.",
}


def index_markdown(per: dict[str, list[dict]], datasets: dict[str, dict]) -> str:
    w = ["# Third-party notices", "",
         "Generated by `scripts/supply_chain.py render` from the committed inventories. Do not "
         "edit by hand. Each distributed artefact has its own notices file, with every "
         "third-party package it contains, the copyright lines found in that package's files, "
         "the attribution for the models and training data it carries, and the full text of "
         "every licence named.", "",
         "This is a record of what the artefacts contain and under which terms, checked against "
         "the policy in [`docs/security/LICENCES.md`](../security/LICENCES.md). It is not legal "
         "advice and does not by itself make a distribution compliant: each file still has to "
         "travel with its artefact, as the table says.", "",
         "| Artefact | Notices file | SBOM | Components | Must carry the notices |",
         "| --- | --- | --- | --- | --- |"]
    for a, comps in per.items():
        w.append(f"| {ARTEFACTS[a]} | [`notices/{a}.txt`](notices/{a}.txt) | "
                 f"[`{a}.cdx.json`](../security/sbom/{a}.cdx.json) | {len(comps)} | {CARRY[a]} |")
    w += ["", "## Models and training data", "",
          "| Model | Artefacts | Licence | Attribution and training data |", "| --- | --- | --- | --- |"]
    seen: dict[str, dict] = {}
    for a, comps in per.items():
        for c in comps:
            if c["kind"] == "model":
                seen.setdefault(c["name"], {"c": c, "in": []})["in"].append(a)
    for name, x in sorted(seen.items()):
        c = x["c"]
        parts = [c["attribution"]] if c.get("attribution") else []
        parts += [f"{d} ({datasets[d]['licence']})" for d in c.get("datasets", [])]
        w.append(f"| {name} | {', '.join(x['in'])} | {c['licence']} | "
                 f"{'; '.join(parts) or '—'} |")
    w += ["", "## Dataset citations", "",
          "Every dataset whose terms carry over to a shipped model, with the citation the source "
          "asks for (from [`docs/DATASETS.md`](../DATASETS.md)):", ""]
    used = sorted({d for x in seen.values() for d in x["c"].get("datasets", [])})
    for d in used:
        w.append(f"- **{d}** ({datasets[d]['licence']}). {datasets[d]['citation']}")
    w += ["", "## Downloaded at installation, not inside our images", ""]
    for what, lic, attr in DOWNLOADED:
        w.append(f"- {what}: {lic}. {attr}")
    w += ["", "Third-party images that `deploy/docker-compose.yml` runs unchanged (Ollama, Caddy, "
          "PostgreSQL with pgvector, Redis) are pulled from their publishers by the installation and are "
          "not redistributed by IRA CARE; their own notices apply.", ""]
    return "\n".join(w)


def _shown(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def render(write: bool) -> list[str]:
    datasets = dataset_rows()
    problems: list[str] = []
    per = {a: artefact_components(a) for a in ARTEFACTS}
    for a, comps in per.items():
        for c in comps:
            for d in c.get("datasets", []):
                if d not in datasets:
                    problems.append(f"{a}: dataset {d!r} is not a row of docs/DATASETS.md")
    if problems:
        return problems
    files = {SBOM_DIR / f"{a}.cdx.json": dumps(cyclonedx(a, comps)) for a, comps in per.items()}
    notices = {a: notices_text(a, comps, datasets) for a, comps in per.items()}
    files.update({NOTICES_DIR / f"{a}.txt": text for a, text in notices.items()})
    files.update({APP_COPIES[a]: app_copy_text(a, notices[a]) for a in APP_COPIES})
    files[NOTICES_INDEX] = index_markdown(per, datasets)
    missing = sorted({t for comps in per.values() for t in needed_licence_texts(comps)
                      if not (LICENCE_TEXTS / f"{t}.txt").exists()})
    if missing:
        problems.append(f"licence texts missing under {LICENCE_TEXTS.relative_to(ROOT)}: "
                        f"{missing} (fetch with: scripts/supply_chain.py licence-texts)")
    for path, text in files.items():
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        elif not path.is_file() or path.read_text(encoding="utf-8") != text:
            problems.append(f"{_shown(path)} is out of date (run: scripts/"
                            "supply_chain.py render --write)")
    return problems


def committed_exception_problems(exceptions: list[dict]) -> list[str]:
    """Exceptions for what only the built artefacts contain must match an entry of a committed
    image inventory that needs them."""
    out = []
    invs = {a: load_json(image_inventory_path(a)) for a in IMAGES
            if image_inventory_path(a).is_file()}
    for x in exceptions:
        eco = x.get("ecosystem")
        if eco not in ("image-api", "image-dashboard", "image-backup", "native-libraries",
                       "os-linked"):
            continue
        hits = []
        for a, inv in invs.items():
            if eco.startswith("image-") and eco != f"image-{a}":
                continue
            secs = {"native-libraries": ["bundled_native_libraries"],
                    "os-linked": ["linked_os_libraries"]}.get(eco, ["python", "npm"])
            for sec in secs:
                hits += [e for e in inv[sec].values()
                         if e["name"] == x["package"] and e["licence"] == x["licence"]]
        if not hits:
            out.append(f"exception {eco}/{x['package']} ({x['licence']}) matches nothing in the "
                       "committed image inventories: remove it")
        elif all(e["status"] == "allowed" for e in hits):
            out.append(f"exception {eco}/{x['package']} is not needed: its licence is allowed")
    return out


# ---------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------

def _summary(inv: dict) -> list[str]:
    os_gpl = sorted(k for k, p in inv["os_packages"].items() if p["gpl_named_in_copyright_file"])
    lines = [f"{inv['artefact']}: {len(inv['python'])} Python, {len(inv['npm'])} npm, "
             f"{len(inv['os_packages'])} OS packages ({len(os_gpl)} whose copyright file names "
             f"a GPL licence; {len(inv['linked_os_libraries'])} linked by our components), "
             f"{len(inv['bundled_native_libraries'])} native libraries bundled in wheels, "
             f"{len(inv['model_files'])} model files"]
    for sec in ("python", "npm", "bundled_native_libraries", "linked_os_libraries"):
        ex = sorted(k for k, e in inv[sec].items() if e["status"] == "excepted")
        if ex:
            lines.append(f"  excepted ({sec}): {', '.join(ex)}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    im = sub.add_parser("image", help="scan a built image")
    im.add_argument("artefact", choices=sorted(IMAGES))
    im.add_argument("image")
    im.add_argument("--base", help="the base image, to tell its packages from ours")
    im.add_argument("--note", default="", help="recorded in the inventory (how it was built)")
    im.add_argument("--out", help="also write the fresh inventory here (a CI artifact)")
    g = im.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    ak = sub.add_parser("apk", help="the watch release APKs against the classpath")
    g = ak.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    cp = sub.add_parser("copyrights", help="copyright lines from the installed packages")
    cp.add_argument("--only", action="append",
                    choices=("python", "npm-dashboard", "npm-mobile", "gradle-watch"))
    sub.add_parser("licence-texts", help="fetch the SPDX texts the notices need")
    rd = sub.add_parser("render", help="CycloneDX SBOMs and notices from the inventories")
    g = rd.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    exceptions = lc.load_exceptions()
    problems: list[str] = []

    if a.cmd == "image":
        native, os_linked = load_native_review()
        facts = scan_image(a.image)
        base = scan_image(a.base) if a.base else None
        inv = image_inventory(a.artefact, facts, base, exceptions, native, os_linked, a.note)
        print("\n".join(_summary(inv)))
        problems += image_problems(inv)
        problems += python_lock_problems(inv, locked_python(a.artefact))
        if a.out:
            Path(a.out).write_text(dumps(inv), encoding="utf-8")
        if a.write:
            SBOM_DIR.mkdir(parents=True, exist_ok=True)
            image_inventory_path(a.artefact).write_text(dumps(inv), encoding="utf-8")
            print(f"wrote {image_inventory_path(a.artefact).relative_to(ROOT)}")
        else:
            committed = load_json(image_inventory_path(a.artefact))
            for sec in ("python", "npm"):
                new, old = set(inv[sec]), set(committed[sec])
                if new == old:
                    continue
                msg = (f"{sec} packages differ from the committed snapshot (added "
                       f"{sorted(new - old)[:8]}, removed {sorted(old - new)[:8]}); the "
                       "snapshot is refreshed with --write")
                # Where the packages come from a hashed lock, drift means the image is not the
                # one the lock and the notices describe: a failure, not a note.
                if sec == "python" and IMAGES[a.artefact].get("python_locks"):
                    problems.append(f"{a.artefact}: {msg}")
                else:
                    print(f"note: {msg}")
    elif a.cmd == "apk":
        contents, problems = apk_contents(lc.load_inventory())
        if a.write and not problems:
            APK_CONTENTS.write_text(dumps(contents), encoding="utf-8")
            print(f"wrote {APK_CONTENTS.relative_to(ROOT)}")
        elif a.check and APK_CONTENTS.is_file() and load_json(APK_CONTENTS) != contents:
            problems.append(f"{APK_CONTENTS.relative_to(ROOT)} does not match the built APKs "
                            "(regenerate with: scripts/supply_chain.py apk --write)")
        for f, c in sorted(contents.items()):
            print(f"watch {f}: {len(c['native_libraries'])} native libraries, "
                  f"{len(c['assets'])} assets, {c['library_version_files_checked']} library "
                  "version files matched to the classpath")
    elif a.cmd == "copyrights":
        data = load_json(COPYRIGHTS) if COPYRIGHTS.is_file() else {}
        inv = lc.load_inventory()
        for eco in a.only or ("python", "npm-dashboard", "npm-mobile", "gradle-watch"):
            ok, why = lc.visible(eco) if eco != "gradle-watch" else (True, "")
            if not ok:
                problems.append(f"{eco}: cannot be read here: {why}")
                continue
            data[eco] = (python_copyrights() if eco == "python" else
                         gradle_copyrights(inv) if eco == "gradle-watch" else
                         npm_copyrights(eco))
            have = sum(1 for v in data[eco].values() if v)
            print(f"{eco}: {len(data[eco])} packages, {have} with a copyright or author line")
        if not problems:
            COPYRIGHTS.parent.mkdir(parents=True, exist_ok=True)
            COPYRIGHTS.write_text(dumps(data), encoding="utf-8")
    elif a.cmd == "licence-texts":
        need = {t for art in ARTEFACTS for t in needed_licence_texts(artefact_components(art))}
        missing = fetch_licence_texts(need)
        print(f"{len(need)} licence texts needed, {len(missing)} could not be fetched: {missing}")
        problems += [f"no text for {m}" for m in missing]
    elif a.cmd == "render":
        problems += render(a.write)
        problems += committed_exception_problems(exceptions)
    for p in problems:
        print(f"problem: {p}")
    print(f"{len(problems)} problem(s)" if problems else "supply-chain check passed")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
