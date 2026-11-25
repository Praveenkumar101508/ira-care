# Native Android builds, 25 November 2026

Local builds of the watch app and the family app on `main` at `eec19a0`, on a Linux x86_64
container with 4 CPUs and 16 GB RAM, shared with another test run (Gradle `--max-workers=2`).
The Android SDK licence terms were accepted (`sdkmanager --licenses`) with the owner's approval
on 25 November 2026. Exact numbers are in `android.json`.

These are **not** signed store builds, and **nothing ran on a device or an emulator**.

## Watch app (Wear OS and AOSP flavours): built and tested

| Step | Result | Time |
|---|---|---|
| `assembleWearosRelease assembleAospRelease` | passed | 274 s |
| `assembleWearosDebug assembleAospDebug testWearosDebugUnitTest testAospDebugUnitTest` | passed; 111 tests per flavour, 0 failed, 0 skipped | 97 s |
| `scripts/ci/check_watch_aosp_deps.sh` | passed: 626 dependency lines on the aosp runtime classpath, none from a Google service | 9 s |

| APK | Size |
|---|---|
| `app-wearos-release-unsigned.apk` | 17,321,915 bytes |
| `app-aosp-release-unsigned.apk` | 17,111,278 bytes |
| `app-wearos-debug.apk` | 42,930,577 bytes |
| `app-aosp-debug.apk` | 38,410,943 bytes |

- **The research fall model is in no release build (D-077).** `unzip -l` finds no `fall_model`
  entry in either release APK; both debug APKs carry `assets/fall_model.tflite` (17,864 bytes)
  and `fall_model.json`, as intended.
- The release APKs are unsigned: the build signs only when IRA's release key is supplied, and no
  such key exists here. A signed release needs the owner's key-custody decision (D1b.c).
- Versions: OpenJDK 21.0.13 (Java 17 bytecode), Gradle 8.14.3, Android Gradle Plugin 8.7.3,
  Kotlin 2.0.21, compile and target SDK 35, minimum SDK 30, build-tools 34.0.0 and 35.0.0.

## Family app (Expo SDK 52): prebuilt, native build blocked by the sandbox's network

| Step | Result | Time |
|---|---|---|
| `npm ci` | passed | 11 s |
| `npx expo prebuild --platform android --no-install`, in a copy outside the repository | passed | 7 s |
| `./gradlew -PreactNativeArchitectures=arm64-v8a assembleRelease` | **blocked** | 298 s |

- **What blocked it.** `expo-image-picker` (the Settings tab uses it to pick a photo) needs
  `com.github.CanHub:Android-Image-Cropper:4.3.1`, which is published only on jitpack.io. This
  sandbox's network policy refuses jitpack.io (403 from the proxy). It is not a defect in the
  repository, and removing the picker would change the product, so nothing was changed. A
  machine or CI runner that can reach jitpack.io is what this build needs.
- **Fixed on the way.** The first attempt stopped before compiling: React Native's Gradle
  plugin requires a Java 17 toolchain, the machine had only Java 21, and Gradle's toolchain
  download is refused by the proxy. Installing OpenJDK 17.0.21 cleared it.
- **Expo's one warning.** `userInterfaceStyle: automatic` needs `expo-system-ui` on Android to
  follow the system's dark mode. Recorded, not changed.
- Versions: Expo 52.0.49, React Native 0.76.9, Gradle 8.10.2 (generated wrapper), OpenJDK
  17.0.21, NDK 26.1.10909125 (installed by Gradle), build-tools 35.0.0. One ABI (`arm64-v8a`)
  to keep the native compile within this machine; a store build would include the others.

## Network note

Maven Central (`repo.maven.apache.org`) answered 429 Too Many Requests to this shared address.
A Gradle init script kept outside the repository redirected those requests to Google's public
mirror of Maven Central (`maven-central.storage-download.googleapis.com`): the same artifacts.
No build file in the repository was changed.

## The family app's native build in CI

Because jitpack.io is refused from the development sandbox, the family app's native Android build
now runs in CI instead: the job "Family app Android build" in `.github/workflows/test.yml`
(`mobile-android`) generates the native project with `expo prebuild`, builds the release APK with
debug signing for arm64-v8a, and records its size in the run summary. Its first run, on the pull request that added it (#82, commit `cbbc727`), passed: the family app
built as a release APK. Requirement D2.g records that CI result, not this sandbox's attempt. It is a
build, not a signed store build, and nothing ran on a phone or emulator.
