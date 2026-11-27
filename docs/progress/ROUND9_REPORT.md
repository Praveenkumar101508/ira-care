# Round 9 report: the independently achievable software, AI and OS work (26 to 27 November 2026)

The owner's directive for this round: finish every independently achievable requirement, fix the
real software gaps the repository itself recorded (memory growth, step-up authentication, the
administrator screen, the credential scanner, the skipped tests, the native journeys), extend the
AI evaluation, spend nothing, change no repository visibility, and report truthfully. This is the
report. Everything below is on pull request 128 (`praveen/vigilant-tesla-bhe022`), one squash
merge from `main` at `82b19ba`; nothing was pushed to `main`.

## The measures (register 1.10.0, `scripts/completion_summary.py`)

| Measure | Before (`82b19ba`, register 1.7.0) | After (register 1.10.0) |
| --- | --- | --- |
| Hardware-independent software implementation | 174 / 190, 91.6 % | 179 / 195, 91.8 % |
| Hardware-independent software acceptance | 153 / 190, 80.5 % | 173 / 195, 88.7 % |
| Software verification | 74 / 205, 36.1 % | 82 / 210, 39.0 % |
| AI development (accepted) | 35 / 46, 76.1 % | 41 / 48, 85.4 % |
| OS development (accepted, hardware-independent) | 5 / 28, 17.9 % | 17 / 29, 58.6 % |
| Hardware integration | 0 / 14 | 0 / 14 |
| External and commercial readiness | 1 / 34, 2.9 % | 1 / 34, 2.9 % |
| Atomic requirements | 253 | 258 (five added: E2.p, D3.l, F4.i, F5.d, G2.j) |
| Completed (accepted or better) | 154 | 174 |

Nothing left a denominator. The five new items are work the directive asked for that the register
did not hold (`PRODUCT_VISION_RECONCILIATION.md`). The verification percentage barely moves
because verification needs integration evidence (a database test, a browser, an emulator, a model
evaluation); the native journeys that would move it are written and have run once or twice each
on the free runners (below), and their next clean runs are what moves the items.

## What was done

| Gap the owner named | What exists now | Proof |
| --- | --- | --- |
| API memory grew 5.18 % an hour against a 5 % limit | The cause found (asyncpg closing idle connections and reopening them in bursts; prepared statements expiring) and fixed in the pool's settings; **a complete 1-hour soak passes every criterion at 1.996 % an hour** | `docs/evidence/soak/MEMORY_INVESTIGATION_ROUND9.md`, `WALLCLOCK_1H_ROUND9.md`, D-102, G2.j |
| Step-up authentication open (D-089) | `auth_time` and `amr` claims on every token, copied unchanged by a refresh; `POST /auth/step-up`; issue, approve and revoke of a device instruction refused unless the proof is at most ten minutes old and shows the second factor when enrolled | 47 tests (unit and database); `docs/security/STEP_UP_AUTHENTICATION.md`; D-095; E2.p verified; T43 controlled, S15 fixed |
| No deployment-administrator screen | `/admin/devices` in the dashboard: a refusal for anyone without the grant, history with audit trail, the three actions, the two-person reset, step-up confirmation | 22 tests (160 in the suite), build green; D-096; D3.l accepted |
| The missing credential-history scanner (D-004) | `scripts/security/published_credential_digests.py` scans every environment-file version on every ref and prints provenance only; the test proves it on a synthetic history and on this one | 20 tests; `docs/security/CREDENTIAL_HISTORY_SCAN.md`; D-097 |
| Tests skipping for environment reasons | Redis, role-separation and PyAV tests run in CI; the destructive failover test passed on its own database; one stale test fixed; seven inherited missing-file skips recorded for an owner decision | `docs/progress/INHERITED_TEST_SKIPS.md`; D-098 |
| Watch journey stopped at Help | The extended journey: medication two-step, check-in, fall countdown with a second detection, Help during a fall, offline queue, reboot, permission revoke and regrant, unpair and pairing again, accessibility; **49 of 49 steps passed on the emulator** after the script learnt to find the Unpair button by its description | `docs/evidence/emulator/WEAR_EMULATOR_JOURNEY.md` (run 45999809729); D-100; C-23.c, D1.d, D1.e, D1.h verified |
| Android stopped at the login screen | The authenticated journey on the debug build with its JavaScript bundled: sign in, record, plan, a Help alert acknowledged, dark mode, offline, sign-out, the wearer's Talk, the carer's isolated home; **18 of 18 on the emulator** after two rounds of script fixes | `docs/evidence/emulator/PHONE_EMULATOR_SMOKE.md` (run 45990580023); D-101, D-105; D2.g, C-14.a verified |
| iOS compiled but never launched | Built with its JavaScript bundled, booted, installed, launched, the sign-in screen reported by the development probe, still running, no crash report: **6 of 6 on the simulator** | `docs/evidence/builds/IOS_SIMULATOR.md` (run 45994124214); D-105; D2.h verified |
| OS items implemented but unaccepted | 76 host tests against an AOSP reference: manifest against allowlist and protection levels, first-boot grants, makefiles, overlay, launcher, boot animation, the sensors HAL and simulated drivers compiled and run; four product-layer defects a first build would have hit, fixed | `docs/os/HOST_CHECKS.md`; D-099; 13 OS items accepted |
| Fall false alarms from hand impacts | A confirmation stage: a still wrist is always asked, a wrist that kept its orientation with the hand carrying on or a burst of impacts is not; synthetic: no caught fall lost, alarms 88 to 62; **off by default** until the permitted recorded falls have been run | `docs/ai/fall/FALSE_ALARM_INVESTIGATION.md`; D-103; F4.i |
| AI safety and conversation depth | 170 original scenarios in seven families with a hold-out set; the red-flag rule reads the older person's idiom and the dose reader never confirms a broken-off, indefinite or other-medicine answer; the 162-turn set unchanged turn for turn; hold-out 37 of 41 with the misses recorded | `docs/ai/conversation/ROUND9_SCENARIOS.md`; D-104; F5.d |
| A forgotten or corrected day of memory still in a stored report | Forgetting or correcting a day rebuilds every stored report covering it, in the same transaction; other periods untouched; a second session sees nothing of it | `tests/tenancy/test_care_memory_reports_db.py` (fails on the earlier code); D-106; F6.b, F6.c |
| Older-adult speech accuracy (F8.c) | The WER harness reports by age bucket and 60 and over against under 60; the selection and the run written down; no dataset downloaded | `docs/ai/speech/OLDER_ADULT_EVAL_PLAN.md`, `tests/readiness/test_speech_wer_age_buckets.py`; F8.c stays blocked on the owner's access decision |
| The four reviewer items | Review packs with the criterion, the evidence, the limitations and the questions; **reviewed and accepted by Sravani Perni** (recorded by the owner, 27 November); F3.c, F7.a, F7.e, F8.a accepted | `docs/ai/review/`; D-107 |
| Operations drills | Encrypted backup and restore round trip; the 22-step care journey | `docs/evidence/ops/DRILLS_ROUND9.md` |

