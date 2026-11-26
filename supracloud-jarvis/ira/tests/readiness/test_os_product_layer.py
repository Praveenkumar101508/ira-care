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

import json
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
BUILD_TIME_INPUTS = {PRODUCT / "prebuilt" / "IraCareWatch.apk", PRODUCT / "bootanimation" / "bootanimation.zip"}

# Protection levels from frameworks/base/core/res/AndroidManifest.xml (AOSP, Apache 2.0), at
# android16-release as read from the public mirror on 26 November 2026; test_protection_levels_
# match_the_aosp_reference holds this table to os/reference/aosp_reference.json.
# Every permission the app requests must be classified here; an unclassified one fails the
# test, so a new permission cannot slip past the allowlist check unexamined. The privileged
# entries at the end are not requested by the app; they let the negative controls add one.
PROTECTION_LEVEL = {
    "android.permission.INTERNET": "normal|instant",
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
    "android.permission.ACCESS_NETWORK_STATE": "normal|instant",
    "android.permission.REBOOT": "signature|privileged",
    "android.permission.WRITE_SECURE_SETTINGS": "signature|privileged|development|role|installer",
    "android.permission.MANAGE_USERS": "signature|privileged",
    "android.permission.STATUS_BAR": "signature|privileged|recents",
}

# What os/scripts/aosp_reference.py read from the public AOSP mirrors (names and constants only).
REFERENCE = json.loads((OS_DIR / "reference" / "aosp_reference.json").read_text(encoding="utf-8"))
SENSORS = OS_DIR / "device" / "ira" / "sensors_sim"
OVERLAY = PRODUCT / "overlays" / "IraFrameworkOverlay"
DEFAULT_GRANTS = PRODUCT / "permissions" / "default-permissions-care.ira.watch.xml"
TOOLS_NS = "{http://schemas.android.com/tools}"


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
            # Compiled caches are git-ignored and never ship; they carry the absolute path of
            # whoever compiled them, which in a nested checkout under a tool's hidden folder matches.
            if f.is_file() and "__pycache__" not in f.parts:
                assert not pattern.search(f.read_text(encoding="utf-8", errors="ignore")), f


# --------------------------------------------------------------------------- launcher (E2.k)

def _launcher_overrides(bp_text: str) -> set[str]:
    """The `overrides` of the IraCareWatch module, comments stripped."""
    bp = _strip_bp_comments(bp_text)
    block = re.search(r'android_app_import\s*\{[^{}]*name:\s*"IraCareWatch"(.*?)\n\}', bp, flags=re.S)
    assert block, "IraCareWatch module not found"
    m = re.search(r"overrides\s*:\s*\[([^\]]*)\]", block.group(1))
    return set(re.findall(r'"([^"]+)"', m.group(1))) if m else set()


def test_the_stock_launcher_is_overridden_and_the_app_is_a_home_app():
    assert "Launcher3QuickStep" in _launcher_overrides((PRODUCT / "Android.bp").read_text(encoding="utf-8"))
    aosp_manifest = (ROOT / "watch/app/src/aosp/AndroidManifest.xml").read_text(encoding="utf-8")
    # Removing the stock launcher is only safe while the app itself declares HOME.
    assert "android.intent.category.HOME" in aosp_manifest and "android.intent.category.DEFAULT" in aosp_manifest


def test_negative_control_a_commented_out_override_is_not_counted():
    bp = (PRODUCT / "Android.bp").read_text(encoding="utf-8")
    disabled = bp.replace('    overrides: ["Launcher3QuickStep"],', '    // overrides: ["Launcher3QuickStep"],')
    assert disabled != bp
    assert _launcher_overrides(disabled) == set()


# --------------------------------------------------------------------------- boot animation (E2.f)

