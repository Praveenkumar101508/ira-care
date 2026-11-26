# iOS project generation on Linux (family app)

Run on 26 November 2026 on the Linux build container, from a clean export of `mobile/`
(Node 22.23.5, `npm ci`, Expo SDK 52, React Native 0.76.9):

```bash
CI=1 EXPO_NO_TELEMETRY=1 npx expo prebuild --platform ios --no-install
```

It finished without error ("Created native directory", "Finished prebuild") and produced
`ios/IRACARE.xcodeproj`, `ios/Podfile` and `ios/IRACARE/Info.plist`: bundle identifier
`care.ira.family`, deployment target iOS 15.1, iPhone only, Hermes, the old architecture,
and an `aps-environment` entitlement for push. CocoaPods and Xcode do not exist on Linux,
so `pod install` and the build itself were not run here; they are what
`.github/workflows/ios-simulator.yml` does on a macOS runner (manual only, not run). The
generated `ios/` folder is not committed; `mobile/.gitignore` now ignores `/ios/` and
`/android/`. Prebuild also rewrites the `ios` and `android` scripts in `package.json`;
that change was not kept.

## A configuration issue found and fixed

The generated `Info.plist` carried three permission texts the app never wrote, added by
the defaults of two config plugins:

| Key | Text | Source | Used by the app? |
|---|---|---|---|
| `NSCameraUsageDescription` | "Allow $(PRODUCT_NAME) to access your camera" | `expo-image-picker` default | No: the app only picks from the library |
| `NSPhotoLibraryUsageDescription` | "Allow $(PRODUCT_NAME) to access your photos" | `expo-image-picker` default | Yes: a photo of your own as the background (Settings) |
| `NSFaceIDUsageDescription` | "Allow $(PRODUCT_NAME) to access your Face ID biometric data." | `expo-secure-store` default | No: secure store is used without biometric prompts |

Generic purpose strings, and permissions an app does not use, are a common reason for
App Store review to reject a build. `mobile/app.json` now configures both plugins
explicitly: the camera and Face ID keys are removed (`false`), and the photo library text
says what it is for ("IRA CARE opens your photos only when you choose one of your own
pictures as the app's background."). After the change the generated `Info.plist` holds
exactly four purpose strings: microphone and speech recognition (talking to IRA), photo
library, and notifications. On Android the same change marks `android.permission.CAMERA`
as removed; `RECORD_AUDIO` stays, because speech recognition needs it. The family app's
type check and its 18 Jest suites (83 tests) pass with the change.
