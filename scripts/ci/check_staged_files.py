#!/usr/bin/env python3
"""Refuse files that must never enter this public repository.

    python3 scripts/ci/check_staged_files.py          # the files staged for the next commit
    python3 scripts/ci/check_staged_files.py --all    # every tracked file (what CI can check)

The repository is public (Apache 2.0). Three kinds of file are refused here, before they are
committed, because history is never rewritten and anything pushed stays public for good:

1. Dataset content, archives and model weights: the paths and suffixes in FORBIDDEN_DIRS and
   FORBIDDEN_SUFFIXES. The datasets the models are trained or evaluated on are downloaded
   outside the repository and deleted after the run (docs/DATASETS.md); only results, and the
   weights the product ships, are committed. A file that is meant to be here is named in
   ALLOWED, with the reason.
2. Files larger than SIZE_LIMIT bytes, unless named in SIZE_ALLOWED with a ceiling.
3. Credentials: known token shapes, private key blocks, webhook URLs, and a high-entropy value
   assigned to a name that says it is a key, secret, token or password. A line carrying
   `gitleaks:allow` or `pragma: allowlist secret` is skipped, as gitleaks and detect-secrets
   skip it, so a deliberate test fixture is visible in review where it applies.

A finding is printed as path, line and rule; the value is never printed. Exit status 1 when
anything is found. The same rules run over every tracked file in
supracloud-jarvis/ira/tests/readiness/test_public_repository.py. This complements gitleaks
(scripts/ci/gitleaks.sh), which scans the whole history in CI; it does not replace it.
"""
from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Directories that hold downloaded data or private papers, wherever they appear in the tree.
FORBIDDEN_DIRS = ("datasets", "raw_data", "data/raw", "private", "confidential", "investor", "investors")

#: Suffixes of dataset files, archives, recordings and model weights.
FORBIDDEN_SUFFIXES = (
    # archives
    ".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".gz", ".bz2", ".xz", ".7z", ".rar",
    # array and tabular data
    ".csv", ".tsv", ".npz", ".npy", ".h5", ".hdf5", ".parquet", ".feather", ".arrow", ".mat", ".edf",
    ".pkl", ".pickle", ".joblib",
    # recordings
    ".wav", ".flac",
    # model weights
    ".safetensors", ".bin", ".pt", ".pth", ".ckpt", ".gguf", ".onnx", ".tflite", ".keras",
    # credentials
    ".env",
)

#: Tracked files that match a forbidden pattern on purpose, and why.
ALLOWED = {
    "watch/app/src/main/assets/fall_model.tflite":
        "fall model v3, the shipped watch model (BITS-2 and UCI HAR, both CC BY 4.0; docs/ai/MODEL_WEIGHTS.md)",
    "docs/ai/fall/fall_model_v2.tflite":
        "fall model v2 candidate, kept as evaluation evidence (BITS-2 and UCI HAR; docs/ai/fall/MODEL_CARD_V2.md)",
    "supracloud-jarvis/ira/editions/private/__init__.py":
        "source code: the 'private' edition of the product, not a private document",
}

#: Any tracked file larger than this needs a line in SIZE_ALLOWED.
SIZE_LIMIT = 1024 * 1024

#: Files allowed above SIZE_LIMIT, each with its own ceiling in bytes (about 25 % above today).
SIZE_ALLOWED = {
    "docs/film/ira_care.mp4": 24 * 1024 * 1024,        # the product film (docs/film/RECORDING.md)
    "docs/hardware/turntable.mp4": 2 * 1024 * 1024,    # the watch model turning (Blender render)
    "docs/hardware/viewer.html": 3 * 1024 * 1024,      # self-contained 3D viewer, model inlined
    "docs/hardware/viewer_blueprint.html": 3 * 1024 * 1024,
    "docs/hardware/renders/hero.png": 3 * 1024 * 1024,
    "docs/hardware/renders/front.png": 3 * 1024 * 1024,
    "docs/hardware/renders/section.png": 3 * 1024 * 1024,
    "docs/hardware/renders/side.png": 3 * 1024 * 1024,
    "docs/hardware/renders/back.png": 3 * 1024 * 1024,
    "docs/hardware/renders/wrist_dock.png": 3 * 1024 * 1024,
    "docs/hardware/renders/exploded.png": 2 * 1024 * 1024,
    "docs/screens/dashboard-person-access.phone.png": 2 * 1024 * 1024,
    "docs/screens/dashboard-person-alerts.phone.png": 2 * 1024 * 1024,
    "docs/screens/dashboard-person-timeline.phone.png": 2 * 1024 * 1024,
}

