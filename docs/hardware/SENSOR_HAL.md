# The sensor contract our watch hardware must meet

The IRA CARE watch app reads the person's body and movement through the standard Android
sensor framework when it runs on our own watch (the `aosp` build flavour, phase 8). There is
no Health Services and no Google service on that device. This page is the contract between
the app and the hardware's sensor HAL (the hardware abstraction layer the board vendor or our
hardware engineer implements): which sensors must exist, in which units, at which rates, with
which batching, and how the app uses each. A development board that meets it runs the app
unchanged; `care/ira/watch/sensors/SensorManagerSensorSource.kt` is the driver, and
`SensorEventMapper` holds the arithmetic the JVM tests prove.

Marked "candidate, to confirm with a hardware engineer": the rates and powers below are the
app's needs, not a measured part's figures. `docs/hardware/COMPONENTS.md` (phase 9) names
candidate parts.

## 1. Sensors the app needs

| Android sensor type | Used for | Units the HAL reports | Rate the app asks for | Batching | Wake behaviour |
| --- | --- | --- | --- | --- | --- |
| `TYPE_HEART_RATE` | every heart-rate sample is kept at its own time; an out-of-range sample is uploaded at once | beats per minute, `float`; `accuracy` set honestly (`SENSOR_STATUS_UNRELIABLE` or `SENSOR_STATUS_NO_CONTACT` when the optical signal is poor or the watch is off the wrist; such samples are dropped by the app, never recorded) | 1 Hz while worn (`SENSOR_DELAY_NORMAL`); the HAL may lower it to one sample every 5 to 10 s at rest to save power, as long as the timestamp is true | up to 60 s of samples may be held and delivered together (`maxReportLatencyUs` 60 000 000); each sample carries its own timestamp | non-wake-up: readings are collected when the app's foreground service wakes to upload (every `reading_upload_interval_s`, 60 s by default) |
| `TYPE_STEP_COUNTER` | movement: the server's "no movement" window after an unanswered check-in, and the morning check | steps since boot, monotonic `float`; a reboot resets it (the app treats a smaller total as zero new steps) | on change | up to 60 s | non-wake-up |
| `TYPE_LOW_LATENCY_OFFBODY_DETECT` | wrist-on / wrist-off: gates both fall detectors; off-wrist at night is a notice, off-wrist by day an alert after the set minutes | `1.0` on the body, `0.0` off; a change must be reported within 1 s of the event | on change | none (low latency) | wake-up: a change must wake the device so the state is never stale |
| `TYPE_ACCELEROMETER` | the fall detectors (the rule-based one and the trained 1D-CNN model on 4 s windows at 50 Hz) | m/s² on three axes, device frame (`x`, `y`, `z`); the app divides by `GRAVITY_EARTH` | 50 Hz (`SENSOR_DELAY_GAME`) continuously while worn | none: the detectors run on the live stream | non-wake-up while the service holds its partial wake lock; the fall detector runs in the foreground service |
| `TYPE_PRESSURE` (barometer) | planned: an altitude drop in the second before an impact raises the fall model's confidence (not yet used by the app) | hPa, `float` | 10 Hz on request | up to 60 s | non-wake-up |
| `TYPE_SKIN_TEMPERATURE` (vendor type if no standard one) | planned: a trend reading for the daily report (not yet used by the app) | °C, `float` | one sample a minute | up to 10 min | non-wake-up |
| battery | the battery notice to the carer | `BatteryManager.BATTERY_PROPERTY_CAPACITY`, 0 to 100 | on upload | | |

The off-body sensor is the one that must never lie: the system says "watch off the wrist"
and "no movement" to a carer on its word. If the hardware has no dedicated off-body sensor,
the HAL must synthesise one from the optical sensor's contact detection and report it under
`TYPE_LOW_LATENCY_OFFBODY_DETECT`.

## 2. What the app does with the readings

- Every sample is sent with the time it was sampled, not the time it was uploaded (the
  server stamps arrival separately). Timestamps are `SystemClock.elapsedRealtimeNanos`
  converted by the framework; the HAL must not reuse or round them.
- Heart rate: samples with `accuracy <= 0`, below 25 bpm or above 250 bpm are dropped on the
  watch. Nothing is interpolated.
- Steps: the running total becomes per-minute deltas; a total smaller than the last one is a
  reset, not negative steps.
- Falls: the accelerometer stream runs through the rule detector (impact over the carer's
  threshold, then stillness) and, where the model file is present, the trained model; either
  starts the countdown, and both are gated by the off-body state.
- Location: one fix from the platform `LocationManager` when an SOS or a suspected fall
  starts; never continuous. GNSS must be available to the framework as `GPS_PROVIDER`.

## 3. Power and rates the app assumes (candidate, to confirm with a hardware engineer)

| Need | Assumption |
| --- | --- |
| Accelerometer at 50 Hz, all day | under 0.1 mA at the sensor; the application processor must not be woken per sample (a sensor hub or FIFO of at least 200 samples, four seconds) |
| Optical heart rate at rest | under 1 mA averaged, with the HAL free to sample every 5 to 10 s between the app's one-a-second request and the hardware's duty cycle |
| Off-body detection | continuous, under 0.05 mA, wake-up capable |
| Upload | the foreground service wakes once a minute by default (a setting from 15 s to 15 min) |

## 4. Permissions and features the image must grant

- `android.permission.BODY_SENSORS` and `BODY_SENSORS_BACKGROUND` (or, on an image without the
  background split, `BODY_SENSORS`), `ACTIVITY_RECOGNITION`, `ACCESS_FINE_LOCATION`,
  `RECORD_AUDIO`, `VIBRATE`, `FOREGROUND_SERVICE_HEALTH`, `RECEIVE_BOOT_COMPLETED`.
- `android.hardware.type.watch` as a feature so the round-screen layouts apply.
- A text-to-speech engine and a speech recogniser registered with the framework
  (`TextToSpeech.getEngines()` non-empty, `SpeechRecognizer.isRecognitionAvailable()` true).
  Without them the app still works through the screen and the buttons and says so; the
  candidates are open engines that run on the device (phase 9 lists them). Speech is not a
  Google dependency in this app: `Speaker` and `Listener` are interfaces.
- A vibrator (`Vibrator.hasVibrator()`), used for questions, repeats, falls and a carer's
  arrival through the `Haptics` interface.
- Network: Wi-Fi and LTE through the ordinary `ConnectivityManager`; the server's held
  connection is the only push channel. No Firebase, no Play Services anywhere in the image.

## 5. How a board is checked against this contract

1. Build the `aosp` flavour: `cd watch && ./gradlew assembleAospDebug` (CI does this, and
   `scripts/ci/check_watch_aosp_deps.sh` proves the build has no Google service library).
2. Install on the board, pair with a workspace, wear it for an hour: the dashboard's Readings
   tab must show heart rate once a second (or the HAL's rest rate), steps by the minute,
   wrist on and off within a second of taking it off, and battery.
3. Drop the watch on a table and set it down: no fall (off-body gate). Wear it and simulate a
   fall on a mat: the countdown starts within 2.5 s of the impact (the model's post-impact
   window is 2 s).
4. Record the measured power draw per sensor against section 3 and correct this page.

Until a board exists, the `IRA_WATCH_SIMULATED_SENSORS=1` build runs the scripted driver so
the rest of the system can be exercised; every reading it produces is marked "simulated".
