"""The licences of what ships, inventoried and checked against a written policy (K.d).

    python3 scripts/licence_check.py                         # summary of the committed inventory
    python3 scripts/licence_check.py --check                 # exit 1 on a forbidden licence or a
                                                             # stale inventory
    python3 scripts/licence_check.py --check --only npm-dashboard
    python3 scripts/licence_check.py --write --only npm-mobile

Four ecosystems are covered:

  python          the packages pinned in supracloud-jarvis/ira/requirements.lock, read from the
                  installed distribution metadata of the interpreter running this script
                  (License-Expression, then a recognisable License field, then the licence
                  classifiers). Run it with an interpreter that has the lock installed.
  npm-dashboard   the production dependency tree of dashboard/ (npm ls --omit=dev --all)
  npm-mobile      the production dependency tree of mobile/, read the same way
  gradle-watch    the release runtime classpath of both watch flavours (wearos, aosp), resolved
                  by Gradle through scripts/ci/gradle_runtime_deps.init.gradle; each module's
                  licence is read from its POM (following <parent> POMs); the init script has
                  Gradle fetch every POM and reports its path, so the cache does not matter.
                  Modules with POM packaging carry no code (a bill of materials, or a Kotlin
                  Multiplatform root whose code is in the -jvm module listed beside it); they
                  are listed apart under "no_code_modules".

Every licence is classified by `classify`: permissive licences are allowed; copyleft (GPL,
AGPL, LGPL and the like), source-available, non-commercial and unknown or missing licences are
forbidden unless docs/security/licence_exceptions.json lists the package with a written reason.
An SPDX "OR" is allowed when any option is allowed (we take that option); "AND" needs every
part allowed. The policy and what is not covered are in docs/security/LICENCES.md.

An ecosystem is "visible" when this run can read it: the lock is installed at its locked
versions (python), or node_modules is installed (npm). `--check` regenerates every visible
ecosystem and fails if the committed docs/security/LICENCES.json differs. An ecosystem that is
not visible is skipped with a note, except that the Python section is still compared with the
lock's names and versions. The Gradle ecosystem is visible when the Android SDK is configured
(ANDROID_HOME, ANDROID_SDK_ROOT or watch/local.properties); extra Gradle arguments (an init
script with a repository mirror, say) can be passed in LICENCE_CHECK_GRADLE_ARGS. Naming an ecosystem with --only makes it required: not being able
to see it is then a failure, not a skip.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import xml.etree.ElementTree as ET
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs" / "security" / "LICENCES.json"
EXCEPTIONS = ROOT / "docs" / "security" / "licence_exceptions.json"
LOCK = ROOT / "supracloud-jarvis" / "ira" / "requirements.lock"
NPM_PROJECTS = {"npm-dashboard": ROOT / "dashboard", "npm-mobile": ROOT / "mobile"}
#: Gradle ecosystems: project directory, module, {flavour: release runtime configuration}.
GRADLE_PROJECTS = {
    "gradle-watch": (ROOT / "watch", ":app", {"wearos": "wearosReleaseRuntimeClasspath",
                                              "aosp": "aospReleaseRuntimeClasspath"}),
}
GRADLE_INIT = ROOT / "scripts" / "ci" / "gradle_runtime_deps.init.gradle"
ECOSYSTEMS = ("python", "npm-dashboard", "npm-mobile", "gradle-watch")
#: Exceptions may also name what scripts/supply_chain.py finds inside the built artefacts:
#: packages only in an image, native libraries bundled in wheels, and OS libraries our own
#: code links.
EXCEPTION_ECOSYSTEMS = ECOSYSTEMS + ("image-api", "image-dashboard", "image-backup",
                                     "native-libraries", "os-linked")
NOASSERTION = "NOASSERTION"  # the SPDX word for "no licence information"

# ---------------------------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------------------------

#: Permissive licences, allowed in shipped code. Compared case-insensitively. "BSD" on its own
#: (a bare "BSD" or the "BSD License" classifier) names no clause count, but every BSD variant
#: is permissive, so it is allowed. MPL-2.0 is file-level copyleft: allowed while we do not
#: modify the files. CC-BY is an attribution licence (used for data such as caniuse-lite).
ALLOWED = frozenset(s.lower() for s in (
    "MIT", "MIT-0", "MIT-CMU", "X11", "ISC", "0BSD", "BSD", "BSD-2-Clause", "BSD-3-Clause",
    "BSD-3-Clause-Clear", "Apache-2.0", "PSF-2.0", "Python-2.0", "Python-2.0.1", "CNRI-Python",
    "MPL-2.0", "CC0-1.0", "Unlicense", "BlueOak-1.0.0", "Zlib", "BSL-1.0", "CC-BY-3.0",
    "CC-BY-4.0", "HPND",
    # Permissive licences of native libraries found inside the built artefacts (wheels' bundled
    # libraries, OS libraries our code links): each keeps a notice, none adds copyleft.
    "FTL", "IJG", "libtiff", "libpng-2.0", "PostgreSQL", "OLDAP-2.8",
    "BSD-Attribution-HPND-disclaimer", "curl", "blessing", "bzip2-1.0.6", "MIT-open-group",
    "ICU", "Unicode-3.0",
))

#: Licence exceptions (the SPDX "WITH" clause) that make a GPL id acceptable to link: the GCC
#: Runtime Library Exception lets any program ship with GCC's runtime (libgcc, libstdc++,
#: libgfortran, libgomp) under its own terms. Other exceptions (Classpath, say) are reviewed one
#: by one.
ALLOWED_WITH = {"gcc-exception-3.1": ("GPL-3.0-or-later", "GPL-3.0-only", "GPL-3.0",
                                      "GPL-3.0+")}

#: Forbidden families, checked in order; the first match gives the reason.
FORBIDDEN = (
    (re.compile(r"^AGPL", re.IGNORECASE), "network copyleft (AGPL)"),
    (re.compile(r"^LGPL", re.IGNORECASE), "weak copyleft (LGPL): needs a reviewed exception"),
    (re.compile(r"^GPL", re.IGNORECASE), "strong copyleft (GPL)"),
    (re.compile(r"^SSPL", re.IGNORECASE), "SSPL (not open source)"),
    (re.compile(r"(^|-)NC(-|$)|non-?commercial", re.IGNORECASE), "non-commercial"),
    (re.compile(r"^(BUSL|Elastic|Commons-Clause|PolyForm)", re.IGNORECASE),
     "source-available, not open source"),
    (re.compile(r"^(EUPL|CDDL|EPL|OSL|CPAL|CECILL|MS-RL|RPL|Sleepycat)", re.IGNORECASE),
     "copyleft"),
)

#: Free-text licence names (the legacy License field, npm's old "licenses" objects) and trove
#: classifiers, mapped to SPDX. Anything not here is parsed as an SPDX expression, and if that
#: fails it is unknown (forbidden). Keys are lower case.
ALIASES = {
    "mit": "MIT", "mit license": "MIT", "the mit license": "MIT", "mit licence": "MIT",
    "apache 2.0": "Apache-2.0", "apache-2.0 license": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0", "apache 2.0 license": "Apache-2.0",
    "apache software license": "Apache-2.0", "apache license version 2.0": "Apache-2.0",
    "bsd": "BSD", "bsd license": "BSD", "modified bsd license": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause", "3-clause bsd license": "BSD-3-Clause",
    "bsd 3-clause license": "BSD-3-Clause", "bsd-3-clause license": "BSD-3-Clause",
    "simplified bsd license": "BSD-2-Clause", "bsd 2-clause license": "BSD-2-Clause",
    "isc license": "ISC", "isc license (iscl)": "ISC",
    "psf": "PSF-2.0", "python software foundation license": "PSF-2.0",
    "the unlicense (unlicense)": "Unlicense", "the unlicense": "Unlicense",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "gnu lesser general public license v3 (lgplv3)": "LGPL-3.0",
    "gnu lesser general public license v3 or later (lgplv3+)": "LGPL-3.0-or-later",
    "gnu lesser general public license v2 or later (lgplv2+)": "LGPL-2.0-or-later",
    "gnu lesser general public license v2 (lgplv2)": "LGPL-2.0",
    "gnu general public license v3 (gplv3)": "GPL-3.0",
    "gnu general public license v3 or later (gplv3+)": "GPL-3.0-or-later",
    "gnu general public license v2 (gplv2)": "GPL-2.0",
    "gnu general public license v2 or later (gplv2+)": "GPL-2.0-or-later",
    "gnu affero general public license v3": "AGPL-3.0",
    "gnu affero general public license v3 or later (agplv3+)": "AGPL-3.0-or-later",
    "server side public license (sspl)": "SSPL-1.0",
    # Maven POM <license><name> spellings
    "the apache software license, version 2.0": "Apache-2.0",
    "the apache license, version 2.0": "Apache-2.0",
    "apache license, version 2.0.": "Apache-2.0",
    "apache 2": "Apache-2.0", "apache-2.0": "Apache-2.0", "apache license 2": "Apache-2.0",
    "apache software license - version 2.0": "Apache-2.0",
    "the mit license (mit)": "MIT", "mit-0": "MIT-0",
    "bsd-3-clause": "BSD-3-Clause", "bsd-2-clause": "BSD-2-Clause",
    "the bsd license": "BSD", "new bsd license (3-clause)": "BSD-3-Clause",
    "eclipse public license 1.0": "EPL-1.0", "eclipse public license - v 1.0": "EPL-1.0",
    "eclipse public license v2.0": "EPL-2.0", "eclipse public license - v 2.0": "EPL-2.0",
    "gnu general public license, version 2 with the classpath exception":
        "GPL-2.0-only WITH Classpath-exception-2.0",
}

#: Licence URLs, for POMs whose <name> is not recognised. Compared without scheme or "www.".
LICENCE_URLS = {
    "apache.org/licenses/license-2.0": "Apache-2.0",
    "apache.org/licenses/license-2.0.txt": "Apache-2.0",
    "apache.org/licenses/license-2.0.html": "Apache-2.0",
    "opensource.org/licenses/mit": "MIT", "opensource.org/licenses/mit-license.php": "MIT",
    "opensource.org/licenses/bsd-3-clause": "BSD-3-Clause",
    "opensource.org/licenses/bsd-2-clause": "BSD-2-Clause",
    "opensource.org/licenses/apache-2.0": "Apache-2.0",
}


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    licence: str  # the normalised SPDX expression, or the declaration verbatim when unknown
    reason: str   # empty when allowed


_TOKEN = re.compile(r"\s*(\(|\)|[^\s()]+)")


def _id_reason(ident: str) -> str:
    """Empty when the licence id is allowed; otherwise why it is not."""
    if ident.lower().rstrip("+") in ALLOWED:
        return ""
    for pattern, why in FORBIDDEN:
        if pattern.search(ident):
            return f"{ident}: {why}"
    return f"{ident}: not on the allowed list"


class _Parser:
    """SPDX licence expressions: OR binds loosest, then AND, then WITH; brackets group."""

    def __init__(self, text: str):
        self.tokens = _TOKEN.findall(text)
        self.i = 0

    def _peek(self) -> str | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _take(self) -> str:
        if self.i >= len(self.tokens):
            raise ValueError("expression ends early")
        self.i += 1
        return self.tokens[self.i - 1]

    def parse(self) -> list[str]:
        problems = self._or()
        if self._peek() is not None:
            raise ValueError(f"unexpected {self._peek()!r}")
        return problems

    def _or(self) -> list[str]:
        options = [self._and()]
        while (self._peek() or "").upper() == "OR":
            self._take()
            options.append(self._and())
        return [] if any(not p for p in options) else [p for o in options for p in o]

    def _and(self) -> list[str]:
        parts = [self._atom()]
        while (self._peek() or "").upper() == "AND":
            self._take()
            parts.append(self._atom())
        return [p for part in parts for p in part]

    def _atom(self) -> list[str]:
        tok = self._take()
        if tok == "(":
            inner = self._or()
            if self._take() != ")":
                raise ValueError("unbalanced brackets")
            return inner
        if tok == ")" or tok.upper() in ("AND", "OR", "WITH"):
            raise ValueError(f"unexpected {tok!r}")
        if (self._peek() or "").upper() == "WITH":
            self._take()
            exception = self._take()
            if exception in ("(", ")"):
                raise ValueError("WITH needs an exception id")
            if tok in ALLOWED_WITH.get(exception.lower(), ()):
                return []
        reason = _id_reason(tok)
        return [reason] if reason else []


def normalise(declared: str | None) -> str | None:
    """The SPDX expression a declaration means, or None when it cannot be read as one."""
    text = " ".join((declared or "").split())
    if not text:
        return None
    if text.lower() in ALIASES:
        return ALIASES[text.lower()]
    try:
        _Parser(text).parse()
    except ValueError:
        return None
    return text


def classify(declared: str | None) -> Verdict:
    """Apply the policy to one declared licence (an SPDX expression or a known free-text name)."""
    text = " ".join((declared or "").split())
    if not text or text.upper() in ("UNKNOWN", NOASSERTION, "UNLICENSED", "NONE"):
        return Verdict(False, text or NOASSERTION, "no licence declared (unknown is forbidden)")
    expr = normalise(text)
    if expr is None:
        return Verdict(False, text, f"{text!r} is not a recognised licence or SPDX expression")
    problems = _Parser(expr).parse()
    return Verdict(not problems, expr, "; ".join(dict.fromkeys(problems)))


# ---------------------------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------------------------

def load_exceptions(path: Path | None = None) -> list[dict]:
    path = path or EXCEPTIONS
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["exceptions"]


def exception_problems(exceptions: list[dict]) -> list[str]:
    """Every exception names an ecosystem, a package and a licence, and gives a real reason."""
    out = []
    for e in exceptions:
        label = f"exception {e.get('ecosystem')}/{e.get('package')}"
        if set(e) != {"ecosystem", "package", "licence", "reason"}:
            out.append(f"{label}: fields must be exactly ecosystem, package, licence, reason")
            continue
        if e["ecosystem"] not in EXCEPTION_ECOSYSTEMS:
            out.append(f"{label}: unknown ecosystem")
        if len(" ".join(str(e["reason"]).split())) < 40:
            out.append(f"{label}: the reason must say why in a sentence, not a word")
    return out


def _excepted(ecosystem: str, name: str, licence: str, exceptions: list[dict]) -> bool:
    return any(e.get("ecosystem") == ecosystem and e.get("package") == name
               and e.get("licence") == licence for e in exceptions)


def entry(ecosystem: str, name: str, version: str, declared: str, source: str,
          exceptions: list[dict]) -> dict:
    v = classify(declared)
    if v.allowed:
        status = "allowed"
    elif _excepted(ecosystem, name, v.licence, exceptions):
        status = "excepted"
    else:
        status = "forbidden"
    out = {"name": name, "version": version, "declared": declared, "source": source,
           "licence": v.licence, "status": status}
    if v.reason:
        out["reason"] = v.reason
    return out


# ---------------------------------------------------------------------------------------------
# Python: the lock, read from installed distribution metadata
# ---------------------------------------------------------------------------------------------

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;#\\]+)")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked(lock: Path | None = None) -> dict[str, str]:
    """{canonical name: version} for every pin in the lock."""
    out = {}
    for line in (lock or LOCK).read_text(encoding="utf-8").splitlines():
        m = _PIN.match(line)
        if m:
            out[canonical(m.group(1))] = m.group(2)
    return out


def python_declared(meta) -> tuple[str, str]:
    """(declared licence, which field it came from) for one distribution's metadata."""
    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return expression, "License-Expression"
    field = " ".join((meta.get("License") or "").split())
    short = field if len(field) <= 100 and "\n" not in (meta.get("License") or "").strip() else ""
    if short and normalise(short) is not None:
        return short, "License"
    trove = sorted({c.split("::")[-1].strip() for c in (meta.get_all("Classifier") or [])
                    if c.startswith("License ::")} - {"OSI Approved"})
    if trove:
        mapped = [ALIASES.get(c.lower(), c) for c in trove]
        joined = mapped[0] if len(mapped) == 1 else " AND ".join(
            f"({m})" if " " in m else m for m in mapped)
        return joined, "Classifier"
    return short, "License" if short else "none"


