"""Plain HTTP from the watch is allowed in debug builds, to the development server only.

Android refuses cleartext traffic by default (targetSdk 35). The debug build talks to the demo
server at http://10.0.2.2:8000, which the emulator journey showed it could never reach ("No
connection. Try again.", run 45758000634), so the debug source set alone carries a network
security config that permits cleartext to the emulator's host address and localhost. These
checks keep it there: no other source set, and not the main manifest, may allow cleartext.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
APP = ROOT / "watch" / "app" / "src"
A = "{http://schemas.android.com/apk/res/android}"
ALLOWED_DEBUG_HOSTS = {"10.0.2.2", "localhost", "127.0.0.1"}


def cleartext_hosts(config_xml: str) -> tuple[bool, set[str]]:
    """(base config permits cleartext, hosts whose domain-config permits it)."""
    root = ET.fromstring(config_xml)
    base = root.find("base-config")
    base_open = base is not None and base.get("cleartextTrafficPermitted") == "true"
    hosts: set[str] = set()
    for dc in root.iter("domain-config"):
        if dc.get("cleartextTrafficPermitted") == "true":
            hosts |= {d.text.strip() for d in dc.findall("domain") if d.text}
    return base_open, hosts


def manifest_allows_cleartext(manifest_xml: str) -> bool:
    app = ET.fromstring(manifest_xml).find("application")
    return app is not None and (app.get(A + "usesCleartextTraffic") == "true"
                                or app.get(A + "networkSecurityConfig") is not None)


def test_main_manifest_does_not_allow_cleartext():
    assert not manifest_allows_cleartext((APP / "main" / "AndroidManifest.xml").read_text(encoding="utf-8"))


def test_only_the_debug_source_set_has_a_network_security_config():
    found = sorted(str(p.relative_to(APP)) for p in APP.rglob("network_security_config*.xml"))
    assert found == ["debug/res/xml/network_security_config.xml"]
    for manifest in APP.glob("*/AndroidManifest.xml"):
        if manifest.parent.name != "debug":
            assert not manifest_allows_cleartext(manifest.read_text(encoding="utf-8")), manifest


def test_debug_cleartext_is_limited_to_the_development_server():
    base_open, hosts = cleartext_hosts((APP / "debug" / "res" / "xml" / "network_security_config.xml")
                                       .read_text(encoding="utf-8"))
    assert not base_open and hosts == ALLOWED_DEBUG_HOSTS
    assert manifest_allows_cleartext((APP / "debug" / "AndroidManifest.xml").read_text(encoding="utf-8"))
    gradle = (ROOT / "watch" / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    assert re.search(r'CARE_API_BASE_URL", "\\"http://10\.0\.2\.2:8000\\""', gradle)


def test_negative_control_wider_cleartext_is_caught():
    wide = ('<network-security-config><base-config cleartextTrafficPermitted="true" />'
            '<domain-config cleartextTrafficPermitted="true"><domain>care.example.com</domain></domain-config>'
            '</network-security-config>')
    base_open, hosts = cleartext_hosts(wide)
    assert base_open and hosts - ALLOWED_DEBUG_HOSTS == {"care.example.com"}
    leaky = ('<manifest xmlns:android="http://schemas.android.com/apk/res/android">'
             '<application android:usesCleartextTraffic="true" /></manifest>')
    assert manifest_allows_cleartext(leaky)
