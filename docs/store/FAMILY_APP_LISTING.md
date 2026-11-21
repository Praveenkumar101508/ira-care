# IRA CARE family app: build configuration and store listing

Prepared for Google Play and the App Store. Nothing has been submitted and no store or
build-service account has been created; the owner does both.

## Listing

- **Name:** IRA CARE
- **Subtitle / short description:** Know your person is all right, and be told when they are not.
- **Description:**

  IRA CARE keeps family and carers close to someone who lives alone. The watch they wear
  checks in with them through the day; this app shows how they are, tells you at once when
  they need someone, and lets you say "I'm on my way" so everyone knows who is going.

  You see each alert with where the person is (at home, with the address, or the watch's
  location), the emergency information their carer wrote, and, while an alert is open, the
  access notes (key safe, door, pets). You see summaries of their check-ins; their own
  words only with their recorded consent. The person can also talk to IRA from this app on
  their own phone.

  IRA CARE never dials emergency services itself. It tells you, and you decide.

- **Category:** Medical is not claimed; **Health & fitness** or **Lifestyle**. Content rating: Everyone.
- **Screenshots:** home (people and open alerts), an alert with whereabouts and access notes, a person's day, Talk to IRA, the pairing steps.
- **Privacy policy URL:** the owner publishes `docs/legal/PRIVACY_POLICY.md`.

## Permissions and why

| Platform | Permission | Why |
| --- | --- | --- |
| Android | POST_NOTIFICATIONS | Alerts arrive as notifications. |
| Android | CALL_PHONE (via the dialler) | "Call" opens the phone's dialler with the number; the person taps to dial. |
| iOS | Notifications | The same. The usage string is in `app.json`. |

## Build configuration

- `mobile/app.json`: bundle id `care.ira.family`, package `care.ira.family`, version 0.1.0,
  `ios.buildNumber` and `android.versionCode` 1, icon, adaptive icon and splash under
  `mobile/assets/`, notifications usage description, `ITSAppUsesNonExemptEncryption=false`.
- `mobile/eas.json`: `development`, `preview` (APK, internal) and `production` (app bundle,
  auto-increment) profiles; `submit` is empty on purpose. `CARE_API_BASE_URL` is set per
  profile to the owner's server. The `eas.projectId` placeholder is filled when the owner
  creates the project.
- Nothing is built by a cloud build service from this repository; the type check and the
  unit tests run in CI, and the web export is built for the demo.

## Data safety answers

As for the watch app (`WEAR_OS_LISTING.md`), with these differences: the family app
collects account identifiers (the email address and sign-in), reads the person's data under
the person's consent, and collects no sensor data of its own.