def python_visible(pins: dict[str, str]) -> tuple[bool, str]:
    from importlib import metadata
    wrong = []
    for name, version in sorted(pins.items()):
        try:
            have = metadata.version(name)
        except metadata.PackageNotFoundError:
            have = None
        if have != version:
            wrong.append(f"{name} {version} (installed: {have or 'none'})")
    if wrong:
        return False, (f"{len(wrong)} of {len(pins)} locked packages are not installed at their "
                       f"locked version in {sys.executable}, e.g. {', '.join(wrong[:3])}")
    return True, ""


def python_section(exceptions: list[dict]) -> dict:
    from importlib import metadata
    packages = {}
    for name, version in sorted(locked().items()):
        declared, source = python_declared(metadata.distribution(name).metadata)
        packages[f"{name}@{version}"] = entry("python", name, version, declared, source, exceptions)
    return {"source": "supracloud-jarvis/ira/requirements.lock, installed distribution metadata",
            "packages": packages}


# ---------------------------------------------------------------------------------------------
# npm: the production dependency tree, read from each package's own package.json
# ---------------------------------------------------------------------------------------------

def npm_visible(project: Path) -> tuple[bool, str]:
    if not (project / "node_modules").is_dir():
        return False, f"{project.relative_to(ROOT)}/node_modules is not installed (run npm ci)"
    return True, ""


