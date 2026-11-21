# IRA CARE on Apple Watch: plan (not started)

Apple Watch support is out of scope until the owner has a Mac, an Apple developer account
and a test watch. This is the plan the Wear OS build is shaped to make easy.

## What is different on watchOS

- A watchOS app cannot stay running in the background for long. Continuous background
  sensing is possible only inside an active session such as a workout session, and using a
  workout session for a non-workout purpose risks App Review rejection.
- Heart rate, steps, movement and wrist detection are recorded by the watch itself into
  HealthKit regardless of any third-party app.

## Approach

1. **Read, do not sense.** The IRA CARE watchOS app reads what the watch has already
   recorded in HealthKit (heart rate, steps, resting heart rate, wrist temperature where
   available) and uploads it on a background refresh and whenever the app is opened.
2. **Check-ins by notification.** Check-ins arrive as local notifications with "I'm OK"
   and "Help" actions, and open the app for a spoken answer when tapped. The watch's own
   fall detection and Emergency SOS stay on.
3. **The phone as relay.** The paired iPhone app (the existing Expo app, built for iOS)
   receives the watch data through the watch-to-phone channel and does the uploading, so
   the watch app itself stays small.
4. **Same backend.** Devices report through the same `/api/v1/care` endpoints as Wear OS;
   the device record carries `platform = watchos` and a lower expected reporting rate, so
   the "no sync" rule uses a longer window.

## What the Wear OS build already does to prepare

- Sensors are behind an interface, so a platform that only exposes recorded data fits.
- The rules engine treats a missing reading as "unknown", never as "normal".
- Device records carry a platform and an expected reporting interval.

## Needs

A Mac with Xcode, an Apple developer account, a test Apple Watch and iPhone, and a review of
HealthKit entitlement wording.
