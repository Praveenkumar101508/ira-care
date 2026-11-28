# Wear OS emulator: the care journey passes (26 November 2026)

The watch app ran through the care journey on a Wear OS emulator for the first time, on GitHub's
standard Linux runner with KVM. The runner is free while the repository is public
([`../COMPUTE_RESOURCES.md`](../COMPUTE_RESOURCES.md)). The earlier attempt on the build machine,
without KVM, is in [`NO_KVM_ATTEMPT.md`](NO_KVM_ATTEMPT.md).

## The passing run

| | |
|---|---|
| Workflow | `.github/workflows/native-checks.yml`, which calls `.github/workflows/emulator.yml` (pull request #119) |
| Run and job | run 45761957903, job 139713814067, commit `04f34c8` (with the fix for the crash after pairing). Run 45758552085 (`ed7ac60`) passed earlier, before that crash was found |
| Runner | `ubuntu-latest` (standard), `/dev/kvm` enabled by a udev rule |
| Image | `google/sdk_gwear_x86_64`, Wear OS on Android 14 (API 34), `wearos_large_round` (454 × 454), userdebug |
| Backend | the demo API (`scripts/demo/api.py`) on the runner, fictional seed data, log-only message providers, PostgreSQL and Redis service containers |
| Job time | 05:29:47 to 05:34:13 UTC (4 min 26 s), including the APK build; the emulator booted in 28.5 s |

| Step | Result | Time |
|---|---|---|
| Prepare the device (setup done, reported unplugged, screen kept on) | pass | 1.9 s |
| Install the debug APK | pass ("Success") | 6.6 s |
| Launch (cold start) | pass, 2,099 ms | 2.3 s |
| Pairing screen shown, with its Pair button | pass | 28.7 s |
| Pairing code created on the server, as the coordinator | pass | — |
| Code typed on the watch | pass | 14.3 s |
| Paired: the pairing screen gives way, and the server records the device `paired`, app version 0.1.0 | pass | 2.3 s |
| Home screen with the Help button (after the runtime permissions are granted) | pass | 2.5 s |
| Help tapped: "Help request received" on the wrist | pass | 7.6 s |
| Exactly one open `help_requested` alert for that person on the server, `via: button`, from that device | pass | 7.6 s |

The screenshots, UI dumps, logcat and `results.json` are uploaded as the run's artefact
`wear-emulator-smoke-api34` (kept 14 days).

## Five defects the journey found on the way, all fixed in #119

| Run | What stopped it | Fix |
|---|---|---|
| 45753366726 | The demo backend did not start: its worker scheduler imports `psutil`, which the test requirements leave out | The job installs the version pinned in `requirements.txt`; a test checks it |
| 45755139976 | The app launched, but Wear OS's charging clock (sysui `ChargingActivity`) was on top, because the emulator reports a charger | The smoke reports the device unplugged before launch; if a Wear OS screen (the charging clock, or the watch face as in run 45762586497) still comes in front, the app is brought back, up to three times, each noted in the log |
| 45757010887 | The code was typed, but the tap on Pair did nothing (the Wear keyboard was still over the screen, and the tap went to the label) | The keyboard is closed first, the tap goes to the button's clickable node, and it is repeated once if nothing changes |
| 45758000634 | Pair answered "No connection. Try again.": the debug build's server is `http://10.0.2.2:8000`, and Android refuses cleartext HTTP unless the app allows it, so **the debug build could never reach a development server** | A debug-only network security config permits cleartext to 10.0.2.2, localhost and 127.0.0.1 only; release builds keep Android's default; guard tests (`test_watch_cleartext.py`) |

| 45760714043 | Paired, then the app was killed: `SecurityException: Starting FGS with type health ... requires ... any of [ACTIVITY_RECOGNITION, BODY_SENSORS, HIGH_SAMPLING_RATE_SENSORS]`. Pairing started the care service before the permission screen had been shown. The run above passed only because the permissions were granted a moment sooner | Every start of the service goes through a permission check (`ForegroundPermission`, unit tests); the permission screen starts the service once the grant arrives; a guard test keeps every start behind the check (`test_watch_foreground_start.py`, negative control on the old code) |

The last two are product defects, not test-harness problems. Without the cleartext fix, nobody could pair a
debug watch build with a local server, on an emulator or a real watch. Release builds were never
affected by the cleartext one: they use HTTPS. The crash after pairing affected every build: a
wearer who paired before allowing the sensors lost the app until it was opened again.

Since that crash was hidden by a passing journey once, the journey now ends with an eleventh step,
`no_app_crash`, which fails if the device's crash buffer shows a crash of the app.

When a step fails, the smoke script now prints what the device shows into the job log: the visible
text, the packages and focused window, the clickable nodes with their bounds, the keyboard state,
the crash buffer and recent error lines. The artefact is not always reachable by whoever reads the
result.

## What this shows, and what it does not

- **Shows:** the watch app, built as CI builds it, installs, starts, pairs with the real server code
  through the real pairing API, and delivers a Help request that the server records as one open
  alert. Requirement D1.i is verified on this run.
- **Does not show:**
  - the fall countdown, check-ins, speech or sensors: emulator sensors are simulated and the
    journey does not exercise them;
  - battery life, or behaviour on a real watch (D1.j and the device rows);
  - the family phone app on an Android emulator, which the journey does not yet include.
    Requirement K.g asks for both apps, so it stays open.

## Repeating it

The job runs by itself on any pull request that changes `watch/**`, `mobile/**`, `scripts/emulator/**`
or the three workflow files. It also runs by hand from the Actions tab (`Wear OS emulator`, "Run workflow").

## Round 9: extended journey — written, not yet run on the emulator

The eleven core steps above are unchanged. `scripts/emulator/watch_smoke.py` now continues past
them through nine groups, chosen with `--steps` (default `all`; the workflow's `steps` input)
and cut short with `--until`. Each group is guarded: a failure records its steps, takes the
device diagnosis, and the next group still runs. Evidence goes to `--out` as before
(screenshots, `ui_<screen>.xml` dumps, server JSON in `results.json`, logcat). **This section
records what is written and proved without a device. No emulator run of these groups has
happened yet**: the build machine has no KVM, so the coordinator runs the workflow on GitHub's
standard runner (`emulator.yml`, "Run workflow", journey `care-journey`, steps `all`) and the
results of that run are to be added here.

| Group | Steps | What a pass proves |
|---|---|---|
| `medication` | `medication_reminder_created`, `medication_reminder_shown`, `medication_unclear_not_recorded`, `medication_confirm_asked`, `medication_taken_two_step` | A medicine due now reaches the wrist as a reminder with Done / Later / Not today. "Later" (the touch alternative to an unclear spoken answer such as "maybe later" or "I think so") snoozes the reminder and **records no taken dose** (C-1). A second reminder answered "Done" makes the server ask once more ("you've taken X. Is that right?") before anything is written; the second "Done" records one dose: `status taken`, `confirmed_via checkin`, `source self_report`, `checkin_id` the confirmation check-in, both check-ins `answered_via button`. The server's provenance for the wrist is `confirmed_via = checkin` (the dose came through the watch's own check-in); there is no `source = watch` value in the schema (`care/repository.py DOSE_SOURCES`). Every step records `voice_exercised: false`: speech is not exercised on the emulator |
| `checkin` | `checkin_requested`, `checkin_shown`, `checkin_answered_by_touch` | A family member's "ask now" becomes the question on the wrist within the watch's 60 s poll; "I'm OK" answers it (`answered_via button`) |
| `fall` | `fall_countdown_shown`, `fall_second_detection_attached`, `fall_alert_after_countdown`, `fall_alert_resolved` | A synthetic fall (`am broadcast` to the debug build's `SyntheticFallReceiver`, which hands a `FallEvent.SuspectedFall` to the care service through `FallInjection`, the same method the detectors' events go through) starts the countdown ("Help in N s") and an `asking` `suspected_fall` alert. A second synthetic fall 5 s later during the countdown is **attached** to that alert (`details.detections` has two entries) and there is still exactly one unresolved SOS alert (D-092). When the countdown lapses the wrist shows "Help request received." and the same alert is `open` with `outcome no_answer` |
| `help_during_fall` | `help_during_fall_countdown_shown`, `help_during_fall_one_alert`, `responder_status_on_wrist`, `help_during_fall_resolved` | Help pressed during a fall countdown opens the fall's own alert, not a second one (one unresolved SOS alert, kind `suspected_fall`). The family member acknowledges "on my way" through the API and the wrist shows "`<name>` is on the way." (the server's word over the SOS stream) |
| `offline` | `offline_help_queued_offline`, `offline_queue_delivered_once` | With `svc wifi disable && svc data disable` the face shows "Offline: saving for later"; Help pressed shows "Waiting for connection. Help request saved." and **nothing reaches the server**; with connectivity back the request arrives **exactly once** (`help_requested`) and the wrist shows it was received |
| `reboot` | `reboot_help_queued_offline`, `reboot_monitoring_resumed`, `reboot_home_screen`, `reboot_queue_delivered_once` | A Help queued offline survives `adb reboot`: after `sys.boot_completed` the care service is running again (`BootReceiver`), the paired home screen is shown, and the queued request arrives once when connectivity returns |
| `permission` | `permission_revoked_shown`, `permission_revoked_reported`, `permission_regranted_recovered` | `pm revoke` of `BODY_SENSORS_BACKGROUND` and `BODY_SENSORS` shows the "Background sensors" permission screen, and the server's device row reports `background_sensor_permission: false` on the next sync (D1.g); granting them again brings the home screen back and the row reports `true` |
| `unpair` | `unpair_warning_shown`, `unpaired_on_server`, `old_token_refused`, `repair_type_code`, `repair_paired`, `repaired_isolated` | "Unpair watch…" shows the warning ("Unpair this watch? IRA will stop watching over you…") with "Keep paired" first; "Unpair" returns the pairing screen, the server row is `revoked` and a `watch_unpaired` notice is opened. The dead token is proved with a scratch device of the journey's own (paired by code, unpaired, then `GET /devices/me` and `POST /devices/me/help` answer 401), because the watch's own token is sealed on the device. Pairing again with a new code gives a new `paired` row while the old stays `revoked`. The Unpair button appears only once the server stops reporting the closed SOS (ten minutes), so this group waits up to `--sos-idle-timeout` |
| `accessibility` | `accessibility_labels`, `accessibility_touch_targets` | On every dumped app screen (home, reminder, confirmation, check-in, fall, Help, responder, permission, unpair) every clickable node has text or a content description, and none is smaller than 44 dp (the rule `AccessibilityTest.kt` keeps on the source) in pixels at the device's `wm density`, with a 48 px floor |