def npm_declared(manifest: dict) -> tuple[str, str]:
    lic = manifest.get("license")
    if isinstance(lic, dict):
        lic = lic.get("type")
    if isinstance(lic, str) and lic.strip():
        return lic.strip(), "package.json license"
    old = manifest.get("licenses")
    if isinstance(old, str) and old.strip():  # an old, non-standard spelling of "license"
        return old.strip(), "package.json licenses"
    if isinstance(old, list) and old:
        names = [x.get("type") if isinstance(x, dict) else x for x in old]
        names = [ALIASES.get(str(n).lower(), str(n)) for n in names if n]
        if names:
            return (names[0] if len(names) == 1 else "(" + " OR ".join(names) + ")",
                    "package.json licenses")
    return "", "none"


def npm_tree(project: Path) -> dict:
    run = subprocess.run(["npm", "ls", "--omit=dev", "--all", "--json", "--long"], cwd=project,
                         capture_output=True, text=True, timeout=600, check=False)
    try:
        tree = json.loads(run.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"npm ls in {project} gave no JSON: {run.stderr[-400:]}") from exc
    missing = [p for p in tree.get("problems", []) if p.startswith("missing:")]
    if missing:
        raise RuntimeError(f"npm ls in {project} reports missing packages: {missing[:3]}")
    return tree