#: Example environment files are committed on purpose; their values are placeholders.
ENV_EXAMPLE = re.compile(r"(^|/)[^/]*\.env(\.[a-z0-9_-]+)*\.example$|(^|/)\.env(\.[a-z0-9_-]+)*\.example$")
#: `.env`, `.env.local`, `prod.env`, `.env.production`: real environment files.
ENV_FILE = re.compile(r"(^|/)(\.env(\.[A-Za-z0-9_-]+)*|[^/]+\.env)$")

# Secret content rules. Each is (name, pattern); the value itself is never reported.
TOKEN_RULES = (
    ("private-key-block", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("openai-style-key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{32,}\b")),
    ("stripe-live-key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}\b")),
    ("twilio-api-key", re.compile(r"\bSK[0-9a-f]{32}\b")),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{20,}")),
    ("discord-webhook", re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{20,}")),
    ("teams-webhook", re.compile(r"https://[a-z0-9-]+\.webhook\.office\.com/[A-Za-z0-9/@_-]{20,}")),
)

#: Rules whose shape is loose enough to match a readable fixture, so the match must also look
#: random: digits, upper and lower case, and not a run of plain words.
VARIED = {"openai-style-key", "huggingface-token", "slack-token"}


def _varied(token: str) -> bool:
    return (bool(re.search(r"\d", token)) and bool(re.search(r"[A-Z]", token))
            and bool(re.search(r"[a-z]", token)) and not _readable(token))


#: A name that says its value is a credential, then `=` or `:`, then a quoted literal.
ASSIGNED_SECRET = re.compile(
    r"""(?ix)
    (?<![a-z0-9_.\-])
    (?P<name>[a-z0-9_.\-]{0,40}?(?:secret|passw(?:or)?d|passphrase|api[_-]?key|access[_-]?key|auth[_-]?token
        |private[_-]?key|client[_-]?secret|bearer|token)[a-z0-9_.\-]{0,40})
    ["']?\s{0,4}[:=]\s{0,4}(?:[a-z]{1,20}\()?
    ["'](?P<value>[^"'\s]{16,200})["']
    """)

#: Content rules switched off for one file, with the reason. Keep this short.
FIXTURE_RULES = {
    # A truncated fake RSA block that proves the log redactor hides one (also PEM_FIXTURES in
    # tests/readiness/test_signing_keys.py). It is not a key.
    "supracloud-jarvis/ira/tests/runtime/test_log_redaction.py": {"private-key-block"},
}

#: Longer lines are inlined data, not hand-written assignments, and are skipped by the content rules.
MAX_LINE = 4000

#: A name that says the value is a digest, not the secret itself.
DIGEST_NAME = re.compile(r"sha\d*|hash|digest|checksum|md5|fingerprint", re.IGNORECASE)

#: Inline markers that exempt one line, shared with gitleaks and detect-secrets.
INLINE_ALLOW = ("gitleaks:allow", "pragma: allowlist secret")

