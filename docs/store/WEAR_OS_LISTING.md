# IRA CARE watch app: store listing and permission declarations

Prepared for the Play Console. Nothing has been submitted; the owner submits. Everything
below describes the app as built in this repository.

## Listing

- **Name:** IRA CARE
- **Short description (80 characters):** A gentle companion on the wrist that checks in and gets help when needed.
- **Full description:**

  IRA CARE is the watch app for people who live alone and the families and carers who
  look after them. A few times a day IRA asks a short, friendly question and listens for
  the answer. If nobody answers and the watch sees no movement, if a heart-rate reading
  stays outside the range a carer set, or if the watch detects a hard fall, IRA asks "are
  you OK?" first and, if there is still no answer, tells the family and carers in turn,
  with the home address and the watch's location at that moment. A Help button and the
  spoken word "help" do the same at once.

  IRA CARE never dials emergency services itself. It tells the people in the care circle,
  who decide what to do. It is not a medical device and makes no diagnosis.

  The watch app needs a paired IRA CARE account; the family app or the carer dashboard
  creates the pairing code.

- **Category:** Health and fitness (companion); **Content rating:** Everyone.
- **Screenshots:** the home screen ("All well", I'm OK, Help, Talk to IRA), a question, the
  fall countdown, the pairing screen. Round and square frames.
- **Contact and policy:** the owner's support address; the privacy policy at
  `docs/legal/PRIVACY_POLICY.md` (to be published at a URL by the owner).

## Permissions and why each exists

| Permission | Why | What the user sees |
| --- | --- | --- |
| BODY_SENSORS, BODY_SENSORS_BACKGROUND | Heart rate while the screen is off: a reading outside the carer's range is one of the three reasons IRA asks "are you OK?". | The permission screen explains it in a sentence; the system settings page is opened for the background grant. |
| ACTIVITY_RECOGNITION | Steps and movement, the "no movement" half of "no answer and no movement". | Same screen. |
| ACCESS_FINE_LOCATION, ACCESS_COARSE_LOCATION | One fix when the person presses Help, says help, or a suspected fall is confirmed. Never in the background, never continuously. | "and your location only when you ask for help" on the permission screen. |
| RECORD_AUDIO | To hear the answer to a question and what the person says to IRA. Audio is never stored; the recogniser returns text. | The system microphone prompt. |
| FOREGROUND_SERVICE, FOREGROUND_SERVICE_HEALTH | The ongoing notification that keeps IRA collecting readings and asking questions. | "IRA is with you." |
| POST_NOTIFICATIONS, VIBRATE, WAKE_LOCK, RECEIVE_BOOT_COMPLETED, INTERNET | The notification, the vibration that goes with a question, staying awake to speak, restarting after a reboot, talking to the server. | |

**Background body-sensor declaration (for the Play Console form):** IRA CARE reads heart
rate in the background because the person it serves lives alone and the watch is often the
only thing with them. A sustained reading outside the range their carer set, while they are
not interacting with the watch, is one of the signals that starts a check ("are you OK?")
and, with no answer, a call to the family. The reading is sent to the person's own care
server and shown to their family and carers under the person's consent; it is not shared
with anyone else and not used for advertising.

**Location declaration:** location is requested at the moment help is asked for or a
suspected fall is confirmed, once, and attached to that alert so the responder knows where
to go. It is not collected in the background and not stored outside the alert.

## Data safety answers

| Question | Answer |
| --- | --- |
| Does the app collect or share user data? | Collects: health and fitness (heart rate, steps), approximate and precise location (at an SOS only), audio transcribed to text (what the person says), app activity (check-in answers). Shares: with the person's own family and carers inside the service, under the person's consent. Not sold; not shared with third parties for advertising. |
| Is data encrypted in transit? | Yes (HTTPS to the care server). |
| Can users request deletion? | Yes: the care organisation's manager erases the person, which removes everything recorded about them except the access log. |
| Is data collection optional? | The heart rate, movement and audio are the product; location is only at an SOS; all require the user's permission grants. |
| Account creation | The watch has no account of its own; it is paired to a person's record by a code from a signed-in family member or carer. |
| Independent security review | Not yet. |

## Release build

`watch/app/build.gradle.kts`: `release` is minified and shrunk with the rules in
`proguard-rules.pro`; signing reads `IRA_WATCH_KEYSTORE`, `IRA_WATCH_KEYSTORE_PASSWORD`,
`IRA_WATCH_KEY_ALIAS`, `IRA_WATCH_KEY_PASSWORD` from the environment or a `keystore.properties`
file the owner keeps outside git; `IRA_CARE_API_BASE_URL` names the production server and
`IRA_WATCH_VERSION_CODE` the upload number. The unsigned release package built in this
repository's checks; the signed one is the owner's.