A `journey_settings` step runs first: quiet hours off and the routine check-ins pushed to every
240 minutes for the demo person, so the journey's own questions are the only ones asked whatever
hour the runner is in; any question left open is answered first (`journey_stray_question_answered`).
Every alert a group opens is resolved by the coordinator at the end of the group. The journey
ends with `no_app_crash_after_journey`.

### Proved without a device

- **Pure helpers, with recorded dumps and fake adb and API transports**:
  `supracloud-jarvis/ira/tests/readiness/test_watch_smoke_journey.py` (12 tests): group selection,
  the accessibility checks on a recorded 454 × 454 home dump, the dose checks (a carer-written dose
  is never the wrist's), reminder check-ins, one fall alert with two detections, the wrist's words
  matched against the Kotlin sources, boot and service checks, the broadcast arguments, the adb
  wrist tapping the button's centre, and the API-only wrist holding Help offline, sending it once
  with its key, attaching a second detection, and answering a reminder by button. Negative controls
  were run by breaking the code (an unknown group accepted; a carer-written dose accepted; height
  ignored in the touch-target check; Help sent twice; the receiver declared in the main manifest;
  the workflow without the `--steps` flag): each made exactly its test fail, and the code was restored.
- **The synthetic fall trigger is debug-only**: `watch/app/src/test/.../SyntheticFallReleaseTest.kt`
  (4 JVM tests, run here with a scratch SDK: pass) and `tests/readiness/test_watch_synthetic_fall.py`
  (4 tests) scan every source set: the receiver and its manifest entry exist in `src/debug` alone,
  the main manifest declares only `BootReceiver`, and nothing in `main` calls `FallInjection.inject`.
  Negative control: a copy of the receiver in `src/release` made `no other source set names the
  receiver or its action` fail (`expected:<[]> but was:<[src/release/java/.../SyntheticFallReceiver.kt]>`);
  the copy was removed. `FallInjectionTest.kt` (3 tests) covers the extras parsing (Float, Int, Long
  and String extras; refused peaks and still times) and the flow to the service.
- **The API side of every group, against the demo API** (`--api-only`): the watch's calls played
  over HTTP with a device token (`ApiWrist`), on this machine, database `ira_watchjourney`
  (provisioned, migrated to 123, seeded with `scripts/demo/seed_demo.py`), `scripts/demo/api.py` on
  port 8791, the worker scheduler running. See the record below.

### The API-only dry run (26 November 2026, this machine)


### Second emulator run of the extended journey (run 45910155640, 26 to 27 November 2026, 23:21 to 00:03 UTC)

With the quiet-face waits and the safer tap in place: **43 of 44 steps passed** on GitHub's
standard runner. Every core step (the host reached after 6 s of waiting, pairing in 2.3 s, Help on
the wrist and on the server), then: the medication reminder shown in 52 s, "Later" recorded as
nothing (reminder `snoozed`, zero taken doses), the second reminder's "Done" answered with the
confirmation question and the dose written only after it (`taken`, `confirmed_via: checkin`);
the family's check-in shown in 52 s and answered by touch; the synthetic fall countdown, a second
detection attached to the same incident (2 detections, 1 alert), the alert open with `no_answer`
after the countdown and resolved; Help during a fall countdown kept to the one alert and
"Sravani Perni is on the way." on the wrist after the acknowledgement; a Help queued offline
delivered exactly once when the network came back, and again across `adb reboot` with the service
running again 26 s after boot; the background sensor permission revoked, reported by the watch
(`background_sensor_permission: false`) and recovered when regranted; every clickable node on the
eleven dumped screens labelled (a card's words on its child count) and every button at least
44 dp; no crash. The one failure: `unpair_warning_shown`, because the "Unpair watch…" button sits
below the fold of the home column and the journey never scrolled to it (it now does), and the job
reached its 45-minute timeout while waiting (the two ten-minute quiet-face waits; the timeout is
now 60 minutes). The next run is the proof for that step and for the re-pairing that follows it.

### Third emulator run of the extended journey (run 45994124214, job 140489106512, commit `fdfa92b`, 27 November 2026)

43 of 44 steps passed again, with the host-network wait, the quiet face before the groups and the
scroll to the bottom of the home column in place. The one failure was `unpair_warning_shown`, and it
was the script's, not the app's: the screen dump that step saved shows the Unpair button on screen,
but Compose had merged the button's text ("Unpair watch…") into its description ("Unpair this watch.
Asks before doing anything."), and the script searched for the text alone, so it scrolled, waited
five minutes and gave up without tapping. The search now accepts the text or the description
(`UNPAIR_BUTTON` in `scripts/emulator/watch_smoke.py`), with a test that fails on the old search
(`test_the_unpair_button_is_found_by_its_description_when_compose_hides_its_text`). The next run is
the proof for the unpair steps.

### Fourth run: the whole extended journey passes (run 45999809729, job 140508259808, commit `5634dcb`, 27 November 2026, 12:37 to 13:08 UTC)

**Every step passed: 49 of 49** (`{"ok": true, "steps": 49}`). The earlier runs recorded fewer
steps because a failed step ends its group; this one ran every group to the end: install and
pairing; Help from the wrist and on the server; the stray question answered; the medication
reminder shown, an unclear answer not recorded, the confirmation asked and the dose taken in two
steps; a check-in answered by touch; the fall countdown with a second detection attached, the alert
after it and its resolution; Help during a fall countdown making one alert, the responder status on
the wrist; Help queued offline and delivered once, and again across a reboot with monitoring
resumed; the sensor permission revoked (shown and reported) and granted again; the unpair warning,
the server's record, the old token refused (401), pairing again isolated from the old pairing; the
accessibility labels and touch targets; no app crash after the journey.

Register items this run moves to verified, each for what the run shows and no more: C-23.c
(automated accessibility checks on the watch app), D1.d (a fall detection, injected through the
debug-only receiver, feeding the countdown), D1.e (Help, its delivery and the responder status on
the wrist), D1.h (a reminder shown and answered on the wrist). Not moved, because the run does not
show all of their titles: D1.a (voice check-ins; the run answers by touch), D1.g (battery state),
C-21.b (the refusal to unpair while Help is pending), C-20.a (a crash, as against a reboot).

## Round 11: battery, carer visit, unpairing refused, the thank-you (28 November 2026)

Run 46146704537, job 140984229449, commit `242f25a` (pull request 134), the same runner, image and
backend as above. **56 of 57 steps passed**; the one that failed is below.

| Step | What it shows | Result |
|---|---|---|
| `battery_level_reported` (D1.g) | the emulator's battery set to 23 % (`dumpsys battery set level 23`): the server's device row reads `battery_percent` 23 | pass, 57.3 s |
| `checkin_thank_you_clears` | after a check-in answered by touch, "Thank you." gives way to "All well" | pass, 35.7 s |
| `visit_expected_shown` (D1.b) | a visit planned through the API: "Uday is coming at 13:38." on the watch (after the closed-Help notice's ten minutes) | pass, 483.6 s |
| `visit_here_shown_and_acknowledged` (D1.b) | the carer marked arrived: "Uday is here." on the watch, and the server records the watch's acknowledgement (`acknowledged_at` 13:26:36) | pass, 5.2 s |
| `visit_departed_cleared` (D1.b) | the carer departed: the face back to "All well" | pass, 4.6 s |
| `unpair_refused_during_fall_check` (C-21.b) | a fall while the unpair screen is open: "A fall check is running. Answer it first.", and no Unpair button | pass, 15.7 s |
| `fall_check_answered_still_paired` (C-21.b) | back home, "I'm OK" answers the countdown (outcome `answered_ok`); the watch is still paired | pass, 16.6 s |
| `help_alert_carries_location` (D1.f) | the Help alert carries the emulator's GPS fix | **fail**: no position on the alert |

Two defects found on the way, both fixed with unit tests:

1. **The battery level never reached the server.** The watch read `BATTERY_PROPERTY_CAPACITY`, which
   the emulator, like some devices, answers as unsupported (run 46143245120: `battery_percent` null).
   It now reads the system's battery broadcast, with the property as fallback (`BatteryLevelTest`).
2. **"Thank you." stayed on the face until a restart**, above "No connection" and the visit line. It
   now clears after 30 seconds, and "No connection" ranks above it (`FaceTextTest`).

The journey also signs in again when its session expires (run 46143245120 stopped two groups with 401
after 35 minutes). D1.f stays accepted, not verified: the next run asks the fused provider first and
records the emulator's location state if there is still no position.
