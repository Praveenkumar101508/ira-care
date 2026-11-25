"""Signing keys stay out of the repository (docs/security/SIGNING_KEYS.md).

Release keys are held by the founders on machines they control. These checks guard the parts a
test can see: no key material is tracked, the files that point at keys are ignored, the watch
build reads its key only from outside the repository, and every key the code reads is in the
custody document. Each check is run once on a deliberately bad input (the negative controls).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
DOC = ROOT / "docs" / "security" / "SIGNING_KEYS.md"

KEY_FILE = re.compile(r"\.(jks|keystore|pk8|p12|pfx|p8|key|pem|mobileprovision)$", re.I)
PEM_PRIVATE = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
# A fake key used to prove the log redactor hides one; it is not a key.
PEM_FIXTURES = {"supracloud-jarvis/ira/tests/runtime/test_log_redaction.py"}
MUST_IGNORE = ["watch/keystore.properties", "keystore.properties", "release.jks", "x.keystore", "platform.pk8",
               "avb.pem", "upload.p12", "AuthKey.p8"]
KEYS_THE_CODE_READS = ["IRA_WATCH_KEYSTORE", "IRA_WATCH_KEYSTORE_PASSWORD", "IRA_WATCH_KEY_ALIAS",
                       "IRA_WATCH_KEY_PASSWORD", "IRA_OTA_MANIFEST_KEY", "IRA_SECRET_KEY"]


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def key_files(paths: list[str]) -> list[str]:
    return [p for p in paths if KEY_FILE.search(p)]


def pem_private_keys(files: dict[str, str]) -> list[str]:
    return [p for p, text in files.items() if p not in PEM_FIXTURES and PEM_PRIVATE.search(text)]


def gradle_secrets_literal(text: str) -> list[str]:
    """Signing values written into the build file instead of read through secret()."""
    return re.findall(r"(?:storePassword|keyPassword|keyAlias)\s*=\s*\"[^\"]*\"", text)


def unignored(paths: list[str]) -> list[str]:
    out = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"], cwd=ROOT, input="\n".join(paths),
                         capture_output=True, text=True)
    ignored = set(out.stdout.split())
    return [p for p in paths if p not in ignored]


def test_no_key_files_are_tracked():
    assert key_files(_git("ls-files").splitlines()) == []


def test_negative_control_a_tracked_keystore_is_caught():
    assert key_files(["watch/app/release.jks", "os/keys/platform.pk8", "README.md"]) == [
        "watch/app/release.jks", "os/keys/platform.pk8"]


def test_no_private_key_block_is_tracked():
    hits = _git("grep", "-l", "-E", "BEGIN ([A-Z0-9]+ )*PRIVATE KEY", "--", ".").splitlines() if subprocess.run(
        ["git", "grep", "-q", "-E", "BEGIN ([A-Z0-9]+ )*PRIVATE KEY"], cwd=ROOT).returncode == 0 else []
    files = {p: (ROOT / p).read_text(encoding="utf-8", errors="replace") for p in hits}
    assert pem_private_keys(files) == []


def test_negative_control_a_pem_key_outside_the_fixture_is_caught():
    pem = "-----BEGIN " + "PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEI\n-----END " + "PRIVATE KEY-----"
    assert pem_private_keys({"deploy/ota.pem.txt": pem, sorted(PEM_FIXTURES)[0]: pem}) == ["deploy/ota.pem.txt"]


def test_files_that_point_at_keys_are_ignored():
    assert unignored(MUST_IGNORE) == []


def test_negative_control_an_unignored_path_is_reported():
    assert unignored(["watch/app/src/main/AndroidManifest.xml"]) == ["watch/app/src/main/AndroidManifest.xml"]


def test_the_watch_build_reads_its_key_only_from_outside_the_repository():
    gradle = (ROOT / "watch" / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    assert 'System.getenv(name) ?: keystoreProps.getProperty(name)' in gradle
    assert gradle_secrets_literal(gradle) == []


def test_negative_control_a_password_written_into_the_build_is_caught():
    assert gradle_secrets_literal('storePassword = "hunter2"\nkeyAlias = secret("A")') == ['storePassword = "hunter2"']


def test_every_key_the_code_reads_is_in_the_custody_document():
    doc = DOC.read_text(encoding="utf-8")
    assert [k for k in KEYS_THE_CODE_READS if f"`{k}`" not in doc and k not in doc] == []
    gradle = (ROOT / "watch" / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    read = set(re.findall(r'secret\("(IRA_WATCH_[A-Z_]+)"\)', gradle))
    assert read <= set(KEYS_THE_CODE_READS), read - set(KEYS_THE_CODE_READS)


def test_the_document_forbids_test_keys_for_anything_distributed():
    doc = DOC.read_text(encoding="utf-8")
    assert "test-keys" in doc and "must never reach a real person" in doc