#: Words that mark a literal as a placeholder or a test value rather than a credential.
PLACEHOLDER = re.compile(
    r"change[-_]?me|example|placeholder|dummy|fake|not[-_]?real|test|sample|redacted|your[-_]|xxxx|<|\$\{|\{\{",
    re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: {self.rule}"


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout


def entropy(value: str) -> float:
    """Shannon entropy in bits per character."""
    counts = Counter(value)
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def path_findings(path: str) -> list[Finding]:
    """Rules that depend on the path alone: dataset folders, data and weight suffixes, .env files."""
    if path in ALLOWED:
        return []
    found: list[Finding] = []
    low = path.lower()
    parts = low.split("/")
    for d in FORBIDDEN_DIRS:
        dparts = d.split("/")
        for i in range(len(parts) - len(dparts)):
            if parts[i:i + len(dparts)] == dparts:
                found.append(Finding(path, 0, f"inside a '{d}/' folder (datasets and private papers stay out)"))
                break
    if ENV_FILE.search(path) and not ENV_EXAMPLE.search(path):
        found.append(Finding(path, 0, "an environment file (only *.example files are committed)"))
    else:
        for suffix in FORBIDDEN_SUFFIXES:
            if low.endswith(suffix):
                found.append(Finding(path, 0, f"a '{suffix}' file (dataset, archive, recording or weights)"))
                break
    return found


def size_findings(path: str, size: int) -> list[Finding]:
    ceiling = SIZE_ALLOWED.get(path, SIZE_LIMIT)
    if size > ceiling:
        return [Finding(path, 0, f"{size} bytes, over the {ceiling}-byte limit (SIZE_ALLOWED in this script)")]
    return []


def _readable(value: str) -> bool:
    """Three or more plain words joined by punctuation: a sentence-like fixture, not a random key."""
    words = [w for w in re.split(r"[-_./:\[\]]+", value) if re.fullmatch(r"[A-Za-z]{3,}", w)]
    return len(words) >= 3


def _looks_secret(value: str) -> bool:
    # An Expo push token is an address for one phone, not a key; only its inside can be random.
    value = re.sub(r"^Expo(?:nent)?PushToken\[(.*)\]$", r"\1", value)
    if len(value) < 16 or PLACEHOLDER.search(value) or _readable(value):
        return False
    if re.fullmatch(r"[0-9a-fA-F]+", value):
        # Hex: digests and commit ids are everywhere here; only a long, varied one is a key.
        return len(value) >= 32 and entropy(value) >= 3.5 and not re.fullmatch(r"(.)\1+", value)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", value) and not re.search(r"\d", value):
        return False  # an identifier such as settings.webhook_secret or an env var name
    return entropy(value) >= 4.0


def content_findings(path: str, text: str) -> list[Finding]:
    """Credential rules over the text of one file."""
    found: list[Finding] = []
    for n, line in enumerate(text.splitlines(), 1):
        if any(marker in line for marker in INLINE_ALLOW):
            continue
        if len(line) > MAX_LINE:
            continue  # minified or inlined data (a base64 model, a lockfile line); gitleaks covers it
        for name, rx in TOKEN_RULES:
            m = rx.search(line)
            if m and name not in FIXTURE_RULES.get(path, ()) and (name not in VARIED or _varied(m.group(0))):
                found.append(Finding(path, n, name))
        for m in ASSIGNED_SECRET.finditer(line):
            if DIGEST_NAME.search(m.group("name")):
                continue  # a digest of a secret is published on purpose (published_credentials.py)
            if _looks_secret(m.group("value")):
                found.append(Finding(path, n, f"high-entropy value assigned to '{m.group('name')}'"))
    return found


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


def scan(paths: list[str], *, size_of, read) -> list[Finding]:
    found: list[Finding] = []
    for path in paths:
        found += path_findings(path)
        size = size_of(path)
        found += size_findings(path, size)
        if size <= SIZE_LIMIT * 4:
            data = read(path)
            if not _is_binary(data):
                found += content_findings(path, data.decode("utf-8", errors="replace"))
    return found


def staged_paths(root: Path = ROOT) -> list[str]:
    out = _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    return [p for p in out.decode().split("\0") if p]


def tracked_paths(root: Path = ROOT) -> list[str]:
    return [p for p in _git(root, "ls-files", "-z").decode().split("\0") if p]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--all", action="store_true", help="check every tracked file, not only staged ones")
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository to check (default: this one)")
    args = parser.parse_args(argv)
    root = args.root
    if args.all:
        paths = tracked_paths(root)

        def size_of(p: str) -> int:
            return (root / p).stat().st_size if (root / p).is_file() else 0

        def read(p: str) -> bytes:
            return (root / p).read_bytes() if (root / p).is_file() else b""
    else:
        paths = staged_paths(root)

        def size_of(p: str) -> int:
            return int(_git(root, "cat-file", "-s", f":{p}").strip())

        def read(p: str) -> bytes:
            return _git(root, "cat-file", "blob", f":{p}")
    found = scan(paths, size_of=size_of, read=read)
    for f in found:
        print(f)
    what = "tracked" if args.all else "staged"
    if found:
        print(f"{len(found)} finding(s) in {len(paths)} {what} file(s). Values are not printed. "
              "If a file is meant to be here, add it to ALLOWED or SIZE_ALLOWED with the reason.",
              file=sys.stderr)
        return 1
    print(f"{len(paths)} {what} file(s) checked: nothing found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

