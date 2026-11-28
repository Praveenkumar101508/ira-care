# IRA CARE: round 10 report (27 and 28 November 2026)

Every figure comes from `scripts/completion_summary.py` over `docs/progress/requirements.json`;
nothing is rounded up. Work ran on the build machine and GitHub's free runners; the owner's
personal Mac was not used for anything, and nothing was bought.

## Mandatory: must be fixed (carried from pull request 132)

Pull request 132 was merged on 28 November 2026 with **one check failing**: the iOS simulator job's
authenticated care journey (`.github/workflows/ios-simulator.yml`, `scripts/simulator/ios_care_journey.py`).
`main` shows that red check, and every pull request that touches the phone or watch apps will fail
the same job, until this is fixed. It must not be released, marked verified or reported as passing
before then.

| Run | Where it stopped | Fix |
|---|---|---|
| 46083831505 | 12 of 16 steps; the sign-in was lost at the restart | signed to run locally (in `main`) |
| 46090062926 | sign-in: iOS autocorrect changed the username | no autocorrect on account fields (on the branch, not yet pushed) |
| 46093555159 | sign-in: iOS's "Save Password?" sheet covered the app | answer it "Not Now" (on the branch, not yet pushed) |

Done means: the native-checks run on `main` (or on the follow-up pull request) green, the iOS journey
16 of 16, and D2.h's journey recorded in `docs/evidence/builds/IOS_SIMULATOR.md`.

## The measures

| Measure | Before 28 November | Now |
|---|---|---|
| Hardware-independent software implementation | 179 / 195 | 179 / 195, 91.8 % |
| Hardware-independent software acceptance | 173 / 195 | 174 / 195, 89.2 % |
| Software verification (database, browser, emulator, model evaluation, device) | 91 / 210 | **109 / 210, 51.9 %** |
| AI development (accepted) | 41 / 48 | 41 / 48, 85.4 % |
| OS development (accepted, hardware-independent) | 17 / 29 | 17 / 29, 58.6 % |
| Hardware integration | 0 / 14 | 0 / 14 |
| External and commercial readiness | 2 / 34 | 2 / 34 |
| Completed requirements (accepted or better) | 175 / 258 | 176 / 258 |

Round 10 began at 82 of 210 verified; the first part (27 November) took it to 91.

## Where every requirement stands

| State | Count | What it means |
|---|---|---|
| Physically validated (`device_validated`) | **0** | nothing has run on a physical watch or phone |
| Verified | 109 | integration evidence that shows the whole title: real PostgreSQL, a real browser, an emulator, a model evaluation |
| Accepted | 67 | functional evidence (unit tests, builds) without integration evidence of the whole title |
| Implemented | 28 | code or a document, waiting for a reviewer, a build host, a device or a run (18 of them external documents, 5 hardware documents) |
| Blocked by something outside the project | 53 | each names what it waits for (`blocked_by` in the register; `STATUS.md`, "Not done, and why") |
| Not started | 1 | G2.e, the 24-hour soak: a free runner stops at 6 hours |

## Failed, and kept failed

- **The fall confirmation stage (F4.i, D-110)** failed its gate on recorded falls: the shipped
  pipeline caught 217 of 316 BITS-2 falls, and the confirmation stage would have lost two of them
  (user4_fall3, user41_fall1). It stays switched off; a readiness test holds it off.
- **The conversation hold-outs (D-111):** 31 of 32 turns on the first round 10 set, and 39 of 45 on
  the second after the rule fixes. The remainder are written up in
  `docs/ai/conversation/ROUND10_CONVERSATION.md`; four repeated worries were not redirected, and
  that redirect is the model's job, so only a real-model run can measure it (not done this round:
  the machine had no model server, and installing one was refused by the session's permissions).
