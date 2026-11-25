#!/usr/bin/env bash
#
# Launch the IRA CARE OS Cuttlefish image with a watch-sized display.
#
# NEVER RUN AGAINST A REAL IMAGE. No image has been built (docs/os/BUILD.md). This script is
# written from the Cuttlefish documentation on source.android.com and is unverified.
#
# Copyright 2026 IRA CARE. Licensed under the Apache License, Version 2.0.
#
# Run it from the shell that ran `lunch` for ira_watch_cf (it needs ANDROID_PRODUCT_OUT and
# the Cuttlefish host tools on PATH), on a Linux x86_64 host with KVM.
#
# Display: one 454 x 454 panel at 320 dpi (xhdpi), 60 Hz. The flag form
#   --display0=width=...,height=...,dpi=...,refresh_rate_hz=...
# is in launch_cvd's own help text. Older host packages use --x_res, --y_res and --dpi
# instead, and newer ones prefer `cvd create --display ...`. Set IRA_CF_DISPLAY_FLAGS to the
# form your host package accepts if the default is rejected. UNCERTAIN which form the host
# package of the checked-out release accepts.
#
# Environment:
#   IRA_CF_CPUS        virtual CPUs (default 4)
#   IRA_CF_MEMORY_MB   guest memory in MB (default 4096; a watch has 1 to 2 GB, but the
#                      phone-based image needs more to boot comfortably)
#   IRA_CF_DISPLAY_FLAGS  replaces the display flag(s) entirely
#   IRA_OS_PRINT_ONLY=1   print the command and exit without checking or launching anything
#
# Nothing is downloaded by this script.

set -euo pipefail

readonly WIDTH=454
readonly HEIGHT=454
readonly DPI=320
readonly REFRESH_HZ=60

display_flags="${IRA_CF_DISPLAY_FLAGS:---display0=width=${WIDTH},height=${HEIGHT},dpi=${DPI},refresh_rate_hz=${REFRESH_HZ}}"
cmd=(launch_cvd --daemon
     --cpus="${IRA_CF_CPUS:-4}"
     --memory_mb="${IRA_CF_MEMORY_MB:-4096}"
     $display_flags)

if [[ "${IRA_OS_PRINT_ONLY:-}" == "1" ]]; then
  printf '%s\n' "${cmd[*]}"
  exit 0
fi

fail() { printf 'CANNOT LAUNCH: %s\n' "$*" >&2; exit 4; }

[[ -n "${ANDROID_PRODUCT_OUT:-}" ]] || fail "ANDROID_PRODUCT_OUT is not set. Run 'source build/envsetup.sh && lunch ira_watch_cf-...' first (docs/os/BUILD.md)."
case "$ANDROID_PRODUCT_OUT" in
  */vsoc_x86_64_only|*/vsoc_x86_64) ;;
  *) fail "ANDROID_PRODUCT_OUT ($ANDROID_PRODUCT_OUT) is not a Cuttlefish x86_64 output." ;;
esac
[[ -e /dev/kvm ]] || fail "/dev/kvm is missing. Cuttlefish needs hardware virtualisation (KVM)."
[[ -r /dev/kvm && -w /dev/kvm ]] || fail "/dev/kvm is not accessible to this user (add the user to the kvm group)."
command -v launch_cvd >/dev/null 2>&1 || fail "launch_cvd is not on PATH. It comes with the Cuttlefish host package or the build's host output."

printf 'Launching: %s\n' "${cmd[*]}"
"${cmd[@]}"
printf 'Started. Connect with: adb devices; the web display is served by the host package (see docs/os/BUILD.md).\n'