## Verification of the final tree

See `VERIFICATION.md` ("Round 9") for the exact counts of every suite run on the final tree:
backend unit and database suites, dashboard, phone app, watch (both flavours), lint, secret scan,
the native journeys on the runners, and the soak.

## What remains, and why

| Item | Blocker | Exact next action |
| --- | --- | --- |
| G2.e, the 24-hour soak | The build container stops its daemons when the session is idle; a day-long run needs a machine the owner controls | `docs/evidence/soak/HOW_TO_RUN_24H.md` on the Mac, under `caffeinate`; the memory criterion is now met |
| E2.h to E4.f, E6.c, the Cuttlefish image | A 64 GB, 400 GB, KVM x86-64 host; none available without spending; the host checks have removed what could be found without one | `docs/os/BUILD.md` on such a host (Essex research computing, a sponsor, or protected credits) |
| F4.i on real falls | BITS-2 and UCI HAR are on the owner's machine, not here | `docs/ai/fall/FALSE_ALARM_INVESTIGATION.md`, "the next step" |
| F1.d to F1.g, F8.c, F3.d, F3.e, F7.k | Dataset permissions (requests sent), the older-adult speech set, the IRA watch's raw PPG, physical devices | Owner: replies, the Mozilla Data Collective, devices |
| D1.j, D2.i, K.h, J.f, C-5.b, C-17.b, C-19.b, C-21.d, C-23.e | A physical watch and phone | Owner: devices |
| D1b.c, E4.g, D2.j, D2.k | Signing keys and store accounts | `docs/security/SIGNING_KEYS_STEP_BY_STEP.md`; the owner's decision on custody; no purchase made |
| C-22.d, I.i to I.k | Qualified (clinical, legal) reviewers | `docs/ai/review/`; the owner's contacts |
| G1.e, G2.f, G2.g, G3.e, G3.f, G4.d | A server, a provider, a monitoring centre: money | Unchanged |
| Hardware H1 to H4 | Funding, a board, an ODM | Unchanged |

## Where the tooling got in the way, and what was decided

- The round began with eleven parallel workstreams; they consumed the usage allowance before any
  finished and were lost to a restart. The rest of the round ran sequentially, one workstream at a
  time, and lost nothing further.
- The hosting environment stops background daemons whenever the session is idle: two soak attempts
  died that way at 18 and 43 minutes; the run that completed was held by a tracked process.
- The hosting environment asks after every turn for the commits to be re-authored under its own
  name; this was refused every time, as `docs/BUILD_RULES.md` and the owner require. Every commit
  is authored as the repository owner; GitHub marks them unverified as it does any unsigned commit.
- The native checks on the pull request are the independent runs of the three journeys; each push
  cancels the run before it, so pushes were batched.