def _render(tmp_path: Path, name: str = "ba.zip") -> Path:
    pytest.importorskip("PIL")
    import importlib.util
    import sys
    sys.dont_write_bytecode = True  # no __pycache__ under os/scripts from a test run
    spec = importlib.util.spec_from_file_location("make_bootanimation", OS_DIR / "scripts" / "make_bootanimation.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build(tmp_path / name)


def test_the_boot_animation_zip_follows_the_platform_format(tmp_path):
    import zipfile
    from io import BytesIO
    from PIL import Image
    out = _render(tmp_path)
    desc = (PRODUCT / "bootanimation" / "desc.txt").read_text(encoding="utf-8")
    width, height, fps = (int(v) for v in desc.splitlines()[0].split())
    parts = [ln.split()[3] for ln in desc.splitlines()[1:] if ln.strip()]
    with zipfile.ZipFile(out) as z:
        infos = z.infolist()
        assert infos[0].filename == "desc.txt" and z.read("desc.txt").decode() == desc
        assert all(i.compress_type == zipfile.ZIP_STORED for i in infos), "every entry stored, never deflated"
        for part in parts:
            frames = sorted(i.filename for i in infos if i.filename.startswith(part + "/"))
            assert frames and frames == [f"{part}/{n:05d}.png" for n in range(len(frames))]
            img = Image.open(BytesIO(z.read(frames[-1])))
            assert img.size == (width, height) and img.mode == "RGB"
        counts = {p: sum(i.filename.startswith(p + "/") for i in infos) for p in parts}
        assert counts == {"part0": fps, "part1": 2 * fps}
        # The round panel's edge stays black: the corner pixel of every last frame.
        assert Image.open(BytesIO(z.read("part1/" + f"{2 * fps - 1:05d}.png"))).getpixel((0, 0)) == (0, 0, 0)


def test_the_boot_animation_is_deterministic(tmp_path):
    import hashlib
    a, b = _render(tmp_path, "a.zip"), _render(tmp_path, "b.zip")
    assert hashlib.sha256(a.read_bytes()).hexdigest() == hashlib.sha256(b.read_bytes()).hexdigest()


# =========================================================================== host checks
# Everything below checks os/ against the watch app's sources and against the names and
# constants os/scripts/aosp_reference.py read from the public AOSP mirrors. None of it proves
# that an image builds or boots (docs/os/HOST_CHECKS.md says what each test does not prove).

# --------------------------------------------------------------------------- merged manifest

def merged_aosp_manifest(main: Path = MAIN_MANIFEST, overlay: Path = AOSP_MANIFEST) -> ET.Element:
    """The aosp flavour's manifest merged over main's, as Gradle's merger would, approximately.

    Modelled: uses-permission and uses-feature are a union; an application component in the
    overlay with the same android:name as one in main merges into it (new attributes added,
    intent-filters appended; the same attribute with a different value is a merge conflict and
    raises, as the real merger fails without tools:replace). Not modelled: tools:node other
    than merge, placeholders, and the permissions library manifests add at build time, which
    os/scripts/build_cuttlefish.sh checks on the built APK with aapt2.
    """
    root = ET.parse(main).getroot()
    ov = ET.parse(overlay).getroot()
    for tag in ("uses-permission", "uses-feature"):
        have = {el.attrib[ANDROID_NS + "name"] for el in root.iter(tag)}
        for el in ov.findall(tag):
            if el.attrib[ANDROID_NS + "name"] not in have:
                root.append(el)
    app, ov_app = root.find("application"), ov.find("application")
    for comp in list(ov_app) if ov_app is not None else []:
        name = comp.attrib.get(ANDROID_NS + "name")
        target = next((c for c in app if c.tag == comp.tag and c.attrib.get(ANDROID_NS + "name") == name), None)
        if target is None:
            app.append(comp)
            continue
        node = comp.attrib.get(TOOLS_NS + "node", "merge")
        if node != "merge":
            raise ValueError(f"tools:node={node} is not modelled")
        for k, v in comp.attrib.items():
            if k.startswith(TOOLS_NS):
                continue
            if k in target.attrib and target.attrib[k] != v:
                raise ValueError(f"merge conflict on {comp.tag} {name}: {k}={target.attrib[k]!r} vs {v!r}")
            target.attrib[k] = v
        for child in comp:
            target.append(child)
    return root


def home_activity(manifest: ET.Element) -> ET.Element | None:
    """The activity with a MAIN + HOME + DEFAULT intent filter, or None."""
    for act in manifest.iter("activity"):
        for f in act.findall("intent-filter"):
            actions = {a.attrib[ANDROID_NS + "name"] for a in f.findall("action")}
            cats = {c.attrib[ANDROID_NS + "name"] for c in f.findall("category")}
            if "android.intent.action.MAIN" in actions and {"android.intent.category.HOME", "android.intent.category.DEFAULT"} <= cats:
                return act
    return None


def test_protection_levels_match_the_aosp_reference():
    assert REFERENCE["permission_protection_levels"] == PROTECTION_LEVEL


def test_privileged_allowlist_matches_the_merged_aosp_manifest():
    merged = merged_aosp_manifest()
    requested = {el.attrib[ANDROID_NS + "name"] for el in merged.iter("uses-permission")}
    assert requested == manifest_permissions(MAIN_MANIFEST), "the aosp flavour manifest must add no permission"
    package, listed = allowlist(ALLOWLIST)
    assert package == application_id()
    assert privileged_permissions(requested) == listed == set()


def test_negative_control_a_privileged_permission_in_the_flavour_manifest_is_caught(tmp_path):
    bad = tmp_path / "AndroidManifest.xml"
    bad.write_text(AOSP_MANIFEST.read_text(encoding="utf-8").replace(
        "<application>", '<uses-permission android:name="android.permission.REBOOT" />\n    <application>'))
    merged = merged_aosp_manifest(MAIN_MANIFEST, bad)
    requested = {el.attrib[ANDROID_NS + "name"] for el in merged.iter("uses-permission")}
    assert privileged_permissions(requested) - allowlist(ALLOWLIST)[1] == {"android.permission.REBOOT"}


def test_negative_control_a_conflicting_attribute_is_a_merge_error(tmp_path):
    bad = tmp_path / "AndroidManifest.xml"
    bad.write_text(AOSP_MANIFEST.read_text(encoding="utf-8").replace(
        'android:showWhenLocked="true"', 'android:showWhenLocked="true"\n            android:exported="false"'))
    with pytest.raises(ValueError, match="merge conflict"):
        merged_aosp_manifest(MAIN_MANIFEST, bad)


# --------------------------------------------------------------------------- first-boot grants (E2.c)

def first_boot_grants(path: Path) -> tuple[str, dict[str, dict[str, str]]]:
    """(package, {permission: attributes}) after checking the file against the platform's parser.

    The tags and attributes are the ones DefaultPermissionGrantPolicy.java reads, recorded in
    the reference; anything else is silently ignored by the platform, so here it is an error.
    """
    schema = REFERENCE["default_permissions_xml"]["tags_and_attributes"]
    root = ET.parse(path).getroot()
    assert root.tag == schema["TAG_EXCEPTIONS"], root.tag
    exceptions = root.findall(schema["TAG_EXCEPTION"])
    assert len(exceptions) == 1 and [c.tag for c in root] == [schema["TAG_EXCEPTION"]], "one exception for the watch app"
    (exc,) = exceptions
    assert set(exc.attrib) <= {schema["ATTR_PACKAGE"], schema["ATTR_CERT"]}, exc.attrib
    grants: dict[str, dict[str, str]] = {}
    for p in exc:
        assert p.tag == schema["TAG_PERMISSION"], p.tag
        assert set(p.attrib) <= {schema["ATTR_NAME"], schema["ATTR_FIXED"], schema["ATTR_WHITELISTED"]}, p.attrib
        for flag in (schema["ATTR_FIXED"], schema["ATTR_WHITELISTED"]):
            assert p.attrib.get(flag, "false") in ("true", "false"), (p.attrib["name"], flag)
        assert p.attrib[schema["ATTR_NAME"]] not in grants, "duplicate grant"
        grants[p.attrib[schema["ATTR_NAME"]]] = dict(p.attrib)
    return exc.attrib[schema["ATTR_PACKAGE"]], grants


def runtime_permissions(requested: set[str]) -> set[str]:
    return {p for p in requested if PROTECTION_LEVEL[p].split("|")[0] == "dangerous"}


def test_first_boot_grants_are_exactly_the_runtime_permissions_requested_and_revocable():
    package, grants = first_boot_grants(DEFAULT_GRANTS)
    assert package == application_id()
    requested = {el.attrib[ANDROID_NS + "name"] for el in merged_aosp_manifest().iter("uses-permission")}
    runtime = runtime_permissions(requested)
    assert len(runtime) >= 5
    assert set(grants) == runtime, (set(grants) ^ runtime)
    assert all(a.get("fixed") == "false" for a in grants.values()), "every grant stays revocable in Settings"
    # Installed where the policy reads it: /product/etc/default-permissions.
    bp = _strip_bp_comments((PRODUCT / "Android.bp").read_text(encoding="utf-8"))
    block = re.search(r'prebuilt_etc\s*\{[^{}]*name:\s*"default_permissions_care\.ira\.watch"(.*?)\n\}', bp, flags=re.S).group(1)
    assert 'sub_dir: "default-permissions"' in block and "product_specific: true" in block
    assert "Product" in REFERENCE["default_permissions_xml"]["directories_read"]


@pytest.mark.parametrize("old, new, problem", [
    ('<permission name="android.permission.RECORD_AUDIO" fixed="false" />',
     '<permission name="android.permission.RECORD_AUDIO" fixed="false" />\n'
     '        <permission name="android.permission.VIBRATE" fixed="false" />', "a normal permission granted"),
    ('<permission name="android.permission.ACCESS_COARSE_LOCATION" fixed="false" />',
     '<permission name="android.permission.ACCESS_COARSE_LOCATION" fixed="false" whitelisted="yes" />',
     "an attribute value the parser does not accept"),
    ('        <permission name="android.permission.BODY_SENSORS" fixed="false" />\n', "", "a runtime permission not granted"),
    ('<permission name="android.permission.RECORD_AUDIO" fixed="false" />',
     '<permission name="android.permission.RECORD_AUDIO" fixed="true" />', "a grant the person cannot revoke"),
    ('<permission name="android.permission.RECORD_AUDIO" fixed="false" />',
     '<permission name="android.permission.RECORD_AUDIO" fixed="false" grant="now" />', "an attribute the parser ignores"),
])
def test_negative_control_first_boot_grant_defects_are_caught(tmp_path, old, new, problem):
    text = DEFAULT_GRANTS.read_text(encoding="utf-8")
    assert old in text
    bad = tmp_path / "default-permissions.xml"
    bad.write_text(text.replace(old, new, 1))
    with pytest.raises(AssertionError):
        _, grants = first_boot_grants(bad)
        assert set(grants) == runtime_permissions(manifest_permissions(MAIN_MANIFEST)), problem
        assert all(a.get("fixed") == "false" for a in grants.values()), problem


# --------------------------------------------------------------------------- product makefile (E2.a)

def mk_values(text: str) -> dict[str, list[str]]:
    """Every `NAME := ...` or `NAME += ...` in a stripped makefile, with continuation lines joined."""
    joined = re.sub(r"\\\n", " ", _strip_mk_comments(text))
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"^(\w+)\s*[:+]?=\s*(.*)$", joined, flags=re.M):
        out.setdefault(m.group(1), []).extend(m.group(2).split())
    return out


