#!/usr/bin/env bash
#
# Build the IRA CARE OS Cuttlefish image (product ira_watch_cf).
#
# NEVER RUN TO COMPLETION. No host with the published resources has run this script; the
# repository's CI runs only its refusal paths (supracloud-jarvis/ira/tests/readiness/
# test_os_product_layer.py). Every step after the preflight is written from AOSP's public
# documentation and is unverified. See docs/os/BUILD.md.
#
# Copyright 2026 IRA CARE. Licensed under the Apache License, Version 2.0.
#
# What it does, in order:
#   1. Preflight: free disk, RAM, CPU architecture and cores against the minimums below.
#      Refuses (exit 2) if any is short. Nothing is downloaded.
#   2. Without IRA_OS_I_HAVE_THE_RESOURCES=1: prints the plan and stops (exit 3). Nothing is
#      downloaded, synced or built.
#   3. With it: syncs AOSP, builds or takes the signed watch APK, copies os/device/ira into
#      the checkout, and builds the image.
#
# Exit codes: 0 built; 2 refused, resources below the minimum; 3 not opted in (plan only);
#             4 a prerequisite is missing; 5 a test override was set together with the opt-in.
#
# Environment:
#   IRA_OS_I_HAVE_THE_RESOURCES=1   the only switch that allows any download, sync or build
#   IRA_OS_AOSP_DIR                 where the AOSP checkout lives (default: $HOME/aosp-ira)
#   IRA_OS_AOSP_BRANCH              manifest branch (default: android-latest-release)
#   IRA_OS_LUNCH                    lunch target (default: ira_watch_cf-trunk_staging-userdebug)
#   IRA_OS_WATCH_APK                a signed aospRelease APK to use instead of building one
#   IRA_OS_JOBS                     parallel jobs for repo sync (default: number of CPUs)
#
# Test-only overrides of the MEASURED values (never of the minimums). They exist so tests
# can exercise the refusal on any machine. They are refused together with the opt-in, so
# they cannot be used to talk the real build past the preflight:
#   IRA_OS_MEASURED_DISK_GB  IRA_OS_MEASURED_RAM_GB  IRA_OS_MEASURED_CPUS  IRA_OS_MEASURED_ARCH

set -euo pipefail

# --- Minimums --------------------------------------------------------------------------
# Published by source.android.com ("Hardware requirements"): a 64-bit x86 system, at least
# 400 GB free disk (about 250 GB for the checkout and 150 GB for the build) and at least
# 64 GB RAM. Figures in decimal gigabytes.
readonly MIN_DISK_GB=400
readonly MIN_RAM_GB=64
readonly REQUIRED_ARCH=x86_64
# NOT published by AOSP: the documentation names no minimum core count (Google's own build
# machines have 72 cores). 8 is IRA's floor, chosen because a full build on fewer cores
# takes the better part of a day. It is a judgement, labelled as one.
readonly MIN_CPUS=8

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
AOSP_DIR="${IRA_OS_AOSP_DIR:-$HOME/aosp-ira}"
AOSP_BRANCH="${IRA_OS_AOSP_BRANCH:-android-latest-release}"
LUNCH_TARGET="${IRA_OS_LUNCH:-ira_watch_cf-trunk_staging-userdebug}"
INHERITED_MK="device/google/cuttlefish/vsoc_x86_64_only/phone/aosp_cf.mk"
ALTERNATIVE_MK="device/google/cuttlefish/vsoc_x86_64/phone/aosp_cf.mk"

say() { printf '%s\n' "$*"; }
err() { printf '%s\n' "$*" >&2; }

is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }

overrides_in_use() {
  [[ -n "${IRA_OS_MEASURED_DISK_GB:-}${IRA_OS_MEASURED_RAM_GB:-}${IRA_OS_MEASURED_CPUS:-}${IRA_OS_MEASURED_ARCH:-}" ]]
}

# The nearest existing directory at or above the checkout path: that is the filesystem the
# checkout will be written to.
existing_ancestor() {
  local d="$1"
  while [[ ! -d "$d" ]]; do d="$(dirname "$d")"; done
  printf '%s' "$d"
}

measure_disk_gb() {
  if [[ -n "${IRA_OS_MEASURED_DISK_GB:-}" ]]; then printf '%s' "$IRA_OS_MEASURED_DISK_GB"; return; fi
  local kb
  kb="$(df -Pk "$(existing_ancestor "$AOSP_DIR")" | awk 'NR==2 {print $4}')"
  printf '%s' $(( kb * 1024 / 1000000000 ))
}

