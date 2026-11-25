# Settings and restrictions for a single-purpose care watch

**State: the policy logic is built and tested; nothing has run on a device.** The decisions
(which restrictions, the lock screen, administrator instructions) are pure Kotlin in
`watch/app/src/main/java/care/ira/watch/devicepolicy/DevicePolicy.kt`, proved on the JVM by
`DevicePolicyTest` (CI runs it in both flavours). The DevicePolicyManager wrapper
(`watch/app/src/aosp/.../DevicePolicyManagerTarget.kt`) is compiled by CI's aosp build but no
component registers it: becoming device owner is provisioning (E4.f), which needs an image.
The server signs administrator instructions with `supracloud-jarvis/ira/care/devicepolicy.py`
(ECDSA P-256, one device, at most 15 minutes, single-use); a cross-language fixture proves the
watch accepts exactly what the server signs. Not built: an administrator route that issues
instructions (it needs a deployment-administrator role a care workspace role must never imply).

The person wearing the watch should never land somewhere that can break it: developer
options, factory reset, safe-mode boot, or a settings page with no way back. A carer or an
IRA administrator must still be able to reach those, deliberately.

## What does it, and what does not

| Need | Mechanism | Standard in AOSP? | State |
| --- | --- | --- | --- |
| Hide developer options from the person | user restriction `DISALLOW_DEBUGGING_FEATURES`, set by the device owner; on a `user` build developer options are also hidden until unlocked by tapping the build number | yes (public `UserManager` and `DevicePolicyManager` API) | needs a device-owner app (below) |
| Block factory reset from Settings | user restriction `DISALLOW_FACTORY_RESET`, set by the device owner | yes | needs a device-owner app |
| Block booting into safe mode | user restriction `DISALLOW_SAFE_BOOT` | yes | needs a device-owner app |
| No lock screen by default | framework overlay `config_disableLockscreenByDefault`, or `setKeyguardDisabled` from the device owner | overlay name uncertain; the API is standard | overlay written (`overlays/IraFrameworkOverlay`) |
| Emergency gesture and power menu kept | framework overlay | yes | overlay written |
| ADB off on production images | `user` build variant (`ro.debuggable=0`, ADB disabled by default) | yes | the `-user` lunch choice exists in `AndroidProducts.mk`; production signing is E4 |

Runtime resource overlays are the standard way to change framework and app **defaults**;
they are used for that (`overlays/IraFrameworkOverlay`). They are **not** the standard way to
lock a setting: an overlay on the Settings app could hide an entry, but the entry's resource
names change between releases and the underlying action would remain reachable by other
routes. No Settings overlay is shipped for that reason.

## "Behind admin": the device-owner app

The restrictions above are set by a **device owner**: one app per device with the
`DevicePolicyManager` admin role, set at provisioning. The watch app is not a device owner
today, and should not become one casually (it would put fleet policy inside the app the
person talks to). The proposal:

1. A small, separate IRA device-policy app (no user interface for the wearer), installed in
   the image as a system app.
2. Made device owner at provisioning, either during manufacture (`dpm set-device-owner` on the
   line, before the device is handed over) or through the platform's provisioning flow. This
   is part of E4 (provisioning; `docs/os/ARCHITECTURE.md`).
3. It sets `DISALLOW_DEBUGGING_FEATURES`, `DISALLOW_FACTORY_RESET`, `DISALLOW_SAFE_BOOT`,
   keeps the keyguard disabled, and lifts a restriction only on a signed instruction from the
   IRA server issued by an administrator (the server already authenticates the watch with its
   device token).
4. A factory reset by an administrator goes through the same signed instruction
   (`DevicePolicyManager.wipeData`), and is recorded in the audit log.

The logic and the wrapper exist (above); the separate device-owner app shell and its provisioning
do not. Recorded in `docs/progress/COMPLETION_REGISTER.md` (rows E2, E4).

## Keeping emergency features on top

Restrictions must never remove an emergency route: the emergency gesture, the power-menu
Emergency entry and the app's Help screen stay available under every policy. See
`EMERGENCY_UI.md`.