def npm_section(ecosystem: str, exceptions: list[dict]) -> dict:
    project = NPM_PROJECTS[ecosystem]
    packages: dict[str, dict] = {}

    def walk(node: dict) -> None:
        for child in (node.get("dependencies") or {}).values():
            # extraneous: on disk but not reachable from the production tree (dev-only).
            if child.get("extraneous") or child.get("missing"):
                continue
            path = child.get("path")
            if path:  # deduplicated references carry no path; the full node appears elsewhere
                manifest = json.loads((Path(path) / "package.json").read_text(encoding="utf-8"))
                name, version = manifest["name"], manifest["version"]
                declared, source = npm_declared(manifest)
                packages[f"{name}@{version}"] = entry(ecosystem, name, version, declared, source,
                                                      exceptions)
            walk(child)

    walk(npm_tree(project))
    return {"source": f"{project.relative_to(ROOT)}: npm ls --omit=dev --all, each package.json",
            "packages": dict(sorted(packages.items()))}


# ---------------------------------------------------------------------------------------------
# Gradle: the release runtime classpath, read from each module's POM as Gradle resolves it
# ---------------------------------------------------------------------------------------------

_POM_NS = "{http://maven.apache.org/POM/4.0.0}"


def gradle_user_home() -> Path:
    return Path(os.environ.get("GRADLE_USER_HOME") or Path.home() / ".gradle")


