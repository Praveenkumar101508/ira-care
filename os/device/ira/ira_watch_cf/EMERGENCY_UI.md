# Help from the lock screen (emergency UI)

**State: design note. Nothing here has run on a device or on Cuttlefish.**

The rule: the person can always ask for help, with one action, whatever state the watch is
in. Today Help is the large red button on the IRA app's home screen (`HomeScreen` in
`watch/app/src/main/java/care/ira/watch/ui/MainActivity.kt`) and, on the planned hardware,
the coral Help button on the case (`docs/hardware/BLUEPRINT.md`). How the OS keeps those
reachable:

## 1. Lock screen

- **Default: no lock screen.** The framework overlay sets
  `config_disableLockscreenByDefault` (`overlays/IraFrameworkOverlay/res/values/config.xml`;
  the name is marked low-confidence there). A care watch worn by one person, often with poor
  sight or dexterity, gains little from a PIN and loses the one-tap Help. If the name does not
  exist in the target release, provisioning sets the same thing: the device-owner policy (see
  `SETTINGS_POLICY.md`) calls `DevicePolicyManager.setKeyguardDisabled(admin, true)`, a public
  API for device owners.
- **If a lock is set anyway** (a carer chooses one, or a later policy requires it), the app's
  home activity must declare `android:showWhenLocked="true"` and `android:turnScreenOn="true"`
  so the Help screen is drawn over the keyguard without unlocking. The app now declares both,
  in the aosp flavour only (`watch/app/src/aosp/AndroidManifest.xml`); not yet seen on a device. The Help flow then only reads
  what it needs (it sends the SOS through the server); nothing private is shown while locked.

## 2. Platform emergency routes kept on

- The repeated power-button press (Emergency SOS gesture) stays enabled:
  `config_emergencyGestureEnabled` = true in the overlay.
- A long press on power opens the power menu with its Emergency entry, not an assistant:
  `config_longPressOnPowerBehavior` = 1 in the overlay.
- The platform's emergency dialer remains in the image. On Cuttlefish there is no modem, so
  these routes can be shown but cannot place a real call; on the hardware they depend on the
  LTE modem and eSIM (`docs/os/QUALCOMM_DEPENDENCIES.md`).

## 3. The Help button on the case

The planned hardware has a dedicated Help button. To reach the app from any state, the
button's key must be mapped in the board's key layout file (`.kl`, a board-support file) to a
key code, and the app (as home activity, or through a small foreground receiver) must start
the SOS on that key. The key code, the long-press rule (a long press, so a knock does not
start an SOS) and the haptic acknowledgement are for the board bring-up; Cuttlefish has no
such button. This needs the target board.

## 4. What stays on top

- The IRA app is the home activity (once it can be; `HOME_LAUNCHER.md`), so there is no other
  app to bury it under.
- The fall countdown and an active SOS already run in the app's foreground service
  (`CareForegroundService`), so they survive the screen turning off.
- Notifications from other packages are not expected: the image carries no other user apps.
