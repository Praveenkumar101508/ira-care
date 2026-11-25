"""The IRA CARE OS product layer (`os/`) is consistent with itself and with the watch app.

Register items E1, E2 and E6. The product layer cannot be built here (an AOSP build needs
about 400 GB free disk and 64 GB RAM), so these tests check what can be checked without any
Android tooling:

- every file a makefile or Android.bp in `os/` points at exists (the one build-time input,
  the signed APK, is named explicitly and must be produced by the build script);
- the privileged-permission allowlist names exactly the privileged permissions the watch
  app's manifest requests, parsed from both XML files;
- the build script refuses on low resources and never downloads without the opt-in;
- the architecture document names all five layers.

Each check has a negative control in this file: the same helper is run on a deliberately
broken temporary copy and must report the break.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
OS_DIR = ROOT / "os"
PRODUCT = OS_DIR / "device" / "ira" / "ira_watch_cf"
BUILD_SCRIPT = OS_DIR / "scripts" / "build_cuttlefish.sh"
LAUNCH_SCRIPT = OS_DIR / "scripts" / "launch_cuttlefish.sh"
MAIN_MANIFEST = ROOT / "watch" / "app" / "src" / "main" / "AndroidManifest.xml"
AOSP_MANIFEST = ROOT / "watch" / "app" / "src" / "aosp" / "AndroidManifest.xml"
GRADLE = ROOT / "watch" / "app" / "build.gradle.kts"
ALLOWLIST = PRODUCT / "permissions" / "privapp-permissions-care.ira.watch.xml"
DOCS = ROOT / "docs" / "os"

ANDROID_NS = "{http://schemas.android.com/apk/res/android}"

# Files a makefile or Android.bp names that are produced on the build host, never committed.
# Each must be written by the build script and ignored by git.
BUILD_TIME_INPUTS = {PRODUCT / "prebuilt" / "IraCareWatch.apk"}

# Protection levels from frameworks/base/core/res/AndroidManifest.xml (AOSP, Apache 2.0).
# Every permission the app requests must be classified here; an unclassified one fails the
# test, so a new permission cannot slip past the allowlist check unexamined. The privileged
# entries at the end are not requested by the app; they let the negative controls add one.
PROTECTION_LEVEL = {
    "android.permission.INTERNET": "normal",
    "android.permission.FOREGROUND_SERVICE": "normal|instant",
    "android.permission.FOREGROUND_SERVICE_HEALTH": "normal|instant",
    "android.permission.POST_NOTIFICATIONS": "dangerous|instant",
    "android.permission.RECORD_AUDIO": "dangerous|instant",
    "android.permission.VIBRATE": "normal|instant",
    "android.permission.WAKE_LOCK": "normal|instant",
    "android.permission.BODY_SENSORS": "dangerous",
    "android.permission.BODY_SENSORS_BACKGROUND": "dangerous",
    "android.permission.ACTIVITY_RECOGNITION": "dangerous|instant",
    "android.permission.RECEIVE_BOOT_COMPLETED": "normal",
    "android.permission.ACCESS_FINE_LOCATION": "dangerous|instant",
    "android.permission.ACCESS_COARSE_LOCATION": "dangerous|instant",
    "android.permission.ACCESS_NETWORK_STATE": "normal",
    "android.permission.REBOOT": "signature|privileged",
    "android.permission.WRITE_SECURE_SETTINGS": "signature|privileged|development",
    "android.permission.MANAGE_USERS": "signature|privileged",
    "android.permission.STATUS_BAR": "signature|privileged|recents",
}


# --------------------------------------------------------------------------- helpers

def _strip_mk_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _strip_bp_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


def referenced_files(os_dir: Path) -> set[Path]:
    """Every in-tree file a .mk or Android.bp under os_dir names, as a path under os_dir."""
    refs: set[Path] = set()
    product_prefix = "device/ira/ira_watch_cf/"
    for mk in os_dir.rglob("*.mk"):
        text = _strip_mk_comments(mk.read_text(encoding="utf-8"))
        for m in re.finditer(r"\$\(LOCAL_(?:DIR|PATH)\)/([^\s:\\]+)", text):
            refs.add(mk.parent / m.group(1))
        for m in re.finditer(re.escape(product_prefix) + r"([^\s:\\]+)", text):
            refs.add(os_dir / product_prefix / m.group(1))
    for bp in os_dir.rglob("Android.bp"):
        text = _strip_bp_comments(bp.read_text(encoding="utf-8"))
        for m in re.finditer(r"\b(?:apk|src|manifest)\s*:\s*\"([^\"]+)\"", text):
            refs.add(bp.parent / m.group(1))
        for m in re.finditer(r"\b(?:srcs|resource_dirs)\s*:\s*\[([^\]]*)\]", text):
            for item in re.findall(r"\"([^\"]+)\"", m.group(1)):
                refs.add(bp.parent / item)
    return refs


def missing_references(os_dir: Path, build_time_inputs: set[Path]) -> list[Path]:
    return sorted(p for p in referenced_files(os_dir) if not p.exists() and p not in build_time_inputs)


def bp_module_names(bp: Path) -> set[str]:
    text = _strip_bp_comments(bp.read_text(encoding="utf-8"))
    return set(re.findall(r"^\w+\s*\{[^}]*?\bname\s*:\s*\"([^\"]+)\"", text, flags=re.M | re.S))


def manifest_permissions(*manifests: Path) -> set[str]:
    perms: set[str] = set()
    for path in manifests:
        if not path.exists():
            continue
        for el in ET.parse(path).getroot().iter("uses-permission"):
            perms.add(el.attrib[ANDROID_NS + "name"])
    return perms


def privileged_permissions(perms: set[str]) -> set[str]:
    unknown = sorted(p for p in perms if p not in PROTECTION_LEVEL)
    if unknown:
        raise LookupError(
            "classify these permissions in PROTECTION_LEVEL from frameworks/base/core/res/"
            f"AndroidManifest.xml before the allowlist can be checked: {unknown}"
        )
    return {p for p in perms if "privileged" in PROTECTION_LEVEL[p].split("|")}


def allowlist(path: Path) -> tuple[str, set[str]]:
    root = ET.parse(path).getroot()
    entries = root.findall("privapp-permissions")
    assert len(entries) == 1, "exactly one privapp-permissions entry, for the watch app"
    entry = entries[0]
    assert not entry.findall("deny-permission"), "no deny entries expected"
    return entry.attrib["package"], {p.attrib["name"] for p in entry.findall("permission")}


def allowlist_mismatch(manifests: tuple[Path, ...], allowlist_path: Path) -> tuple[set[str], set[str]]:
    """(needed but not allowlisted, allowlisted but not needed)."""
    needed = privileged_permissions(manifest_permissions(*manifests))
    _, listed = allowlist(allowlist_path)
    return needed - listed, listed - needed


def application_id() -> str:
    m = re.search(r'applicationId\s*=\s*"([^"]+)"', GRADLE.read_text(encoding="utf-8"))
    assert m, "applicationId not found in watch/app/build.gradle.kts"
    return m.group(1)


LAYERS = {
    "watch app": r"watch app",
    "launcher": r"launcher",
    "system image": r"system image",
    "firmware/bootloader": r"firmware.{0,40}bootloader|bootloader.{0,40}firmware",
    "production OS": r"production os",
}


def missing_layers(text: str) -> list[str]:
    return [name for name, pattern in LAYERS.items() if not re.search(pattern, text, flags=re.I | re.S)]


def _fake_network_tools(bin_dir: Path, marker: Path) -> None:
    """Stand-ins for every tool that could download: each records that it was called."""
    bin_dir.mkdir()
    for tool in ("repo", "git", "curl", "wget", "gradle", "aria2c"):
        f = bin_dir / tool
        f.write_text(f"#!/bin/sh\necho {tool} \"$@\" >> '{marker}'\nexit 0\n")
        f.chmod(f.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def run_build_script(tmp_path: Path, **env_overrides: str) -> tuple[subprocess.CompletedProcess, Path, Path]:
    marker = tmp_path / "network_tool_calls.log"
    bin_dir = tmp_path / "bin"
    _fake_network_tools(bin_dir, marker)
    aosp_dir = tmp_path / "aosp"
    env = {k: v for k, v in os.environ.items() if not k.startswith("IRA_OS_")}
    env.update({"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}", "IRA_OS_AOSP_DIR": str(aosp_dir), "HOME": str(tmp_path)})
    env.update(env_overrides)
    proc = subprocess.run(["bash", str(BUILD_SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    return proc, marker, aosp_dir


ENOUGH = {"IRA_OS_MEASURED_DISK_GB": "500", "IRA_OS_MEASURED_RAM_GB": "128",
          "IRA_OS_MEASURED_CPUS": "16", "IRA_OS_MEASURED_ARCH": "x86_64"}


# --------------------------------------------------------------------------- references

def test_every_referenced_file_exists():
    refs = referenced_files(OS_DIR)
    # Guard against a parser that finds nothing and passes vacuously.
    assert len(refs) >= 6, sorted(refs)
    assert PRODUCT / "ira_watch_cf.mk" in refs
    assert ALLOWLIST in refs
    assert missing_references(OS_DIR, BUILD_TIME_INPUTS) == []


def test_build_time_inputs_are_produced_by_the_script_and_never_committed():
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    for p in BUILD_TIME_INPUTS:
        assert p in referenced_files(OS_DIR), f"{p} is no longer referenced; drop it from BUILD_TIME_INPUTS"
        assert str(p.relative_to(OS_DIR / "device" / "ira")) in script, f"build script must produce {p}"
        ignored = subprocess.run(["git", "check-ignore", "-q", str(p)], cwd=ROOT)
        assert ignored.returncode == 0, f"{p} must be git-ignored"


def test_product_packages_and_required_modules_are_defined():
    modules = bp_module_names(PRODUCT / "Android.bp")
    mk = _strip_mk_comments((PRODUCT / "ira_watch_cf.mk").read_text(encoding="utf-8"))
    block = re.search(r"PRODUCT_PACKAGES\s*\+=\s*((?:.*\\\n)*.*)", mk)
    assert block
    packages = set(block.group(1).replace("\\", " ").split())
    assert packages and packages <= modules, packages - modules
    bp = _strip_bp_comments((PRODUCT / "Android.bp").read_text(encoding="utf-8"))
    required = set(re.findall(r"\"([^\"]+)\"", re.search(r"required\s*:\s*\[([^\]]*)\]", bp).group(1)))
    assert required and required <= modules, required - modules


def test_negative_control_a_missing_reference_is_caught(tmp_path):
    copy = tmp_path / "os"
    shutil.copytree(OS_DIR, copy)
    bp = copy / "device" / "ira" / "ira_watch_cf" / "Android.bp"
    bp.write_text(bp.read_text(encoding="utf-8") + '\nprebuilt_etc {\n    name: "ghost",\n    src: "permissions/ghost.xml",\n}\n')
    mk = copy / "device" / "ira" / "ira_watch_cf" / "ira_watch_cf.mk"
    mk.write_text(mk.read_text(encoding="utf-8") + "\nPRODUCT_COPY_FILES += device/ira/ira_watch_cf/missing.rc:$(TARGET_COPY_OUT_PRODUCT)/etc/init/missing.rc\n")
    inputs = {copy / p.relative_to(OS_DIR) for p in BUILD_TIME_INPUTS}
    missing = {p.name for p in missing_references(copy, inputs)}
    assert missing == {"ghost.xml", "missing.rc"}


# --------------------------------------------------------------------------- permissions

def test_allowlist_names_the_app_package():
    package, _ = allowlist(ALLOWLIST)
    assert package == application_id() == "care.ira.watch"


def test_allowlist_equals_the_privileged_permissions_in_the_manifest():
    requested = manifest_permissions(MAIN_MANIFEST, AOSP_MANIFEST)
    assert len(requested) >= 10, "manifest parse found too few permissions"
    missing, extra = allowlist_mismatch((MAIN_MANIFEST, AOSP_MANIFEST), ALLOWLIST)
    assert missing == set(), f"privileged permissions requested but not allowlisted: {missing}"
    assert extra == set(), f"allowlisted permissions the manifest does not request: {extra}"


def test_negative_control_an_extra_allowlisted_permission_is_caught(tmp_path):
    bad = tmp_path / "privapp.xml"
    text = ALLOWLIST.read_text(encoding="utf-8").replace(
        "</privapp-permissions>", '    <permission name="android.permission.REBOOT" />\n    </privapp-permissions>')
    bad.write_text(text)
    missing, extra = allowlist_mismatch((MAIN_MANIFEST, AOSP_MANIFEST), bad)
    assert missing == set() and extra == {"android.permission.REBOOT"}


def test_negative_control_a_privileged_permission_added_to_the_manifest_is_caught(tmp_path):
    bad = tmp_path / "AndroidManifest.xml"
    bad.write_text(MAIN_MANIFEST.read_text(encoding="utf-8").replace(
        '<uses-permission android:name="android.permission.INTERNET" />',
        '<uses-permission android:name="android.permission.INTERNET" />\n'
        '    <uses-permission android:name="android.permission.WRITE_SECURE_SETTINGS" />'))
    missing, extra = allowlist_mismatch((bad,), ALLOWLIST)
    assert missing == {"android.permission.WRITE_SECURE_SETTINGS"} and extra == set()


def test_negative_control_an_unclassified_permission_stops_the_check(tmp_path):
    bad = tmp_path / "AndroidManifest.xml"
    bad.write_text(MAIN_MANIFEST.read_text(encoding="utf-8").replace(
        '<uses-permission android:name="android.permission.INTERNET" />',
        '<uses-permission android:name="android.permission.INTERNET" />\n'
        '    <uses-permission android:name="android.permission.NOT_CLASSIFIED_YET" />'))
    with pytest.raises(LookupError):
        allowlist_mismatch((bad,), ALLOWLIST)


def test_first_boot_grants_are_only_permissions_the_app_requests():
    root = ET.parse(PRODUCT / "permissions" / "default-permissions-care.ira.watch.xml").getroot()
    (exception,) = root.findall("exception")
    assert exception.attrib["package"] == application_id()
    granted = {p.attrib["name"] for p in exception.findall("permission")}
    requested = manifest_permissions(MAIN_MANIFEST, AOSP_MANIFEST)
    assert granted and granted <= requested, granted - requested
    assert all(PROTECTION_LEVEL[p].startswith("dangerous") for p in granted)


# --------------------------------------------------------------------------- build script

@pytest.mark.parametrize("override, reason", [
    ({"IRA_OS_MEASURED_DISK_GB": "57", "IRA_OS_MEASURED_RAM_GB": "15", "IRA_OS_MEASURED_CPUS": "4"}, "below 400 GB"),
    ({"IRA_OS_MEASURED_DISK_GB": "399"}, "free disk 399 GB is below 400 GB"),
    ({"IRA_OS_MEASURED_RAM_GB": "63"}, "memory 63 GB is below 64 GB"),
    ({"IRA_OS_MEASURED_ARCH": "aarch64"}, "is not x86_64"),
    ({"IRA_OS_MEASURED_CPUS": "4"}, "below IRA's floor of 8"),
])
def test_build_script_refuses_on_low_resources(tmp_path, override, reason):
    env = {**ENOUGH, **override}
    proc, marker, aosp_dir = run_build_script(tmp_path, **env)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "REFUSED" in proc.stderr and reason in proc.stderr
    assert "Nothing was downloaded" in proc.stderr
    assert not marker.exists() and not aosp_dir.exists()


def test_build_script_refuses_low_resources_even_when_opted_in(tmp_path):
    proc, marker, aosp_dir = run_build_script(
        tmp_path, IRA_OS_I_HAVE_THE_RESOURCES="1", IRA_OS_MEASURED_DISK_GB="57")
    assert proc.returncode == 2
    assert not marker.exists() and not aosp_dir.exists()


def test_build_script_does_not_download_without_the_opt_in(tmp_path):
    proc, marker, aosp_dir = run_build_script(tmp_path, **ENOUGH)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "preflight passed" in proc.stdout
    assert "NOT OPTED IN: nothing will be downloaded" in proc.stdout
    assert not marker.exists(), marker.read_text() if marker.exists() else ""
    assert not aosp_dir.exists()


@pytest.mark.parametrize("value", ["0", "yes", "true", ""])
def test_only_the_exact_opt_in_value_counts(tmp_path, value):
    proc, marker, _ = run_build_script(tmp_path, IRA_OS_I_HAVE_THE_RESOURCES=value, **ENOUGH)
    assert proc.returncode == 3
    assert not marker.exists()


def test_test_overrides_cannot_be_combined_with_the_opt_in(tmp_path):
    proc, marker, aosp_dir = run_build_script(tmp_path, IRA_OS_I_HAVE_THE_RESOURCES="1", **ENOUGH)
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert "for tests only" in proc.stderr
    assert not marker.exists() and not aosp_dir.exists()


def test_build_script_on_this_machine_never_downloads(tmp_path):
    """No overrides: whatever this machine measures, without the opt-in nothing is fetched."""
    proc, marker, aosp_dir = run_build_script(tmp_path)
    assert proc.returncode in (2, 3), proc.stdout + proc.stderr
    assert not marker.exists() and not aosp_dir.exists()


def test_build_script_minimums_are_the_published_ones():
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"^readonly MIN_DISK_GB=400$", text, flags=re.M)
    assert re.search(r"^readonly MIN_RAM_GB=64$", text, flags=re.M)
    assert re.search(r"^readonly REQUIRED_ARCH=x86_64$", text, flags=re.M)


def test_launch_script_uses_launch_cvd_with_a_watch_display():
    env = {**os.environ, "IRA_OS_PRINT_ONLY": "1"}
    env.pop("IRA_CF_DISPLAY_FLAGS", None)
    proc = subprocess.run(["bash", str(LAUNCH_SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0
    assert proc.stdout.startswith("launch_cvd ")
    assert "width=454,height=454,dpi=320" in proc.stdout


def test_launch_script_refuses_without_a_built_product(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("ANDROID_PRODUCT_OUT", "IRA_OS_PRINT_ONLY")}
    proc = subprocess.run(["bash", str(LAUNCH_SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 4 and "ANDROID_PRODUCT_OUT" in proc.stderr


# --------------------------------------------------------------------------- docs and spec

def test_architecture_names_the_five_layers():
    text = (DOCS / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert missing_layers(text) == []


def test_negative_control_a_missing_layer_is_caught():
    text = (DOCS / "ARCHITECTURE.md").read_text(encoding="utf-8")
    stripped = re.sub(r"production os", "shipping thing", text, flags=re.I)
    assert missing_layers(stripped) == ["production OS"]


def test_build_doc_marks_every_step_as_not_run():
    text = (DOCS / "BUILD.md").read_text(encoding="utf-8")
    steps = re.findall(r"^\d+\. \*\*.+?(?=^\d+\. \*\*|^## |\Z)", text, flags=re.M | re.S)
    assert len(steps) >= 8
    for step in steps:
        assert "not run in this repository's CI" in " ".join(step.split()), step[:80]


def test_supplier_register_covers_the_required_components():
    text = (DOCS / "QUALCOMM_DEPENDENCIES.md").read_text(encoding="utf-8").lower()
    for word in ("board support package", "kernel", "modem", "sensor hub", "gpu", "nda"):
        assert word in text, word
    header = next(line for line in text.splitlines() if line.startswith("| #"))
    for column in ("availability", "licence", "owner action"):
        assert column in header


def test_boot_animation_spec_is_text_for_the_watch_display():
    desc = (PRODUCT / "bootanimation" / "desc.txt").read_text(encoding="utf-8")
    assert desc.endswith("\n")
    lines = desc.splitlines()
    assert re.fullmatch(r"454 454 \d+", lines[0])
    for line in lines[1:]:
        assert re.fullmatch(r"[cp] \d+ \d+ part\d+", line), line
    assert not any((PRODUCT / "bootanimation").glob("*.zip")), "no binary assets in the repository"


def test_product_identity_and_locale():
    mk = _strip_mk_comments((PRODUCT / "ira_watch_cf.mk").read_text(encoding="utf-8"))
    assert re.search(r"^PRODUCT_NAME := ira_watch_cf$", mk, flags=re.M)
    assert re.search(r"^PRODUCT_LOCALES := en_GB\b", mk, flags=re.M)
    assert re.search(r"^\$\(call inherit-product, device/google/cuttlefish/vsoc_x86_64", mk, flags=re.M)


def test_no_assistant_branding_in_the_os_layer():
    pattern = re.compile(r"claude|anthropic|generated by|co-authored-by", re.I)
    for base in (OS_DIR, DOCS):
        for f in base.rglob("*"):
            if f.is_file():
                assert not pattern.search(f.read_text(encoding="utf-8", errors="ignore")), f