def copy_file_sources(text: str) -> list[tuple[str, str]]:
    return [tuple(entry.split(":", 1)) for entry in mk_values(text).get("PRODUCT_COPY_FILES", [])]


def unresolved_copy_sources(text: str, os_dir: Path, build_time_inputs: set[Path]) -> list[str]:
    """Sources that are neither a file in os/ nor an AOSP file the reference saw."""
    present = REFERENCE["frameworks_native_data_etc_present"]
    bad = []
    for src, _dst in copy_file_sources(text):
        if src.startswith("device/ira/"):
            p = os_dir / src
            if not p.exists() and p not in build_time_inputs:
                bad.append(src)
        elif src.startswith("frameworks/native/data/etc/") and present.get(src.rsplit("/", 1)[1]):
            continue
        else:
            bad.append(src)
    return bad


def test_product_copy_files_sources_all_resolve():
    mk = (PRODUCT / "ira_watch_cf.mk").read_text(encoding="utf-8")
    sources = copy_file_sources(mk)
    assert len(sources) >= 2
    assert unresolved_copy_sources(mk, OS_DIR, BUILD_TIME_INPUTS) == []
    for _src, dst in sources:
        assert dst.startswith("$(TARGET_COPY_OUT_PRODUCT)/"), dst
    # The watch feature is declared by IRA's own file; AOSP ships none of that name.
    assert REFERENCE["frameworks_native_data_etc_present"]["android.hardware.type.watch.xml"] is False
    feature = ET.parse(PRODUCT / "permissions" / "android.hardware.type.watch.xml").getroot()
    assert [f.attrib["name"] for f in feature.findall("feature")] == ["android.hardware.type.watch"]
    assert "android.hardware.type.watch" in {el.attrib[ANDROID_NS + "name"] for el in merged_aosp_manifest().iter("uses-feature")}


