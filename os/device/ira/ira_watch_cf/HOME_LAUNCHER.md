# IRA as the home screen (launcher)

**State: not possible yet without an app change. The app has not been changed.**

On IRA CARE OS the watch app should be the home screen: the first thing shown after boot,
what the home or back gesture returns to, and the only app the person ever sees. Android
treats an app as a home app only if one of its activities declares an intent filter with
`android.intent.action.MAIN` and the categories `android.intent.category.HOME` and
`android.intent.category.DEFAULT`.

`watch/app/src/main/AndroidManifest.xml` (checked 25 November 2026) declares `MainActivity`
with `MAIN` and `LAUNCHER` only. There is no HOME filter in the main manifest, and the
`aosp` flavour has no manifest of its own. So the image keeps the stock launcher for now:
removing it without a replacement would leave the device with no home activity.

## The app change needed (follow-up, for the watch app's owner)

1. In the **aosp flavour only** (a new `watch/app/src/aosp/AndroidManifest.xml`, merged by
   Gradle), add a second intent filter to `.ui.MainActivity`:

   ```xml
   <activity android:name=".ui.MainActivity" android:exported="true">
       <intent-filter>
           <action android:name="android.intent.action.MAIN" />
           <category android:name="android.intent.category.HOME" />
           <category android:name="android.intent.category.DEFAULT" />
       </intent-filter>
   </activity>
   ```

   Keeping it out of the `wearos` flavour matters: on a Wear OS watch from a shop, the app
   must not offer itself as the system home.
2. Back must not leave the home activity: on the home screen, back does nothing (a home app
   that finishes itself shows an empty screen until the system restarts it).
3. `launchMode="singleTask"` is already set, which a home activity needs.
4. Add `android:showWhenLocked` and `android:turnScreenOn` (or the matching `Activity`
   calls) so Help is reachable over the lock screen; see `EMERGENCY_UI.md`.
5. The app's first-run permission screen opens the system's app-details page for background
   body sensors. On the image those permissions are proposed as first-boot grants
   (`permissions/default-permissions-care.ira.watch.xml`), so the screen should check what is
   already granted and skip itself, rather than send the person into Settings.

## The image change that follows (in this folder)

When the app has the filter, uncomment `overrides: ["Launcher3QuickStep"]` in `Android.bp`
so the stock launcher is removed from the image and IRA is the only home app; Android then
uses it without asking. The launcher module name must be checked in the checked-out AOSP
release (`m IraCareWatch` reports an unknown override as an error).

A role-based alternative (making IRA the default holder of the HOME role while keeping the
stock launcher for engineers) depends on the PermissionController's default-holder
configuration in the target release and is not specified here.