def gradle_visible(project: Path) -> tuple[bool, str]:
    if not (project / "gradlew").is_file():
        return False, f"{project.relative_to(ROOT)}/gradlew is missing"
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not sdk and not (project / "local.properties").is_file():
        return False, "the Android SDK is not configured (ANDROID_HOME or local.properties)"
    return True, ""


def gradle_modules(ecosystem: str) -> tuple[dict[str, list[str]], dict[str, str]]:
    """({flavour: ["group:module:version", ...]}, {"group:module:version": POM path}) as Gradle
    resolves the release runtime classpath, with the POM of every module and of every parent
    POM fetched through Gradle itself. The init script fails rather than print a partial list.
    """
    project, module, configs = GRADLE_PROJECTS[ecosystem]
    extra = shlex.split(os.environ.get("LICENCE_CHECK_GRADLE_ARGS", ""))
    cmd = ["./gradlew", "-q", "--console=plain", "-I", str(GRADLE_INIT), *extra,
           f"{module}:iraRuntimeDeps", "-PiraConfigurations=" + ",".join(configs.values())]
    run = subprocess.run(cmd, cwd=project, capture_output=True, text=True, timeout=1800,
                         check=False)
    line = next((x for x in run.stdout.splitlines() if x.startswith("IRA-RUNTIME-DEPS ")), None)
    if run.returncode != 0 or line is None:
        raise RuntimeError(f"gradle in {project} failed: {(run.stderr or run.stdout)[-800:]}")
    resolved = json.loads(line.split(" ", 1)[1])
    return ({flavour: resolved["configurations"][conf] for flavour, conf in configs.items()},
            resolved["poms"])


