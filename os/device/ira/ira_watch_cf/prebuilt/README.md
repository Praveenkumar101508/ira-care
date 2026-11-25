# Prebuilt watch app (build input, not committed)

`Android.bp` imports `prebuilt/IraCareWatch.apk`. That file is **not in the repository**:
no binaries are committed, and the release APK must be signed with IRA's own key, which is
never in the repository either.

`os/scripts/build_cuttlefish.sh` puts it here on a build host, after the resource preflight
and only with `IRA_OS_I_HAVE_THE_RESOURCES=1`:

1. Either take `IRA_OS_WATCH_APK` (a path to a signed `aospRelease` APK), or build it with
   `cd watch && ./gradlew :app:assembleAospRelease` with the signing variables described in
   `watch/app/build.gradle.kts` set.
2. Refuse an unsigned APK (`app-aosp-release-unsigned.apk`) and the debug build (its package
   is `care.ira.watch.debug`, which the permission files do not name).
3. Check the package name and the requested permissions with `aapt2 dump badging` and
   `aapt2 dump permissions` against `permissions/privapp-permissions-care.ira.watch.xml`.
4. Copy it to this directory as `IraCareWatch.apk`.

None of these steps has been run: there is no AOSP build host yet (`docs/os/BUILD.md`).