def test_negative_control_the_former_aosp_feature_path_is_reported_missing():
    mk = (PRODUCT / "ira_watch_cf.mk").read_text(encoding="utf-8")
    old = mk.replace("device/ira/ira_watch_cf/permissions/android.hardware.type.watch.xml:",
                     "frameworks/native/data/etc/android.hardware.type.watch.xml:")
    assert old != mk
    assert unresolved_copy_sources(old, OS_DIR, BUILD_TIME_INPUTS) == ["frameworks/native/data/etc/android.hardware.type.watch.xml"]
    # A file AOSP does have is accepted, so the check is about existence, not about the prefix.
    ok = mk + "\nPRODUCT_COPY_FILES += frameworks/native/data/etc/android.hardware.sensor.gyroscope.xml:$(TARGET_COPY_OUT_PRODUCT)/etc/permissions/x.xml\n"
    assert unresolved_copy_sources(ok, OS_DIR, BUILD_TIME_INPUTS) == []


def test_product_registration_is_consistent():
    mk_text = (PRODUCT / "ira_watch_cf.mk").read_text(encoding="utf-8")
    mk = mk_values(mk_text)
    products = mk_values((PRODUCT / "AndroidProducts.mk").read_text(encoding="utf-8"))
    assert mk["PRODUCT_NAME"] == ["ira_watch_cf"]
    assert products["PRODUCT_MAKEFILES"] == ["ira_watch_cf:$(LOCAL_DIR)/ira_watch_cf.mk"]
    choices = products["COMMON_LUNCH_CHOICES"]
    assert len(choices) >= 2
    for choice in choices:
        m = re.fullmatch(r"(\w+)-(\w+)-(user|userdebug|eng)", choice)
        assert m and m.group(1) == "ira_watch_cf", choice
        assert m.group(2) == REFERENCE["lunch_default_release"], "the release config lunch assumes when none is given"
    inherit = re.search(r"\$\(call inherit-product, ([^)]+)\)", _strip_mk_comments(mk_text))
    assert inherit
    assert mk["PRODUCT_DEVICE"] == [inherit.group(1).split("/")[3]], "PRODUCT_DEVICE must be the inherited Cuttlefish device"
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert f'INHERITED_MK="{inherit.group(1)}"' in script, "the build script checks the same inherited makefile"
    assert f"LUNCH_TARGET=\"${{IRA_OS_LUNCH:-{choices[0]}}}\"" in script
    board_info = mk["TARGET_BOARD_INFO_FILE"][0]
    assert (OS_DIR / board_info).read_text(encoding="utf-8").strip() == "config=phone"
    assert mk["PRODUCT_LOCALES"][0] == "en_GB"
    assert "ro.control_privapp_permissions=enforce" in mk["PRODUCT_PRODUCT_PROPERTIES"]


def test_negative_control_a_lunch_choice_for_another_product_is_caught():
    products = mk_values("COMMON_LUNCH_CHOICES := \\\n    ira_watch_cf-trunk_staging-userdebug \\\n    ira_watch-trunk_staging-user\n")
    bad = [c for c in products["COMMON_LUNCH_CHOICES"] if not re.fullmatch(r"ira_watch_cf-\w+-(user|userdebug|eng)", c)]
    assert bad == ["ira_watch-trunk_staging-user"]


def test_product_packages_and_overrides_resolve_to_known_modules():
    modules = bp_module_names(PRODUCT / "Android.bp")
    mk = mk_values((PRODUCT / "ira_watch_cf.mk").read_text(encoding="utf-8"))
    unknown = [p for p in mk["PRODUCT_PACKAGES"] if p not in modules]
    assert unknown == [], f"PRODUCT_PACKAGES not defined in os/ or known from AOSP: {unknown}"
    overrides = _launcher_overrides((PRODUCT / "Android.bp").read_text(encoding="utf-8"))
    assert overrides <= set(REFERENCE["system_ext_packages"]), f"overrides naming no AOSP handheld system_ext package: {overrides}"


# --------------------------------------------------------------------------- framework overlay (E2.d)

def overlay_problems(config: Path, reference: dict[str, str]) -> list[str]:
    problems = []
    root = ET.parse(config).getroot()
    assert root.tag == "resources"
    for el in root:
        name = el.attrib.get("name")
        if name not in reference:
            problems.append(f"{name}: not a framework resource in the reference")
        elif reference[name] != el.tag:
            problems.append(f"{name}: framework type {reference[name]}, overlay says {el.tag}")
        if el.tag == "bool" and el.text not in ("true", "false"):
            problems.append(f"{name}: bool value {el.text!r}")
        if el.tag == "integer" and not re.fullmatch(r"-?\d+", el.text or ""):
            problems.append(f"{name}: integer value {el.text!r}")
    return problems


