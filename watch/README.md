# IRA CARE watch app (Wear OS)

The companion on the wrist: Kotlin, Jetpack Compose for Wear OS, Wear OS 3 and later
(Galaxy Watch 4 onwards). It runs all day as a foreground service, speaks with the
watch's own text-to-speech, listens with the watch's own speech recogniser, reads heart
rate, steps and wrist state through Wear OS Health Services, keeps readings in an offline
queue, and talks to the backend's device API under `/api/v1/care/devices/me`.

## Layout

| Package | What it holds |
| --- | --- |
| `api` | `CareApi`: the device endpoints (pair, report, due check-in, repeated, answer, missed, help, the SOS state and its held stream); urgent sends carry an idempotency key |
| `outbox` | `Outbox`: Help, falls, "I'm OK" and answers on disk, each with its own key, retried with backoff and sent before readings; `SosState`: the words the wrist shows, which only the server can turn into "received", "[Name] is on the way" or "[Name] has arrived" |
| `checkin` | `CheckinController`: the ask, repeat, answer, miss state machine, no Android in it |
| `sensors` | `SensorSource` (interface), `HealthServicesSensorSource`, `ReadingBatcher` (every heart-rate sample at its own time) |
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
./gradlew assembleDebug testDebugUnitTest
```

Needs a JDK 17 or later and an Android SDK with platform 35 (`ANDROID_HOME`, or a
`local.properties` with `sdk.dir`). The unit tests run on the JVM with no device or
emulator. The backend address is `CARE_API_BASE_URL` in `app/build.gradle.kts`.

## What has not been done

Nothing here has run on a real watch yet: that needs a Galaxy Watch with LTE. The
background body-sensor permission has to be granted by the person in system settings;
the permission screen sends them there and the app reports to the carer when it is off.