def pom_path(group: str, module: str, version: str, home: Path | None = None) -> Path | None:
    """A POM already in the local Gradle cache, if any. Only the copyright collector uses this
    (scripts/supply_chain.py copyrights); the licence check uses the POMs Gradle resolves,
    because whether a POM is cached depends on the machine."""
    base = (home or gradle_user_home()) / "caches" / "modules-2" / "files-2.1"
    hits = sorted((base / group / module / version).glob(f"*/{module}-{version}.pom"))
    return hits[0] if hits else None


def _pom_text(el, tag: str) -> str:
    found = el.find(_POM_NS + tag) if el is not None else None
    if found is None:
        found = el.find(tag) if el is not None else None
    return " ".join((found.text or "").split()) if found is not None else ""


def _pom_root(path: Path):
    return ET.parse(path).getroot()


def _maven_licence(name: str, url: str) -> str:
    key = " ".join(name.split()).lower()
    if key in ALIASES:
        return ALIASES[key]
    u = re.sub(r"^https?://(www\.)?", "", url.strip().lower()).rstrip("/")
    if u in LICENCE_URLS:
        return LICENCE_URLS[u]
    return name.strip() or url  # an SPDX id already ("Apache-2.0"), or unknown and forbidden


def pom_declared(group: str, module: str, version: str,
                 poms: dict[str, str]) -> tuple[str, str, str]:
    """(declared licence, where it came from, POM packaging) for one module. Follows <parent>
    when a POM names no licence, as Maven does."""
    path = poms.get(f"{group}:{module}:{version}")
    if path is None:
        return "", "no POM resolved", "unknown"
    root = _pom_root(path)
    packaging = _pom_text(root, "packaging") or "jar"
    coord = f"{group}:{module}:{version}"
    for _ in range(8):
        lic = root.find(_POM_NS + "licenses")
        if lic is None:
            lic = root.find("licenses")
        names = []
        for el in list(lic) if lic is not None else []:
            mapped = _maven_licence(_pom_text(el, "name"), _pom_text(el, "url"))
            if mapped and mapped not in names:
                names.append(mapped)
        if names:
            declared = names[0] if len(names) == 1 else " AND ".join(
                f"({n})" if " " in n else n for n in names)
            source = "POM" if coord == f"{group}:{module}:{version}" else f"parent POM {coord}"
            return declared, source, packaging
        parent = root.find(_POM_NS + "parent")
        if parent is None:
            parent = root.find("parent")
        if parent is None:
            break
        coord = ":".join(_pom_text(parent, t) for t in ("groupId", "artifactId", "version"))
        ppath = poms.get(coord)
        if ppath is None:
            return "", f"parent POM {coord} not resolved", packaging
        root = _pom_root(ppath)
    return "", "POM names no licence", packaging