def test_every_overlaid_resource_exists_in_the_framework_with_its_type():
    config = OVERLAY / "res" / "values" / "config.xml"
    assert len(ET.parse(config).getroot()) >= 5
    assert overlay_problems(config, REFERENCE["config_xml_resources"]) == []
    manifest = ET.parse(OVERLAY / "AndroidManifest.xml").getroot()
    assert manifest.attrib["package"] == "care.ira.os.overlay.framework"
    overlay = manifest.find("overlay")
    assert overlay.attrib[ANDROID_NS + "targetPackage"] == "android"
    assert overlay.attrib[ANDROID_NS + "isStatic"] == "true"
    assert re.fullmatch(r"\d+", overlay.attrib[ANDROID_NS + "priority"])
    assert manifest.find("application").attrib[ANDROID_NS + "hasCode"] == "false"


def test_negative_control_an_unknown_or_mistyped_overlay_resource_is_caught(tmp_path):
    text = (OVERLAY / "res" / "values" / "config.xml").read_text(encoding="utf-8")
    bad = tmp_path / "config.xml"
    bad.write_text(text.replace('<bool name="config_windowIsRound">', '<bool name="config_windowIsRoundish">')
                   .replace('<integer name="config_longPressOnPowerBehavior">1</integer>',
                            '<bool name="config_longPressOnPowerBehavior">1</bool>'))
    problems = overlay_problems(bad, REFERENCE["config_xml_resources"])
    assert any("config_windowIsRoundish: not a framework resource" in p for p in problems)
    assert any("config_longPressOnPowerBehavior: framework type integer" in p for p in problems)
    assert any("bool value '1'" in p for p in problems)


# --------------------------------------------------------------------------- launcher (E2.k, merged)

def test_the_merged_aosp_manifest_makes_the_app_the_home_screen_and_help_reachable_when_locked():
    act = home_activity(merged_aosp_manifest())
    assert act is not None, "no activity with MAIN + HOME + DEFAULT in the merged aosp manifest"
    assert act.attrib[ANDROID_NS + "name"] == ".ui.MainActivity"
    assert act.attrib[ANDROID_NS + "exported"] == "true"
    assert act.attrib[ANDROID_NS + "launchMode"] == "singleTask"
    assert act.attrib[ANDROID_NS + "showWhenLocked"] == "true"
    assert act.attrib[ANDROID_NS + "turnScreenOn"] == "true"
    # The wearos flavour (main manifest alone) never offers itself as home.
    assert home_activity(ET.parse(MAIN_MANIFEST).getroot()) is None
    assert "Launcher3QuickStep" in REFERENCE["system_ext_packages"]


def test_negative_control_a_flavour_manifest_without_home_is_caught(tmp_path):
    bad = tmp_path / "AndroidManifest.xml"
    bad.write_text(AOSP_MANIFEST.read_text(encoding="utf-8").replace(
        '<category android:name="android.intent.category.HOME" />', ""))
    assert home_activity(merged_aosp_manifest(MAIN_MANIFEST, bad)) is None


# --------------------------------------------------------------------------- boot animation (E2.e, E2.f)

def _readme_frame_counts() -> dict[str, int]:
    text = (PRODUCT / "bootanimation" / "README.md").read_text(encoding="utf-8")
    return {part: int(n) for part, n in re.findall(r"`(part\d)/`: (\d+) frames", text)}