measure_ram_gb() {
  if [[ -n "${IRA_OS_MEASURED_RAM_GB:-}" ]]; then printf '%s' "$IRA_OS_MEASURED_RAM_GB"; return; fi
  local kb
  kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
  # MemTotal is a little below the installed memory (the kernel reserves some), but a
  # machine with 64 GiB installed still reports well over 64 decimal GB.
  printf '%s' $(( kb * 1024 / 1000000000 ))
}

measure_cpus() {
  if [[ -n "${IRA_OS_MEASURED_CPUS:-}" ]]; then printf '%s' "$IRA_OS_MEASURED_CPUS"; return; fi
  nproc
}

measure_arch() {
  if [[ -n "${IRA_OS_MEASURED_ARCH:-}" ]]; then printf '%s' "$IRA_OS_MEASURED_ARCH"; return; fi
  uname -m
}

preflight() {
  local disk ram cpus arch
  disk="$(measure_disk_gb)"; ram="$(measure_ram_gb)"; cpus="$(measure_cpus)"; arch="$(measure_arch)"
  for v in "$disk" "$ram" "$cpus"; do
    if ! is_uint "$v"; then err "REFUSED: a measured value is not a whole number: '$v'."; exit 2; fi
  done

  say "IRA CARE OS build preflight (checkout at $AOSP_DIR)"
  say "  free disk: ${disk} GB   (minimum ${MIN_DISK_GB} GB, published by source.android.com)"
  say "  memory:    ${ram} GB   (minimum ${MIN_RAM_GB} GB, published by source.android.com)"
  say "  CPU:       ${arch}, ${cpus} cores   (${REQUIRED_ARCH} required, published; ${MIN_CPUS} cores is IRA's floor, not published)"
  if overrides_in_use; then say "  (measured values overridden by IRA_OS_MEASURED_* for a test)"; fi

  local short=()
  (( disk < MIN_DISK_GB )) && short+=("free disk ${disk} GB is below ${MIN_DISK_GB} GB")
  (( ram < MIN_RAM_GB )) && short+=("memory ${ram} GB is below ${MIN_RAM_GB} GB")
  [[ "$arch" != "$REQUIRED_ARCH" ]] && short+=("CPU architecture ${arch} is not ${REQUIRED_ARCH}")
  (( cpus < MIN_CPUS )) && short+=("${cpus} CPU cores is below IRA's floor of ${MIN_CPUS}")

  if (( ${#short[@]} > 0 )); then
    err ""
    err "REFUSED: this machine cannot build AOSP."
    for s in "${short[@]}"; do err "  - $s"; done
    err ""
    err "A full AOSP build needs a 64-bit x86 host with at least ${MIN_DISK_GB} GB free disk and"
    err "${MIN_RAM_GB} GB RAM (source.android.com). Nothing was downloaded, synced or built."
    err "See docs/os/BUILD.md for the host this needs."
    exit 2
  fi
  say "  preflight passed"
}

print_plan() {
  say ""
  say "NOT OPTED IN: nothing will be downloaded, synced or built."
  say "Set IRA_OS_I_HAVE_THE_RESOURCES=1 to run these steps (each is unverified; docs/os/BUILD.md):"
  say "  1. repo init -u https://android.googlesource.com/platform/manifest -b ${AOSP_BRANCH}   (in ${AOSP_DIR})"
  say "  2. repo sync -c -j<cpus>                                    (about 250 GB)"
  say "  3. build or take the signed aospRelease watch APK, check it, copy it to"
  say "     device/ira/ira_watch_cf/prebuilt/IraCareWatch.apk"
  say "  4. copy os/device/ira to ${AOSP_DIR}/device/ira and render"
  say "     device/ira/ira_watch_cf/bootanimation/bootanimation.zip (os/scripts/make_bootanimation.py)"
  say "  5. check that ${INHERITED_MK} exists in the checkout"
  say "  6. source build/envsetup.sh && lunch ${LUNCH_TARGET} && m"
}

need() {
  command -v "$1" >/dev/null 2>&1 || { err "MISSING: '$1' is not on PATH. $2"; exit 4; }
}

prepare_watch_apk() {
  local apk dest="$AOSP_DIR/device/ira/ira_watch_cf/prebuilt/IraCareWatch.apk"
  if [[ -n "${IRA_OS_WATCH_APK:-}" ]]; then
    apk="$IRA_OS_WATCH_APK"
  else
    if [[ -z "${IRA_WATCH_KEYSTORE:-}" ]]; then
      err "MISSING: IRA_WATCH_KEYSTORE is not set. The image needs the release APK signed with"
      err "IRA's key (see watch/app/build.gradle.kts), or set IRA_OS_WATCH_APK to a signed APK."
      exit 4
    fi
    (cd "$REPO_ROOT/watch" && ./gradlew --no-daemon :app:assembleAospRelease)
    apk="$REPO_ROOT/watch/app/build/outputs/apk/aosp/release/app-aosp-release.apk"
  fi
  [[ -f "$apk" ]] || { err "MISSING: no APK at $apk (an unsigned build is named app-aosp-release-unsigned.apk and cannot be used)."; exit 4; }

  need aapt2 "It comes with the Android SDK build-tools."
  need apksigner "It comes with the Android SDK build-tools."
  apksigner verify "$apk" >/dev/null || { err "REFUSED: $apk is not signed."; exit 4; }
  local pkg
  pkg="$(aapt2 dump packagename "$apk")"
  [[ "$pkg" == "care.ira.watch" ]] || { err "REFUSED: package is '$pkg', not care.ira.watch (the debug build cannot be used)."; exit 4; }

  # The privapp allowlist is derived from the source manifest. Gradle's manifest merger can
  # add permissions from libraries; any the source manifest does not name must be classified
  # by hand before the image is built.
  local built source extra
  built="$(aapt2 dump permissions "$apk" | sed -n "s/.*uses-permission: name='\([^']*\)'.*/\1/p" | sort -u)"
  source="$(grep -o 'uses-permission android:name="[^"]*"' "$REPO_ROOT/watch/app/src/main/AndroidManifest.xml" | sed 's/.*name="\(.*\)"/\1/' | sort -u)"
  extra="$(comm -23 <(printf '%s\n' "$built") <(printf '%s\n' "$source") | grep -v '^$' || true)"
  if [[ -n "$extra" ]]; then
    say "The built APK requests permissions the source manifest does not name:"
    printf '  %s\n' $extra
    if [[ "${IRA_OS_EXTRA_PERMISSIONS_REVIEWED:-}" != "1" ]]; then
      err "STOPPED: classify each one (normal, dangerous or privileged). If any is privileged, add it to"
      err "os/device/ira/ira_watch_cf/permissions/privapp-permissions-care.ira.watch.xml. Then rerun with"
      err "IRA_OS_EXTRA_PERMISSIONS_REVIEWED=1."
      exit 4
    fi
  fi
  mkdir -p "$(dirname "$dest")"
  cp "$apk" "$dest"
  say "watch APK placed at $dest"
}

main() {
  preflight

  if [[ "${IRA_OS_I_HAVE_THE_RESOURCES:-}" != "1" ]]; then
    print_plan
    exit 3
  fi
  if overrides_in_use; then
    err "REFUSED: IRA_OS_MEASURED_* overrides are for tests only and cannot be combined with"
    err "IRA_OS_I_HAVE_THE_RESOURCES=1. Unset them and let the script measure this machine."
    exit 5
  fi

  need repo "Install it as described on source.android.com (Download the Android source)."
  need git ""
  need python3 ""

  mkdir -p "$AOSP_DIR"
  cd "$AOSP_DIR"
  if [[ ! -d .repo ]]; then
    repo init --partial-clone -u https://android.googlesource.com/platform/manifest -b "$AOSP_BRANCH"
  fi
  repo sync -c -j"${IRA_OS_JOBS:-$(nproc)}"

  rm -rf "$AOSP_DIR/device/ira"
  mkdir -p "$AOSP_DIR/device"
  cp -a "$REPO_ROOT/os/device/ira" "$AOSP_DIR/device/ira"
  prepare_watch_apk
  python3 -c 'import PIL' 2>/dev/null || { err "MISSING: Pillow (python3 -m pip install pillow) renders the boot animation."; exit 4; }
  python3 "$REPO_ROOT/os/scripts/make_bootanimation.py" "$AOSP_DIR/device/ira/ira_watch_cf/bootanimation/bootanimation.zip"

  if [[ ! -f "$AOSP_DIR/$INHERITED_MK" ]]; then
    err "STOPPED: $INHERITED_MK is not in this AOSP checkout."
    if [[ -f "$AOSP_DIR/$ALTERNATIVE_MK" ]]; then
      err "This checkout has $ALTERNATIVE_MK instead. Change the inherit-product line and"
      err "PRODUCT_DEVICE (vsoc_x86_64) in os/device/ira/ira_watch_cf/ira_watch_cf.mk, then rerun."
    fi
    exit 4
  fi

  # envsetup.sh and lunch read unset variables; relax nounset for them only.
  set +u
  # shellcheck disable=SC1091
  source build/envsetup.sh
  lunch "$LUNCH_TARGET"
  set -u
  m
  say "Build finished. Launch it with os/scripts/launch_cuttlefish.sh from this shell."
}

main "$@"
