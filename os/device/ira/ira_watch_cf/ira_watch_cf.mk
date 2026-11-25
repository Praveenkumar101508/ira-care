#
# IRA CARE OS on a Cuttlefish virtual device: the product definition.
#
# UNBUILT. Configuration only. No image has been built from this file and nothing has
# booted from it: the build needs a host with about 400 GB free disk and 64 GB RAM
# (source.android.com), which this repository's CI does not have. docs/os/BUILD.md.
#
# Copyright 2026 IRA CARE. Licensed under the Apache License, Version 2.0. The files this
# makefile inherits are part of the Android Open Source Project, also Apache 2.0; their own
# licence headers stay with them in the AOSP checkout.
#
# What this product is: the stock Cuttlefish x86_64 phone target, with IRA's watch app
# preinstalled as a privileged app, IRA's framework overlay, watch-sized display settings
# applied at launch (os/scripts/launch_cuttlefish.sh) and British English by default. It
# is a development and demonstration image, not the production watch OS: that needs the
# chip vendor's board support package (docs/os/QUALCOMM_DEPENDENCIES.md).
#

# ---------------------------------------------------------------------------
# 1. The base: Cuttlefish x86_64.
#
# UNCERTAIN PATH. Different AOSP revisions name the 64-bit x86 Cuttlefish phone product
# differently:
#   device/google/cuttlefish/vsoc_x86_64_only/phone/aosp_cf.mk  (aosp_cf_x86_64_only_phone,
#       PRODUCT_DEVICE vsoc_x86_64_only; seen in AOSP main snapshots in 2024 and 2025)
#   device/google/cuttlefish/vsoc_x86_64/phone/aosp_cf.mk       (aosp_cf_x86_64_phone,
#       PRODUCT_DEVICE vsoc_x86_64; the older name, still in the public custom-device guide)
# Later AOSP main snapshots list only an "_hsum" (headless system user) variant in
# COMMON_LUNCH_CHOICES, so the plain file may move again. The path below could not be
# checked against a checkout here. os/scripts/build_cuttlefish.sh stops before building
# if it is missing and prints the alternative to try.
#
# Cuttlefish also has a wear target in some revisions (vsoc_x86/wear, 32-bit, and later
# variants). It is not used: it pulls in Wear OS expectations, and IRA's watch is plain AOSP
# with no Wear OS licence (docs/DECISIONS.md D-059).
# ---------------------------------------------------------------------------
$(call inherit-product, device/google/cuttlefish/vsoc_x86_64_only/phone/aosp_cf.mk)

# ---------------------------------------------------------------------------
# 2. Identity. These override the values the inherited product set.
#
# PRODUCT_DEVICE stays the Cuttlefish device, so the build uses Cuttlefish's own
# BoardConfig.mk (device/google/cuttlefish/vsoc_x86_64_only/BoardConfig.mk). This product
# therefore needs no BoardConfig of its own: it changes no partition, kernel or board
# setting. If the inherit path above is switched to vsoc_x86_64, change this line with it.
# ---------------------------------------------------------------------------
PRODUCT_NAME := ira_watch_cf
PRODUCT_DEVICE := vsoc_x86_64_only
PRODUCT_BRAND := IRA
PRODUCT_MANUFACTURER := IRA CARE
PRODUCT_MODEL := IRA CARE Watch (Cuttlefish)

# The Cuttlefish custom-device guide sets these two so the SoC fields report the product.
PRODUCT_VENDOR_PROPERTIES += \
    ro.soc.manufacturer=$(PRODUCT_MANUFACTURER) \
    ro.soc.model=$(PRODUCT_DEVICE)

# The board-info file names which predefined Cuttlefish host configuration the legacy
# launch_cvd starts by default. The public custom-device guide sets TARGET_BOARD_INFO_FILE in
# the product makefile this way. "phone" matches the inherited product; the watch-sized
# display is then set by flags in os/scripts/launch_cuttlefish.sh. UNCERTAIN: whether the
# host package offers a "wear" configuration usable here was not checked.
TARGET_BOARD_INFO_FILE := device/ira/ira_watch_cf/android-info.txt

# ---------------------------------------------------------------------------
# 3. Language and time. The first locale in PRODUCT_LOCALES is the default.
# ---------------------------------------------------------------------------
PRODUCT_LOCALES := en_GB
PRODUCT_PRODUCT_PROPERTIES += \
    persist.sys.timezone=Europe/London

# ---------------------------------------------------------------------------
# 4. Display. A round watch panel of 454 x 454 pixels.
#
# On Cuttlefish the display size and density are launch-time settings of the virtual
# device (launch_cvd), not build properties, so they live in os/scripts/launch_cuttlefish.sh
# and ro.sf.lcd_density is deliberately NOT set here (it would fight the launcher's value).
#
# Density: a 454-pixel panel about 1.39 inches across is about 327 pixels per inch, which
# Android buckets as xhdpi: 320 dpi. The candidate panel in docs/hardware/COMPONENTS.md is
# 466 x 466 at 1.43 inches (about 326 ppi), the same bucket. The round shape is told to the
# framework by the overlay (overlays/IraFrameworkOverlay); Cuttlefish itself draws a square.
#
# The app declares uses-feature android.hardware.type.watch. The framework sets the watch UI
# mode from that system feature, so the image declares it. UNCERTAIN: the inherited phone
# product already copies handheld_core_hardware.xml; declaring both handheld and watch is
# unusual and may need the handheld file removed on the build host.
# ---------------------------------------------------------------------------
PRODUCT_COPY_FILES += \
    frameworks/native/data/etc/android.hardware.type.watch.xml:$(TARGET_COPY_OUT_VENDOR)/etc/permissions/android.hardware.type.watch.xml

# ---------------------------------------------------------------------------
# 5. IRA's own modules (defined in Android.bp next to this file).
# ---------------------------------------------------------------------------
PRODUCT_PACKAGES += \
    IraCareWatch \
    IraFrameworkOverlay

# Privileged apps must be allowlisted, and a boot with a privileged permission that is not
# allowlisted fails. Enforcement is the AOSP default for new devices; it is stated here so
# a change to the allowlist cannot be skipped quietly.
PRODUCT_PRODUCT_PROPERTIES += \
    ro.control_privapp_permissions=enforce

# ---------------------------------------------------------------------------
# 6. Boot animation. desc.txt and README.md in bootanimation/ are the specification;
# os/scripts/make_bootanimation.py renders the frames and bootanimation.zip from them, and
# os/scripts/build_cuttlefish.sh runs it into the AOSP checkout before the build (the zip is
# never committed). NOT YET SHOWN: no image has booted, so the animation has not been seen at boot.
# ---------------------------------------------------------------------------
PRODUCT_COPY_FILES += \
    device/ira/ira_watch_cf/bootanimation/bootanimation.zip:$(TARGET_COPY_OUT_PRODUCT)/media/bootanimation.zip

# ---------------------------------------------------------------------------
# 7. Home screen. IRA is the only home app: the watch app's aosp flavour declares the HOME
# filter (HOME_LAUNCHER.md, #60), and Android.bp's IraCareWatch module overrides the stock
# launcher so it is not installed. UNCERTAIN: the launcher's module name (Launcher3QuickStep in
# recent AOSP) must be checked on the build host; Soong reports an unknown override as an error.
# ---------------------------------------------------------------------------