def gradle_section(ecosystem: str, exceptions: list[dict]) -> dict:
    project, module, configs = GRADLE_PROJECTS[ecosystem]
    by_flavour, poms = gradle_modules(ecosystem)
    packages: dict[str, dict] = {}
    no_code: set[str] = set()
    flavours: dict[str, list[str]] = {}
    for flavour, coords in sorted(by_flavour.items()):
        keys = []
        for coord in coords:
            group, name, version = coord.split(":")
            declared, source, packaging = pom_declared(group, name, version, poms)
            if packaging == "pom":  # a bill of materials or a multiplatform root: no code
                no_code.add(f"{group}:{name}@{version}")
                continue
            key = f"{group}:{name}@{version}"
            packages[key] = entry(ecosystem, f"{group}:{name}", version, declared, source,
                                  exceptions)
            keys.append(key)
        flavours[flavour] = sorted(keys)
    return {"source": (f"{project.relative_to(ROOT)} {module}: "
                       + ", ".join(configs.values())
                       + " (Gradle), each module's POM"),
            "flavours": flavours, "no_code_modules": sorted(no_code),
            "packages": dict(sorted(packages.items()))}


# ---------------------------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------------------------

def visible(ecosystem: str) -> tuple[bool, str]:
    if ecosystem == "python":
        return python_visible(locked())
    if ecosystem in GRADLE_PROJECTS:
        return gradle_visible(GRADLE_PROJECTS[ecosystem][0])
    return npm_visible(NPM_PROJECTS[ecosystem])


def section(ecosystem: str, exceptions: list[dict]) -> dict:
    if ecosystem == "python":
        return python_section(exceptions)
    if ecosystem in GRADLE_PROJECTS:
        return gradle_section(ecosystem, exceptions)
    return npm_section(ecosystem, exceptions)