def test_boot_animation_generator_run_from_the_command_line_matches_the_specification(tmp_path):
    pytest.importorskip("PIL")
    import math
    import zipfile
    from io import BytesIO
    from PIL import Image
    out = tmp_path / "bootanimation.zip"
    proc = subprocess.run(["python3", str(OS_DIR / "scripts" / "make_bootanimation.py"), str(out)],
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    assert out.exists() and proc.stdout.startswith("wrote ")
    desc = (PRODUCT / "bootanimation" / "desc.txt").read_text(encoding="utf-8")
    width, height, fps = (int(v) for v in desc.splitlines()[0].split())
    parts = {ln.split()[3]: ln.split()[0] for ln in desc.splitlines()[1:] if ln.strip()}
    assert parts == {"part0": "c", "part1": "p"}
    counts = _readme_frame_counts()
    assert counts == {"part0": fps, "part1": 2 * fps}, "README's frame counts are one and two seconds at desc.txt's rate"
    with zipfile.ZipFile(out) as z:
        infos = z.infolist()
        assert infos[0].filename == "desc.txt"
        for part, n in counts.items():
            frames = [i for i in infos if i.filename.startswith(part + "/")]
            assert len(frames) == n, (part, len(frames))
            for i in frames:
                assert i.compress_type == zipfile.ZIP_STORED and i.compress_size == i.file_size
                assert i.filename.endswith(".png")
        # The mark stays inside the 320-pixel safe circle README promises: outside radius 165 all is black.
        for name in ("part0/%05d.png" % (fps - 1), "part1/%05d.png" % (fps // 2 + 1)):
            img = Image.open(BytesIO(z.read(name)))
            assert img.size == (width, height) and img.mode == "RGB"
            cx = cy = width / 2
            for deg in range(0, 360, 15):
                x, y = int(cx + 166 * math.cos(math.radians(deg))), int(cy + 166 * math.sin(math.radians(deg)))
                assert img.getpixel((x, y)) == (0, 0, 0), (name, deg)
            assert img.getpixel((width // 2, height // 2)) != (0, 0, 0), "the mark's dot is drawn at the centre"
    if shutil.which("unzip"):
        listing = subprocess.run(["unzip", "-v", str(out)], capture_output=True, text=True, timeout=60).stdout
        entries = [ln for ln in listing.splitlines() if ln.strip().endswith((".png", "desc.txt"))]
        assert entries and all(" Stored " in ln for ln in entries), "unzip -v must show Stored on every entry"
    # The product installs it where the platform's bootanimation reads first.
    dests = [dst for src, dst in copy_file_sources((PRODUCT / "ira_watch_cf.mk").read_text(encoding="utf-8"))
             if src.endswith("bootanimation/bootanimation.zip")]
    assert dests == ["$(TARGET_COPY_OUT_PRODUCT)/media/bootanimation.zip"]
    assert "/product/media/" in REFERENCE["bootanimation_paths"] and "bootanimation.zip" in REFERENCE["bootanimation_paths"]


@pytest.mark.parametrize("bad_desc, message", [
    ("454 454 30\nx 1 0 part0\np 0 0 part1\n", "TYPE c, p or f"),
    ("454 454 30\nc 1 0 part0\np 0 0 part2\n", "names parts"),
    ("454 454\nc 1 0 part0\n", "WIDTH HEIGHT FPS"),
    ("454 400 30\nc 1 0 part0\np 0 0 part1\n", "must be square"),
])
def test_negative_control_the_generator_refuses_a_desc_the_platform_would_not_play(tmp_path, bad_desc, message):
    pytest.importorskip("PIL")
    desc = tmp_path / "desc.txt"
    desc.write_text(bad_desc)
    proc = subprocess.run(["python3", str(OS_DIR / "scripts" / "make_bootanimation.py"), str(tmp_path / "out.zip"),
                           "--desc", str(desc)], capture_output=True, text=True, timeout=300)
    assert proc.returncode != 0 and message in proc.stderr, proc.stderr
    assert not (tmp_path / "out.zip").exists()


# --------------------------------------------------------------------------- sensors HAL (E3.a, E3.c-e)

def hal_sensor_table(src: str) -> dict[str, dict[str, object]]:
    """{type: {min_delay_us, flags}} from the info(...) rows of SimSensorsHal.cpp."""
    text = _strip_bp_comments(src)
    rows = re.findall(r"info\(\s*\d+,\s*SensorType::(\w+),\s*\"[^\"]*\",\s*[^,]+,\s*[^,]+,\s*(\d+),\s*([^)]+)\)", text)
    return {t: {"min_delay_us": int(d), "flags": " ".join(f.split())} for t, d, f in rows}


def _flag_expression(expr: str) -> int:
    expr = expr.strip()
    m = re.fullmatch(r"SensorInfo::(SENSOR_FLAG_\w+)", expr)
    if m:
        return REFERENCE["sensors"]["flag_constants"][m.group(1)]
    if re.fullmatch(r"[\d\s<|]+", expr):
        return eval(expr, {"__builtins__": {}}, {})  # noqa: S307 - digits, shifts and ors only
    raise ValueError(f"{expr}: not a SensorInfo constant")


def hal_flag_values(src: str) -> dict[str, int]:
    """kContinuous, kOnChange and kWakeUp as the service defines them, resolved through the
    SensorInfo constants the AIDL interface declares (os/reference) or a literal expression."""
    text = _strip_bp_comments(src)
    values = {}
    for stmt in re.findall(r"constexpr int32_t ([^;]+);", text):
        for name, expr in re.findall(r"(k\w+) = ([^,]+)", stmt):
            values[name] = _flag_expression(expr)
    return values


def test_the_hal_service_declares_the_contract_sensors_with_the_contract_rates_and_modes():
    src = (SENSORS / "SimSensorsHal.cpp").read_text(encoding="utf-8")
    table = hal_sensor_table(src)
    types = REFERENCE["sensors"]["types"]
    assert set(table) == {"HEART_RATE", "ACCELEROMETER", "GYROSCOPE", "LOW_LATENCY_OFFBODY_DETECT"}
    assert set(table) <= set(types), "every declared type is an AIDL SensorType"
    assert table["HEART_RATE"]["min_delay_us"] == 1_000_000, "1 Hz while worn"
    assert table["ACCELEROMETER"]["min_delay_us"] == table["GYROSCOPE"]["min_delay_us"] == 20_000, "50 Hz"
    assert table["LOW_LATENCY_OFFBODY_DETECT"]["min_delay_us"] == 0, "on-change sensors report 0"
    flags = hal_flag_values(src)
    consts = REFERENCE["sensors"]["flag_constants"]
    assert flags["kOnChange"] == consts["SENSOR_FLAG_BITS_ON_CHANGE_MODE"] == 2
    assert flags["kContinuous"] == consts["SENSOR_FLAG_BITS_CONTINUOUS_MODE"] == 0
    assert flags["kWakeUp"] == consts["SENSOR_FLAG_BITS_WAKE_UP"] == 1
    assert table["ACCELEROMETER"]["flags"] == table["GYROSCOPE"]["flags"] == "kContinuous"
    assert table["HEART_RATE"]["flags"] == "kOnChange"
    assert table["LOW_LATENCY_OFFBODY_DETECT"]["flags"] == "kOnChange | kWakeUp", "off-body must wake the device"
    assert 'kVendor[] = "IRA simulated"' in src
    contract = (ROOT / "docs" / "hardware" / "SENSOR_HAL.md").read_text(encoding="utf-8")
    for t in ("TYPE_HEART_RATE", "TYPE_ACCELEROMETER", "TYPE_LOW_LATENCY_OFFBODY_DETECT"):
        assert t in contract
    assert "wake-up" in contract and "m/s²" in contract and "beats per minute" in contract
    model = (SENSORS / "sim_signals.h").read_text(encoding="utf-8")
    assert "kGravity = 9.80665" in model and "kNoContact = -1" in model
    scenarios = set(re.findall(r'name == "(\w+)"', src)) | {"resting"}
    readme = (SENSORS / "README.md").read_text(encoding="utf-8")
    assert scenarios == {"resting", "walking", "fall", "off_wrist"} and all(f"`{s}`" in readme for s in scenarios)


def test_negative_control_the_former_one_shot_flag_value_is_caught():
    src = (SENSORS / "SimSensorsHal.cpp").read_text(encoding="utf-8")
    old = re.sub(r"constexpr int32_t kContinuous = [^;]+;[^\n]*\s*constexpr int32_t kOnChange = [^;]+;[^\n]*\s*"
                 r"constexpr int32_t kWakeUp = [^;]+;[^\n]*",
                 "constexpr int32_t kContinuous = 0, kOnChange = 2 << 1, kWakeUp = 1;", src, flags=re.S)
    assert old != src
    flags = hal_flag_values(old)
    assert flags["kOnChange"] == 4 == REFERENCE["sensors"]["flag_constants"]["SENSOR_FLAG_BITS_ONE_SHOT_MODE"]
    assert flags["kOnChange"] != REFERENCE["sensors"]["flag_constants"]["SENSOR_FLAG_BITS_ON_CHANGE_MODE"]


def test_the_sensors_hal_version_agrees_between_android_bp_vintf_and_the_aosp_default():
    bp = _strip_bp_comments((SENSORS / "Android.bp").read_text(encoding="utf-8"))
    (lib,) = re.findall(r'"android\.hardware\.sensors-V(\d+)-ndk"', bp)
    fragments = re.findall(r'vintf_fragments:\s*\[\s*"([^"]+)"', bp)
    rcs = re.findall(r'init_rc:\s*\[\s*"([^"]+)"', bp)
    assert len(fragments) == 1 and len(rcs) == 1
    vintf = ET.parse(SENSORS / fragments[0]).getroot()
    (hal,) = vintf.findall("hal")
    assert hal.attrib["format"] == "aidl" and hal.find("name").text == "android.hardware.sensors"
    assert int(hal.find("version").text) == int(lib) == REFERENCE["sensors"]["default_service_vintf"]["version"]
    assert hal.find("fqname").text == REFERENCE["sensors"]["default_service_vintf"]["fqname"] == "ISensors/default"
    assert f"android.hardware.sensors-V{lib}-ndk" in REFERENCE["sensors"]["default_service_ndk_libs"]
    (name,) = re.findall(r'name:\s*"(android\.hardware\.sensors-service\.ira-sim)"', bp)
    rc = (SENSORS / rcs[0]).read_text(encoding="utf-8")
    assert re.search(rf"^service vendor\.sensors-ira-sim /vendor/bin/hw/{re.escape(name)}$", rc, flags=re.M)
    assert 'relative_install_path: "hw"' in bp and "vendor: true" in bp
    assert 'SimSensors::descriptor + "/default"' in (SENSORS / "SimSensorsHal.cpp").read_text(encoding="utf-8")


def test_negative_control_a_vintf_version_that_differs_from_the_library_is_caught(tmp_path):
    copy = tmp_path / "sensors_sim"
    shutil.copytree(SENSORS, copy)
    frag = copy / "android.hardware.sensors-ira-sim.xml"
    frag.write_text(frag.read_text(encoding="utf-8").replace("<version>3</version>", "<version>2</version>"))
    bp = _strip_bp_comments((copy / "Android.bp").read_text(encoding="utf-8"))
    (lib,) = re.findall(r'"android\.hardware\.sensors-V(\d+)-ndk"', bp)
    assert int(ET.parse(frag).getroot().find("hal/version").text) != int(lib)


# --------------------------------------------------------------------------- build and launch scripts (E2.g, E6.a)

def _fenced_sh_lines(text: str) -> list[str]:
    blocks = re.findall(r"```sh\n(.*?)```", text, flags=re.S)
    return [ln.strip() for b in blocks for ln in b.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _doc_scripts(text: str) -> set[str]:
    return {m.group(1) for ln in _fenced_sh_lines(text) for m in [re.search(r"bash (os/scripts/\S+)", ln)] if m}


def test_build_doc_commands_name_scripts_and_variables_that_exist():
    text = (DOCS / "BUILD.md").read_text(encoding="utf-8")
    scripts = _doc_scripts(text)
    assert scripts == {"os/scripts/build_cuttlefish.sh", "os/scripts/launch_cuttlefish.sh"}
    for s in scripts:
        assert (ROOT / s).exists(), s
        assert subprocess.run(["bash", "-n", str(ROOT / s)], capture_output=True).returncode == 0, f"{s} has a syntax error"
    build, launch = BUILD_SCRIPT.read_text(encoding="utf-8"), LAUNCH_SCRIPT.read_text(encoding="utf-8")
    for var in set(re.findall(r"\b(IRA_OS_\w+|IRA_CF_\w+)\b", text)):
        assert var in build or var in launch, f"{var} is in BUILD.md but no script reads it"
    for ln in _fenced_sh_lines(text):
        m = re.match(r"export (\w+)=", ln)
        if m:
            assert m.group(1) in build, ln
    # The older display-flag form BUILD.md offers is accepted by the launch script.
    env = {**os.environ, "IRA_OS_PRINT_ONLY": "1", "IRA_CF_DISPLAY_FLAGS": "--x_res=454 --y_res=454 --dpi=320"}
    proc = subprocess.run(["bash", str(LAUNCH_SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0 and "--x_res=454 --y_res=454 --dpi=320" in proc.stdout
    assert "--display0" not in proc.stdout


def test_negative_control_a_doc_command_naming_a_missing_script_is_caught():
    text = (DOCS / "BUILD.md").read_text(encoding="utf-8").replace("os/scripts/launch_cuttlefish.sh", "os/scripts/start_cvd.sh")
    scripts = _doc_scripts(text)
    assert "os/scripts/start_cvd.sh" in scripts and not (ROOT / "os/scripts/start_cvd.sh").exists()


def _this_machine() -> tuple[int, int, str]:
    mem_kb = int(re.search(r"^MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text(), flags=re.M).group(1))
    return mem_kb * 1024 // 1_000_000_000, os.cpu_count() or 0, os.uname().machine


def test_build_script_preflight_measures_this_machine_and_refuses_when_it_is_short(tmp_path):
    ram, cpus, arch = _this_machine()
    disk = shutil.disk_usage(tmp_path).free // 1_000_000_000
    short = disk < 400 or ram < 64 or cpus < 8 or arch != "x86_64"
    proc, marker, aosp_dir = run_build_script(tmp_path)
    assert proc.returncode == (2 if short else 3), (disk, ram, cpus, arch, proc.stdout, proc.stderr)
    assert f"memory:    {ram} GB" in proc.stdout and f"{cpus} cores" in proc.stdout
    if short:
        assert "REFUSED: this machine cannot build AOSP." in proc.stderr
    assert not marker.exists() and not aosp_dir.exists()


def test_launch_script_refuses_without_kvm_or_the_host_tools(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("IRA_OS_PRINT_ONLY", "IRA_CF_DISPLAY_FLAGS")}
    # PATH holds only an empty directory, so launch_cvd cannot be found whatever this host has.
    env.update({"ANDROID_PRODUCT_OUT": str(tmp_path / "out/target/product/vsoc_x86_64_only"), "PATH": str(tmp_path)})
    proc = subprocess.run([shutil.which("bash") or "/bin/bash", str(LAUNCH_SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 4 and proc.stderr.startswith("CANNOT LAUNCH:"), proc.stderr
    kvm = Path("/dev/kvm")
    if not kvm.exists():
        assert "/dev/kvm is missing" in proc.stderr
    elif not os.access(kvm, os.R_OK | os.W_OK):
        assert "/dev/kvm is not accessible" in proc.stderr
    else:
        assert "launch_cvd is not on PATH" in proc.stderr
    assert "Launching:" not in proc.stdout


def test_launch_script_refuses_a_non_cuttlefish_product(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("IRA_OS_PRINT_ONLY",)}
    env["ANDROID_PRODUCT_OUT"] = str(tmp_path / "out/target/product/generic_x86_64")
    proc = subprocess.run(["bash", str(LAUNCH_SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 4 and "is not a Cuttlefish x86_64 output" in proc.stderr


# --------------------------------------------------------------------------- supplier register (E6.b)

STATUS_WORDS = {"not obtained", "requested", "under NDA", "obtained", "not needed"}


def register_rows(text: str) -> list[tuple[str, str]]:
    """(id, status) per component row of the supplier register's table."""
    rows = []
    for line in text.splitlines():
        if line.startswith("| Q"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            rows.append((cells[0], cells[-1]))
    return rows


def test_supplier_register_has_fourteen_rows_each_with_a_status_from_the_fixed_set():
    text = (DOCS / "QUALCOMM_DEPENDENCIES.md").read_text(encoding="utf-8")
    header = next(line for line in text.splitlines() if line.startswith("| #"))
    assert header.rstrip().endswith("| Status |")
    rows = register_rows(text)
    assert [r[0] for r in rows] == [f"Q{i}" for i in range(1, 15)]
    assert all(status in STATUS_WORDS for _, status in rows), rows
    for word in sorted(STATUS_WORDS):
        assert f"`{word}`" in text, f"the register must list the status word {word}"
    assert all(status == "not obtained" for _, status in rows), "nothing has been obtained; a change here needs the owner's record"


def test_negative_control_a_status_outside_the_set_is_caught():
    text = (DOCS / "QUALCOMM_DEPENDENCIES.md").read_text(encoding="utf-8")
    bad = text.replace("| not obtained |", "| ordered |", 1)
    rows = register_rows(bad)
    assert [s for _, s in rows if s not in STATUS_WORDS] == ["ordered"]


# --------------------------------------------------------------------------- the reference itself

def test_the_aosp_reference_is_well_formed_and_says_what_it_did_not_check():
    assert REFERENCE["sources"] and REFERENCE["unchecked"]
    assert any("cuttlefish" in u for u in REFERENCE["unchecked"]), "the inherited Cuttlefish makefile could not be checked"
    assert len(REFERENCE["config_xml_resources"]) > 1000 and len(REFERENCE["permission_protection_levels"]) == len(PROTECTION_LEVEL)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", REFERENCE["written"])
    script = (OS_DIR / "scripts" / "aosp_reference.py").read_text(encoding="utf-8")
    assert "android.googlesource.com is not reachable" in script
