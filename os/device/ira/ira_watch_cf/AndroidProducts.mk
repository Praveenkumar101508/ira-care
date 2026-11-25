#
# IRA CARE OS: product list for the Cuttlefish virtual watch.
#
# UNBUILT. This file has never been read by an AOSP build: no build host with the
# published minimum resources (about 400 GB free disk and 64 GB RAM, source.android.com)
# has run it. See docs/os/BUILD.md.
#
# Copyright 2026 IRA CARE. Licensed under the Apache License, Version 2.0, the licence
# of the Android Open Source Project this file is written for.
#
# Placement: this directory is copied to device/ira/ira_watch_cf/ inside an AOSP checkout
# (os/scripts/build_cuttlefish.sh does it). The AOSP build finds every AndroidProducts.mk
# under device/.
#

PRODUCT_MAKEFILES := \
    ira_watch_cf:$(LOCAL_DIR)/ira_watch_cf.mk

# The lunch triplet is <product>-<release config>-<variant>, the form AOSP uses since
# Android 14. "trunk_staging" is the release config of AOSP main; a release branch such as
# android-latest-release may name a different one. NOT VERIFIED against the branch that
# will be checked out: run `lunch` with no argument on the build host to list what exists.
COMMON_LUNCH_CHOICES := \
    ira_watch_cf-trunk_staging-userdebug \
    ira_watch_cf-trunk_staging-user
