# Accepted but not verified: one row per requirement (28 November 2026)

Every requirement in the `accepted` state, read individually: what evidence it has, and the one
route to integration evidence of its whole title. Generated from `docs/progress/requirements.json`;
nothing here changes a state. Where the measure does not reasonably apply (a build, a CI job, a
document, a dataset decision), the row says so instead of inventing a test.

| Route | Requirements |
|---|---|
| not an integration target | 18 |
| the AOSP build host (Cuttlefish) | 17 |
| watch emulator journey on CI | 15 (D1.b, D1.f, D1.g and C-21.b verified since, 28 November) |
| a physical watch or phone | 7 |
| browser or database, here | 4 (C-10.b, C-10.c, C-12.b and C-23.b verified since, 28 November) |
| a staging server | 4 |
| a reviewer | 1 |

| Requirement | Title | Evidence now | Route to verification | How |
|---|---|---|---|---|
| C-10.a | Person record refreshes every 30 s and says when it last updated (B1) | unit_test | not an integration target | its browser verification is its own row, C-10.d (verified); counting the same run twice would inflate the measure |
| C-12.a | Phone Talk client reads the NDJSON stream incrementally (expo/fetch) | unit_test | a physical watch or phone | expo/fetch streaming is the native client; the web build uses the browser's fetch, so a browser run would not exercise it |
| C-13.a | Phone speaks IRA's answer (speech out) | unit_test | a physical watch or phone | text-to-speech on a phone |
| C-13.b | Phone listens for one utterance and keeps no recording (speech in) | unit_test | a physical watch or phone | a microphone on a phone |
| C-15.a | Push token is unregistered on sign-out, and being offline does not block sign-out | unit_test | a physical watch or phone | push tokens need Firebase or APNs on a device; the web build has no push |
| C-17.a | Watch speech recognition: one listener at a time, destroyed on every path, crash recovery  | build, unit_test | a physical watch or phone | the emulator has no speech recogniser to drive |
| C-18.a | Watch text-to-speech: time limit, volume restored, one at a time (D2) | build, unit_test | watch emulator journey on CI | watch text-to-speech runs on the emulator; checking the time limit and the restored volume needs a journey step |
| C-19.a | Watch storage encrypted per wearer with a Keystore key (D5) | build, unit_test | watch emulator journey on CI | files on the emulator read with run-as: not readable as plain text; per-wearer key |
| C-19.c | Watch storage never falls back to plain text for sensitive data when sealing fails; Help i | unit_test | a physical watch or phone | a sealing failure needs a Keystore fault, which only a device's TEE or a test double gives |
| C-20.a | Offline queue writes are atomic and survive a crash; unreadable lines are reported (D5) | unit_test | watch emulator journey on CI | kill the app mid-write on the emulator; the queue is intact after restart |
| C-20.b | Outbox events are on disk before sending, keep their key and back off on failure | unit_test | watch emulator journey on CI | partly shown by the offline and reboot groups (on disk, delivered once); back-off needs a failing server step |
| C-21.a | Pairing data kept per wearer; re-pairing another wearer does not read the previous one's d | unit_test | watch emulator journey on CI | pair a second wearer on the same emulator; the first wearer's data is not read |
| C-22.c | System-wide security review of the integrated stack (threat model, secrets, dependencies,  | browser, docs_only, unit_test | a reviewer | a system-wide security review: an independent reviewer (C-22.d is the penetration test) |
| C-24.a | Watch keeps retryable server failures (5xx, 425, 429) and drops only permanent refusals (E | unit_test | watch emulator journey on CI | a journey step with the server answering 503 then 200: the request is kept and sent |
| C-5.a | A negative, a doubt or trouble never ends the watch fall countdown as OK (A4b) | build, unit_test | watch emulator journey on CI | a synthetic fall's countdown answered with a doubt: the alert must stay open |
| D1.a | Watch check-ins by voice and touch | build, unit_test | a physical watch or phone | touch is shown on the emulator; voice needs a microphone |
| D1.c | Watch heart-rate and wrist readings batched and sent | unit_test | watch emulator journey on CI | simulated readings from the watch arrive at the server, marked simulated |
| D1b.a | AOSP flavour builds and unit-tests with no Google service dependency | build | the AOSP build host (Cuttlefish) | the AOSP flavour on Cuttlefish |
| D1b.b | Platform layer behind interfaces (one app for Wear OS and AOSP) | unit_test | the AOSP build host (Cuttlefish) | the AOSP flavour on Cuttlefish |
| D2.f | Phone app typecheck and tests in CI | build | not an integration target | a CI build-and-test job: its evidence is the job |
| D3.j | Dashboard typecheck, tests and production build in CI | build | not an integration target | a CI build-and-test job: its evidence is the job |
| D4.a | One capability list held by backend, watch and dashboard | unit_test | not an integration target | one list in three code bases: a consistency check, already a unit test |
| E2.a | Cuttlefish product makefile and product registration | config_check, unit_test | the AOSP build host (Cuttlefish) |  |
| E2.b | Privileged prebuilt watch app with a privapp allowlist that matches the manifest | config_check, unit_test | the AOSP build host (Cuttlefish) |  |
| E2.c | First-boot permission grants (proposal) | config_check, unit_test | the AOSP build host (Cuttlefish) |  |
| E2.d | Framework overlay | config_check, unit_test | the AOSP build host (Cuttlefish) |  |
| E2.e | Boot animation specification | config_check, docs_only, unit_test | the AOSP build host (Cuttlefish) |  |
| E2.g | Cuttlefish build script refuses on low resources and never downloads without the opt-in | config_check, unit_test | the AOSP build host (Cuttlefish) | the resource refusal itself is host-checked |
| E2.j | Launcher: watch app is the HOME activity in the aosp flavour, with lock-screen attributes | unit_test | the AOSP build host (Cuttlefish) |  |
| E2.k | Launcher: stock AOSP launcher removed from the image (overrides) | config_check, docs_only, unit_test | the AOSP build host (Cuttlefish) |  |
| E2.m | Device-owner policy app: developer options and factory reset behind an administrator | db_test, docs_only, unit_test | the AOSP build host (Cuttlefish) | the server half has database tests; the device-owner half needs the image |
| E2.o | Deployment-administrator instructions: an authority no care role implies, signed single-us | db_test, docs_only, unit_test | the AOSP build host (Cuttlefish) | the server half has database tests; the watch collecting an instruction needs the image or a device |
| E3.a | Sensor HAL contract (types, units, rates, batching, wake) | docs_only, unit_test | the AOSP build host (Cuttlefish) |  |
| E3.b | App-level simulated sensor source, readings marked simulated | config_check, unit_test | watch emulator journey on CI | the app-level simulated source on the emulator: readings marked simulated |
| E3.c | Simulated sensors HAL driver: heart rate | config_check, unit_test | the AOSP build host (Cuttlefish) |  |
| E3.d | Simulated sensors HAL driver: accelerometer and gyroscope | config_check, unit_test | the AOSP build host (Cuttlefish) |  |
| E3.e | Simulated sensors HAL driver: off-body detection | config_check, unit_test | the AOSP build host (Cuttlefish) |  |
| E6.a | OS build instructions, every step marked as not run | docs_only, unit_test | the AOSP build host (Cuttlefish) | build instructions are proved by running them |
| F1.b | Model-weight licence register for every shipped or downloaded model | config_check, docs_only, unit_test | not an integration target | a licence register: re-read at source (LICENCE_SOURCE_CHECK.md) |
| F2.d | New versioned care-conversation dataset | docs_only, unit_test | not an integration target | a dataset: its checks are its tests |
| F3.c | Heart model card | docs_only, model_eval | not an integration target | a model card: its numbers are model evaluations already cited |
| F4.d | SisFall-trained model kept out of every build, debug included, and the OS image (D-077, D- | build, unit_test | not an integration target | an exclusion from builds: checked in every build |
| F4.i | Confirmation stage after a suspected fall: a still wrist is always asked, a wrist that kep | docs_only, model_eval, unit_test | not an integration target | switched off after failing its gate twice (D-110, D-113): nothing to verify in the product |
| F7.a | Wrist IMU activity dataset with a licence that allows commercial use selected | docs_only, model_eval | not an integration target | a dataset choice |
| F7.d | Activity inference integrated in the watch app | build, unit_test | watch emulator journey on CI | activity inference on the emulator with replayed motion |
| F7.e | Activity model card | docs_only, model_eval | not an integration target | a model card |
| F7.f | Activity estimate summarised into hourly periods on the watch and sent only under the acti | build, docs_only, unit_test | watch emulator journey on CI | hourly summaries sent only under consent, from the emulator (the server half is browser-verified, F7.i) |
| F8.a | Speech engine licences reviewed (OmniVoice is CC BY-NC) | docs_only, unit_test | not an integration target | a licence review |
| G1.b | API and dashboard Docker images rebuilt and checked in CI | build, unit_test | not an integration target | image builds in CI; running them is the deployment-guide workflow (G4.a) |
| G1.d | HTTPS reverse proxy configuration | build, config_check | a staging server | HTTPS through Caddy with a real certificate needs a domain; the deployment-guide workflow runs it on localhost |
| G2.g | Outage drills on staging after the remediation | db_test | a staging server | drills on staging |
| G2.h | Encrypted off-site backups with a restore drill | db_test, docs_only, unit_test | a staging server | an off-site bucket |
| G4.c | Operations runbooks (incident, restore, key rotation) | config_check, db_test, docs_only, unit_test | a staging server | the provider-key rotations and the compose wrappers |
| K.a | Backend unit suite on integrated main | config_check, unit_test | not an integration target | a CI suite |
| K.c | Dashboard, phone and watch builds on integrated main | build | not an integration target | CI builds |
| K.d | Licence check of shipped dependencies | config_check, docs_only, unit_test | not an integration target | a licence check |
| K.e | Secret scan in CI | build, config_check, docs_only, unit_test | not an integration target | a CI scan |
| L.c | Final evidence pack index generated from requirements.json | build, config_check, unit_test | not an integration target | a generated index |
