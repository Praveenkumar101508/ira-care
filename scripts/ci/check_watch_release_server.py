#!/usr/bin/env python3
"""The built watch APKs: release keeps its configured server and refuses cleartext; only the
debug (test) build can be given a test server's address.

    python3 scripts/ci/check_watch_release_server.py            # after assemble{Wearos,Aosp}{Release,Debug}

For each release APK:
  * the merged manifest has no android:usesCleartextTraffic and no android:networkSecurityConfig,
    so Android's default for targetSdk 28 and later applies: no cleartext to any host;
  * the code carries the configured address (IRA_CARE_API_BASE_URL, default
    https://care.example.invalid), which is https;
  * the code carries neither the emulator's development address (10.0.2.2) nor the test
    server's stored-address preferences (care_test_server).
For each debug APK, as the check's own positive control: the test server preferences are there.

The address strings are compile-time constants, so they stay readable in a minified build's dex.
Requires aapt2 from the Android SDK (ANDROID_HOME or ANDROID_SDK_ROOT).
"""
from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APKS = ROOT / "watch" / "app" / "build" / "outputs" / "apk"
DEFAULT_RELEASE_URL = "https://care.example.invalid"
EMULATOR_URL = "10.0.2.2"
TEST_SERVER_MARKER = "care_test_server"
CLEARTEXT_ATTRIBUTES = ("usesCleartextTraffic", "networkSecurityConfig")


def manifest_problems(xmltree: str) -> list[str]:
    """Problems in `aapt2 dump xmltree --file AndroidManifest.xml` output for a release APK."""
    return [f"manifest sets android:{a}" for a in CLEARTEXT_ATTRIBUTES if a in xmltree]


def release_dex_problems(dex: list[bytes], configured_url: str) -> list[str]:
    problems = []
    if not configured_url.startswith("https://"):
        problems.append(f"the configured release address is not https: {configured_url}")
    blob = b"\n".join(dex)
    if configured_url.encode() not in blob:
        problems.append(f"the configured address {configured_url} is not in the code")
    if EMULATOR_URL.encode() in blob:
        problems.append("the emulator's development address 10.0.2.2 is in the release code")
    if TEST_SERVER_MARKER.encode() in blob:
        problems.append("the test server's stored address (care_test_server) is in the release code")
    return problems


def debug_dex_problems(dex: list[bytes]) -> list[str]:
    blob = b"\n".join(dex)
    return [] if TEST_SERVER_MARKER.encode() in blob else ["the debug build has no test server address (care_test_server)"]


def dex_of(apk: Path) -> list[bytes]:
    with zipfile.ZipFile(apk) as z:
        return [z.read(n) for n in z.namelist() if n.startswith("classes") and n.endswith(".dex")]


def aapt2() -> str:
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not sdk:
        sys.exit("ANDROID_HOME is not set")
    tools = sorted((Path(sdk) / "build-tools").glob("*/aapt2"))
    if not tools:
        sys.exit("no aapt2 under ANDROID_HOME/build-tools")
    return str(tools[-1])


def main() -> int:
    configured = os.environ.get("IRA_CARE_API_BASE_URL") or DEFAULT_RELEASE_URL
    tool = aapt2()
    releases = sorted(APKS.glob("*/release/*.apk"))
    debugs = sorted(APKS.glob("*/debug/*.apk"))
    if not releases or not debugs:
        print(f"expected release and debug APKs under {APKS}; found {len(releases)} and {len(debugs)}")
        return 1
    failed = False
    for apk in releases:
        tree = subprocess.run([tool, "dump", "xmltree", "--file", "AndroidManifest.xml", str(apk)],
                              check=True, capture_output=True, text=True).stdout
        problems = manifest_problems(tree) + release_dex_problems(dex_of(apk), configured)
        print(f"{apk.relative_to(ROOT)}: " + ("; ".join(problems) if problems else f"ok (server {configured}, no cleartext)"))
        failed |= bool(problems)
    for apk in debugs:
        problems = debug_dex_problems(dex_of(apk))
        print(f"{apk.relative_to(ROOT)}: " + ("; ".join(problems) if problems else "ok (test server address can be set)"))
        failed |= bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
