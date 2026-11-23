# IRA CARE watch app (Wear OS and our own AOSP watch)

The companion on the wrist: Kotlin and Jetpack Compose. It runs all day as a foreground
service, speaks with the device's text-to-speech, listens with its speech recogniser, reads
heart rate, steps and wrist state, keeps readings in an offline queue, and talks to the
backend's device API under `/api/v1/care/devices/me`.

Two build flavours, one app (phase 8):

| Flavour | Runs on | Readings | Google on the device |
| --- | --- | --- | --- |
| `wearos` (default) | Wear OS 3 and later: development boards and shop watches (Galaxy Watch 4 onwards) | Health Services (`HealthServicesSensorSource`) | yes (Health Services) |
| `aosp` | our own watch on plain Android (AOSP), no Play Services, no Wear OS licence | the Android sensor framework (`SensorManagerSensorSource`), against `docs/hardware/SENSOR_HAL.md` | none; `scripts/ci/check_watch_aosp_deps.sh` fails the build if one appears |

Every platform dependency is behind an interface in `platform/` and `sensors/`: sensors,
location (the platform `LocationManager`), speech (`Speaker`, `Listener`), haptics
(`Haptics`), connectivity (`Connectivity`) and push (`ServerChannel`: the server's own held
connection; no Firebase). `IRA_WATCH_SIMULATED_SENSORS=1` at build time swaps in the scripted
`SimulatedSensorSource` for the emulator, CI and the presenter; its readings say "simulated".

## Layout

| Package | What it holds |
| --- | --- |
| `api` | `CareApi`: the device endpoints (pair, report, due check-in, repeated, answer, missed, help, the SOS state and its held stream); urgent sends carry an idempotency key |
| `outbox` | `Outbox`: Help, falls, "I'm OK" and answers on disk, each with its own key, retried with backoff and sent before readings; `SosState`: the words the wrist shows, which only the server can turn into "received", "[Name] is on the way" or "[Name] has arrived" |
| `checkin` | `CheckinController`: the ask, repeat, answer, miss state machine, no Android in it |
| `sensors` | `SensorSource` (interface), `HealthServicesSensorSource` (wearos flavour), `SensorManagerSensorSource` with `SensorEventMapper` (aosp flavour; also runs on Wear OS), `SimulatedSensorSource`, `ReadingBatcher` (every heart-rate sample at its own time) |
| `platform` | `Haptics`, `Connectivity`, `ServerChannel`, `SpeechCapabilities`; `PlatformDrivers` (one per flavour) picks the sensor driver |
| `queue` | `OfflineQueue`: readings on disk until the backend acknowledges them |
| `pairing` | `PairingStore`: the device token and ids |
| `voice` | `Speaker` and `Listener` interfaces with Android implementations |
| `service` | `CareForegroundService` (the day-long loop: readings every `reading_upload_interval_s`, an out-of-range heart rate at once), `SyncWorker`, `BootReceiver` |
| `ui` | Pairing, permission and home screens: the question, "I'm OK", "Help" |

The sensor source is an interface on purpose: a vendor sensor SDK can be added behind it
once partner approval exists, without touching the rest of the app.

## Build and test

```bash
cd watch
./gradlew assembleWearosDebug assembleAospDebug testWearosDebugUnitTest testAospDebugUnitTest
bash ../scripts/ci/check_watch_aosp_deps.sh
```

Needs a JDK 17 or later and an Android SDK with platform 35 (`ANDROID_HOME`, or a
`local.properties` with `sdk.dir`). The unit tests run on the JVM with no device or
emulator. The backend address is `CARE_API_BASE_URL` in `app/build.gradle.kts`.

## What has not been done

Nothing here has run on a real watch yet: that needs a Galaxy Watch with LTE. The
background body-sensor permission has to be granted by the person in system settings;
the permission screen sends them there and the app reports to the carer when it is off.