- **The iOS authenticated journey (D2.h's journey):** not yet passed. It reached 12 of 16 steps
  (run 46083831505). The next run launched the locally signed app and stopped at sign-in, where
  iOS autocorrect rewrote the username; that is fixed and committed (below), but not yet run.

## What round 10 did

**First part (27 November, pull request 132):**
- OmniVoice (CC BY-NC weights) refused in the commercial edition (D-109).
- The fall confirmation stage evaluated on recorded data and kept off (D-110).
- Conversation safety rules: head injury, chest tightness, stroke, breathlessness and bleeding
  phrases; questions about the Help button answered, not escalated; dose times that name another
  dose (D-111).
- The deployment administrator journey in a real browser, 4 of 4, with two defects fixed (D-112).
- Licences re-read at source (44 sources agree), UMAFall version 8 provenance workflow, the
  deployment-guide and 5.5-hour soak workflows.
- Android phone journey 18 of 18; watch journey 49 steps; the iOS journey written.

**Second part (28 November), all on the branch:**

| What | Evidence | Requirements |
|---|---|---|
| Eight browser tests against the running server and the production dashboard build | `docs/evidence/browser/ROUND10_VERIFICATION_BROWSER.md` | J.h, G2.c, C-11.a, D4.b, C-16.b, C-23.a, C-23.d, F7.i verified |
| The whole care journey's logs read for personal data; real driver errors' text kept out | `test_care_logs_no_personal_data_db.py`, `test_log_exception_text_db.py` | C-6.a, C-6.b verified |
| The runbook drills: the worker stopped, key rotation | `docs/evidence/ops/DRILLS_ROUND10.md`, 10 of 10 | G4.c accepted, G2.a verified |
| The phone app's web build in a real browser | `docs/evidence/browser/ROUND10_PHONE_WEB.md` | C-11.b, D2.b, D2.c, D2.d, C-16.a verified |
| The Android journey's three experiences in one app | `PHONE_EMULATOR_SMOKE.md` | D2.a verified |
| An evidence label corrected (a PostgreSQL test filed as a unit test) | `VERIFICATION.md` | G2.d verified |

**Defects found by the new tests, all fixed with a test that fails without the fix:**
1. Unnamed form controls on a person's record (three time fields and a drop-down): critical for a
   screen reader. Found by axe in the browser; the unit accessibility test never opened that tab.
2. The alert banner's "I'm on my way" sat inside the banner's own pressable, which iOS VoiceOver
   treats as one element, so VoiceOver could not reach the button.
3. iOS autocorrect rewrote usernames, email addresses and server addresses on the sign-in and
   invitation screens.
4. Links inside text on the dashboard were told apart by colour alone (serious, WCAG 1.4.1);
   found by axe once the home page's attention list had items. Now underlined.
5. The phone showed a watch silent for hours as "on the wrist"; it now says "last known" after an
   hour, as the dashboard does.
6. The deployment's nightly backups (`deploy/backup.sh`) and fetched off-site copies were readable
   by every account on the host (0644); now 0600 in a 0700 folder, and CI checks it.

## What remains, and what each needs

| Item | Needs |
|---|---|
| iOS journey green; pull request 132 merged | the next native-checks run on the branch (the owner) |
| C-7.b (Talk only for the wearer) | a phone emulator run with the new check |
| G4.a deployment guide, G2.e interim soak, F1.g UMAFall | the three manual workflows, once on `main` |
| Distress redirects on the real model | a model server (Ollama with Gemma 4, free) on a machine the session may install on |
| G2.e 24-hour soak | a Linux server that stays up for a day |
| The OS image (E2.h onwards) | a build host: about 64 GB of memory, 400 GB of disk, KVM |
| Physical validation (0 so far) | a watch and a phone |
| Datasets | replies from KFall, SisFall, FallAllD, WEDA-FALL; a Mozilla Data Collective account |
| Reviews and sign-off | a clinical safety officer, a data protection officer, legal review, an MHRA adviser |
| Live messages | provider keys and the owner's decision to pay |

The owner's actions, in order, are in `docs/progress/NEXT.md`.