def dumps(inventory: dict) -> str:
    return json.dumps(inventory, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def load_inventory(path: Path | None = None) -> dict:
    path = path or INVENTORY
    if not path.is_file():
        return {"generated_by": "scripts/licence_check.py", "ecosystems": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def counts(sec: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in sec["packages"].values():
        out[e["status"]] = out.get(e["status"], 0) + 1
    return out


def policy_problems(inventory: dict, exceptions: list[dict],
                    only: tuple[str, ...] = ECOSYSTEMS) -> list[str]:
    """Forbidden entries, entries whose recorded verdict no longer follows from the policy, and
    exceptions that are malformed or no longer match a package in the inventory."""
    out = list(exception_problems(exceptions))
    ecos = inventory.get("ecosystems", {})
    for eco in only:
        for key, e in ecos.get(eco, {}).get("packages", {}).items():
            expected = entry(eco, e["name"], e["version"], e["declared"], e["source"], exceptions)
            if e != expected:
                out.append(f"{eco} {key}: recorded verdict does not follow from the policy "
                           f"(recorded {e.get('status')}, policy says {expected['status']})")
            if expected["status"] == "forbidden":
                out.append(f"{eco} {key}: FORBIDDEN licence {expected['licence']!r}: "
                           f"{expected.get('reason', '')}")
    for x in exceptions:
        if x.get("ecosystem") not in only or x.get("ecosystem") not in ecos:
            continue
        hits = [e for e in ecos[x["ecosystem"]]["packages"].values()
                if e["name"] == x.get("package") and e["licence"] == x.get("licence")]
        if not hits:
            out.append(f"exception {x['ecosystem']}/{x.get('package')} ({x.get('licence')}) "
                       "matches no package in the inventory: remove it")
        elif all(e["status"] == "allowed" for e in hits):
            out.append(f"exception {x['ecosystem']}/{x['package']} is not needed: its licence is "
                       "allowed")
    return out


def lock_problems(inventory: dict) -> list[str]:
    """The Python section names exactly the lock's packages at the lock's versions."""
    pins = {f"{n}@{v}" for n, v in locked().items()}
    have = set(inventory.get("ecosystems", {}).get("python", {}).get("packages", {}))
    out = []
    if pins - have:
        out.append(f"python: in the lock but not the inventory: {sorted(pins - have)[:8]}")
    if have - pins:
        out.append(f"python: in the inventory but not the lock: {sorted(have - pins)[:8]}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail on a forbidden licence or a "
                      "stale inventory")
    mode.add_argument("--write", action="store_true", help=f"regenerate {INVENTORY.name}")
    ap.add_argument("--only", action="append", choices=ECOSYSTEMS,
                    help="limit to this ecosystem and require it to be visible (repeatable)")
    a = ap.parse_args(argv)
    chosen = tuple(a.only) if a.only else ECOSYSTEMS
    required = bool(a.only)
    exceptions = load_exceptions()
    inventory = load_inventory()
    problems: list[str] = []

    if a.write or a.check:
        for eco in chosen:
            ok, why = visible(eco)
            if not ok:
                if required or a.write:
                    problems.append(f"{eco}: cannot be read here: {why}")
                else:
                    print(f"{eco}: skipped, not visible here: {why}")
                continue
            fresh = section(eco, exceptions)
            committed = inventory.get("ecosystems", {}).get(eco)
            if a.write:
                inventory.setdefault("ecosystems", {})[eco] = fresh
            elif fresh != committed:
                old = set((committed or {}).get("packages", {}))
                new = set(fresh["packages"])
                changed = sorted(k for k in old & new
                                 if committed["packages"][k] != fresh["packages"][k])
                problems.append(
                    f"{eco}: {INVENTORY.relative_to(ROOT)} is out of date (added "
                    f"{sorted(new - old)[:5]}, removed {sorted(old - new)[:5]}, changed "
                    f"{changed[:5]}). Regenerate with --write --only {eco}.")
        if a.write and not problems:
            inventory["generated_by"] = "scripts/licence_check.py"
            inventory["policy"] = "docs/security/LICENCES.md"
            INVENTORY.write_text(dumps(inventory), encoding="utf-8")
            print(f"wrote {INVENTORY.relative_to(ROOT)}")

    if "python" in chosen and "python" in inventory.get("ecosystems", {}):
        problems += lock_problems(inventory)
    problems += policy_problems(inventory, exceptions, chosen)

    for eco in chosen:
        sec = inventory.get("ecosystems", {}).get(eco)
        if sec:
            parts = ", ".join(f"{k} {v}" for k, v in sorted(counts(sec).items()))
            print(f"{eco}: {len(sec['packages'])} packages ({parts})")
        else:
            print(f"{eco}: not in the inventory")
    for p in problems:
        print(f"problem: {p}")
    print(f"{len(problems)} problem(s)" if problems else "licence check passed")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
