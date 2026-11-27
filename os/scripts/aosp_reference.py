#!/usr/bin/env python3
"""Snapshot the AOSP facts the host-side checks of os/ compare against (docs/os/HOST_CHECKS.md).

The product layer in os/ names things that live in the Android Open Source Project: resource
names a framework overlay replaces, protection levels of the permissions the watch app requests,
the Sensors HAL's flag constants and version, the stock launcher's module name, where the boot
animation is read from. None of that can be checked against an AOSP checkout here (none exists,
and android.googlesource.com is not reachable from the build machine), so this script reads the
public Git mirrors over HTTPS and writes what it found to os/reference/aosp_reference.json. The
readiness tests then check os/ against that file, offline and deterministically.

Sources, all Apache 2.0, read by raw file URL:
  frameworks/base and build/make: github.com/aosp-mirror, branch android16-release (the release
      android-latest-release pointed at when this was written);
  frameworks/native and hardware/interfaces: not on aosp-mirror; read from LineageOS's mirror,
      branch lineage-22.2 (Android 15). Marked as such in the file.
  device/google/cuttlefish and packages/apps/Launcher3: no public mirror reachable; recorded as
      unchecked.

Only names, types and constants are copied, never code. Rerun when the pinned branch changes:

    python3 os/scripts/aosp_reference.py            # writes os/reference/aosp_reference.json
    python3 os/scripts/aosp_reference.py --check    # exit 1 if the file would change
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "reference" / "aosp_reference.json"
RAW = "https://raw.githubusercontent.com"
BASE_BRANCH = "android16-release"
NATIVE_BRANCH = "lineage-22.2"

SOURCES = {
    "config_xml": f"{RAW}/aosp-mirror/platform_frameworks_base/{BASE_BRANCH}/core/res/res/values/config.xml",
    "core_manifest": f"{RAW}/aosp-mirror/platform_frameworks_base/{BASE_BRANCH}/core/res/AndroidManifest.xml",
    "default_permission_policy": f"{RAW}/aosp-mirror/platform_frameworks_base/{BASE_BRANCH}/services/core/java/com/android/server/pm/permission/DefaultPermissionGrantPolicy.java",
    "bootanimation": f"{RAW}/aosp-mirror/platform_frameworks_base/{BASE_BRANCH}/cmds/bootanimation/BootAnimation.cpp",
    "handheld_system_ext": f"{RAW}/aosp-mirror/platform_build/{BASE_BRANCH}/target/product/handheld_system_ext.mk",
    "envsetup": f"{RAW}/aosp-mirror/platform_build/{BASE_BRANCH}/envsetup.sh",
    "sensor_info_aidl": f"{RAW}/LineageOS/android_hardware_interfaces/{NATIVE_BRANCH}/sensors/aidl/android/hardware/sensors/SensorInfo.aidl",
    "sensor_type_aidl": f"{RAW}/LineageOS/android_hardware_interfaces/{NATIVE_BRANCH}/sensors/aidl/android/hardware/sensors/SensorType.aidl",
    "sensors_default_bp": f"{RAW}/LineageOS/android_hardware_interfaces/{NATIVE_BRANCH}/sensors/aidl/default/Android.bp",
    "sensors_default_vintf": f"{RAW}/LineageOS/android_hardware_interfaces/{NATIVE_BRANCH}/sensors/aidl/default/sensors-default.xml",
}
# Files the product layer might copy from frameworks/native/data/etc. Each is probed; the ones
# that are not there are recorded, because PRODUCT_COPY_FILES fails the build on a missing source.
NATIVE_ETC_PROBES = [
    "android.hardware.type.watch.xml",
    "wearable_core_hardware.xml",
    "handheld_core_hardware.xml",
    "android.hardware.sensor.heartrate.xml",
    "android.hardware.sensor.accelerometer.xml",
    "android.hardware.sensor.gyroscope.xml",
    "android.hardware.location.gps.xml",
]
PERMISSIONS = [
    "INTERNET", "FOREGROUND_SERVICE", "FOREGROUND_SERVICE_HEALTH", "POST_NOTIFICATIONS", "RECORD_AUDIO",
    "VIBRATE", "WAKE_LOCK", "BODY_SENSORS", "BODY_SENSORS_BACKGROUND", "ACTIVITY_RECOGNITION",
    "RECEIVE_BOOT_COMPLETED", "ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION", "ACCESS_NETWORK_STATE",
    "REBOOT", "WRITE_SECURE_SETTINGS", "MANAGE_USERS", "STATUS_BAR",
]


def fetch(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def config_resources(text: str) -> dict[str, str]:
    return {name: kind for kind, name in re.findall(
        r"<(bool|integer|string|dimen|fraction|color|integer-array|string-array|array)\s+name=\"([^\"]+)\"", text)}


def protection_levels(text: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r"<permission\s+(.*?)/?>", text, flags=re.S):
        attrs = m.group(1)
        name = re.search(r'android:name="(android\.permission\.\w+)"', attrs)
        level = re.search(r'android:protectionLevel="([^"]+)"', attrs)
        if name and level and name.group(1).rsplit(".", 1)[1] in PERMISSIONS:
            out[name.group(1)] = level.group(1)
    return out


def aidl_constants(text: str, prefix: str) -> dict[str, int]:
    return {k: int(v, 0) for k, v in re.findall(rf"const int ({prefix}\w+) = (0x[0-9A-Fa-f]+|\d+);", text)}


def sensor_types(text: str) -> dict[str, int]:
    return {k: int(v) for k, v in re.findall(r"^\s*([A-Z_0-9]+) = (\d+),", text, flags=re.M)}


def build() -> dict:
    texts = {}
    for key, url in SOURCES.items():
        status, body = fetch(url)
        if status != 200:
            raise SystemExit(f"{url}: HTTP {status}")
        texts[key] = body
    native_etc = {}
    for name in NATIVE_ETC_PROBES:
        status, _ = fetch(f"{RAW}/LineageOS/android_frameworks_native/{NATIVE_BRANCH}/data/etc/{name}")
        native_etc[name] = status == 200
    sensors_bp = "\n".join(ln.split("//", 1)[0] for ln in texts["sensors_default_bp"].splitlines())
    ndk_libs = sorted(set(re.findall(r'"(android\.hardware\.sensors-V\d+-ndk)"', sensors_bp)))
    vintf_version = re.search(r"<version>(\d+)</version>", texts["sensors_default_vintf"]).group(1)
    vintf_fqname = re.search(r"<fqname>([^<]+)</fqname>", texts["sensors_default_vintf"]).group(1)
    system_ext_packages = re.findall(r"^\s+([A-Za-z0-9_.]+)\s*\\?$", texts["handheld_system_ext"], flags=re.M)
    bootanim_paths = re.findall(r'static const char \w*BOOTANIMATION\w*\[\] = "([^"]+)";', texts["bootanimation"])
    policy = texts["default_permission_policy"]
    xml_names = dict(re.findall(r'private static final String (TAG_\w+|ATTR_\w+) = "(\w[\w-]*)";', policy))
    policy_dirs = re.findall(r"Environment\.get(\w+)Directory\(\), \"etc/default-permissions\"", policy)
    default_release = re.search(r'^\s*(?:local )?release="?(trunk_staging)"?\s*$', texts["envsetup"], flags=re.M)
    return {
        "written": dt.date.today().isoformat(),
        "how": "os/scripts/aosp_reference.py; names and constants only, read from public Git mirrors",
        "sources": {
            "frameworks/base, build/make": f"github.com/aosp-mirror, branch {BASE_BRANCH}",
            "frameworks/native, hardware/interfaces": f"github.com/LineageOS, branch {NATIVE_BRANCH} (Android 15)",
            "device/google/cuttlefish, packages/apps/Launcher3": "no public mirror reachable; unchecked",
        },
        "unchecked": [
            "device/google/cuttlefish/vsoc_x86_64_only/phone/aosp_cf.mk (the inherited product makefile)",
            "the release config name in COMMON_LUNCH_CHOICES for the checked-out branch",
            "that `overrides` on a product-partition android_app_import removes a system_ext package",
        ],
        "config_xml_resources": config_resources(texts["config_xml"]),
        "permission_protection_levels": protection_levels(texts["core_manifest"]),
        "default_permissions_xml": {"tags_and_attributes": xml_names, "directories_read": policy_dirs},
        "bootanimation_paths": bootanim_paths,
        "system_ext_packages": sorted(set(system_ext_packages)),
        "lunch_default_release": default_release.group(1) if default_release else None,
        "sensors": {
            "flag_constants": aidl_constants(texts["sensor_info_aidl"], "SENSOR_FLAG_"),
            "types": sensor_types(texts["sensor_type_aidl"]),
            "default_service_ndk_libs": ndk_libs,
            "default_service_vintf": {"version": int(vintf_version), "fqname": vintf_fqname},
        },
        "frameworks_native_data_etc_present": native_etc,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed file differs (the date is ignored)")
    args = ap.parse_args(argv)
    data = build()
    text = json.dumps(data, indent=1, sort_keys=True) + "\n"
    if args.check:
        old = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
        old.pop("written", None)
        new = dict(data)
        new.pop("written", None)
        if old != new:
            print(f"{OUT} is out of date; rerun without --check")
            return 1
        print(f"{OUT} matches the mirrors")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT} ({len(data['config_xml_resources'])} framework resources, "
          f"{len(data['permission_protection_levels'])} permissions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
