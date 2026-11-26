# IRA CARE: remaining external dependency register

Prepared 25 November 2026 on `main` at `65c61fa`. It lists every requirement that
`python3 scripts/completion_summary.py` reports as `blocked_external` (61 on this commit), grouped
by what blocks them, followed by the owner's own outstanding actions and the one requirement
that is not started (the 24-hour soak). For each group: the exact dependency, why software
cannot remove it, what software preparation is complete, what is needed, and the next action.

Costs appear only where the repository holds a reliable figure (a measurement, a published price
with its date, or an official fee); otherwise the entry says **not estimated**. No supplier,
adviser or provider has been contacted, and nothing has been bought.

Re-run `python3 scripts/completion_summary.py` for the current list; it names the blocker of
every item, and this register must be updated when that list changes.

## Summary

| Group | Blocker | Requirements | Cost |
| --- | --- | --- | --- |
| A | Physical watches and phones, with device builds | C-5.b, C-12.c, C-13.c, C-17.b, C-19.b, C-21.d, C-23.e, D1.j, D2.i, F4.g, F7.k, J.f, K.h | not estimated |
| B | A host with KVM virtualisation | resolved: GitHub's free KVM runner (D1.i verified) | none |
| C | An AOSP build host (about 400 GB disk, 64 GB RAM) | E2.h, E2.i, E2.l, E2.n, E4.a, E4.b, E4.c, E4.e, E4.f, E6.c | not estimated |
| D | The target board, its BSP, suppliers, an ODM and funding | E3.f, E4.h, E4.i, E4.j, E6.d, D4.c, F3.d, F3.e, H1–H4.h, H1–H4.i, H1–H4.j | certification estimates only (below) |
| E | Signing-key custody by the founders | D1b.c, E4.g | none (keys are made on founder-controlled machines) |
| F | Apple and Google accounts | D2.j, D2.k | not estimated |
| G | Dataset authors' permission | F1.d, F1.e, F1.f, F1.g | none expected; not confirmed |
| H | A GPU session for the conversation fine-tune | F2.e, F2.f, F2.g | Kaggle's free GPU was used before (`docs/MODEL_CHOICE.md`) |
| I | Older-adult speech data and listeners | F8.c, F8.d, F8.e | not estimated |
| J | A rented server (owner's approval to spend) | G1.e, G2.f, G4.d | not estimated (prices unreadable, `docs/ops/HOSTING_PROPOSAL.md`) |
| K | Live delivery providers and a monitoring centre | G3.e, G3.f | not estimated |
| L | Independent assurance: penetration tester, Clinical Safety Officer, MHRA adviser, data protection officer | C-22.d, I.i, I.j, I.k | not estimated |

61 requirements in total. Not blocked, but not started: the 24-hour wall-clock soak (G2.e),
section M.

## A. Physical watches and phones

- **Requirements:** C-5.b (fall-countdown voice handling on a watch), C-12.c (streaming on a
  phone), C-13.c (phone speech in and out), C-17.b (watch speech recognition lifecycle),
  C-19.b (keystore key hardware-backed; needs a watch with a TEE or StrongBox), C-21.d (unpair on
  a watch), C-23.e (TalkBack and VoiceOver pass), D1.j (watch app on a watch), D2.i (phone app on
  Android and iPhone), F4.g (fall detection on the IRA watch with simulated falls), F7.k (battery
  cost of activity estimation), J.f (the care journey on a watch and phone), K.h (device runs on
  integrated `main`).
- **Why external:** these are properties of real hardware: microphones, speakers, the platform
  speech engines, the secure element, battery and screen readers. No emulator or build machine
  can stand in for them. This build container has no attached devices.
- **Software preparation complete:** the watch app builds in both flavours and its JVM tests
  run in CI; the family app's Android release build runs in CI; the speech harness can score
  transcripts from any recogniser (`docs/ai/speech/RESULTS.md`); the activity model's per-window
  cost is measured on the JVM (`docs/ai/activity/MODEL_CARD.md`); the sensor evaluation plan is
  written (`docs/hardware/SENSOR_EVALUATION_PLAN.md`).
- **Needed:** at least one Wear OS watch (Galaxy Watch 4 or later is the documented target), one
  Android phone and one iPhone, plus device builds (group F for iOS).
- **Next action:** the founders choose and buy test devices; then run the device checklist in
  `docs/DEVICE_SETUP.md`.
- **Cost:** not estimated.

## B. A host with KVM virtualisation (resolved, 26 November 2026)

- **Requirements:** D1.i (watch app on a Wear OS emulator through the care journey) and K.g
  (emulator runs of both apps on integrated `main`) are now **verified**.
- **How it was resolved:** GitHub's standard Linux runner exposes KVM, and it is free while the
  repository is public. `.github/workflows/native-checks.yml` runs the Wear OS journey on every
  pull request that changes the apps: install, pair, home and Help all pass, and the server records
  the alert (`docs/evidence/emulator/WEAR_EMULATOR_JOURNEY.md`).
- **Phone app:** the family app's release build reaches its sign-in screen on an Android 14
  emulator in the same checks (`docs/evidence/emulator/PHONE_EMULATOR_SMOKE.md`). Both run on
  pull requests merged into `main`, so K.g is verified too.
- **Cost:** none while the repository is public.

## C. An AOSP build host

- **Requirements:** E2.h (Cuttlefish image built), E2.i (boots with IRA preinstalled), E2.l (IRA as
  the home screen on a boot), E2.n (emergency UI from the lock screen), E4.a (verified boot with
  test keys), E4.b (software KeyMint used by the app), E4.c (signed A/B OTA applied), E4.e
  (rollback protection refuses an older image), E4.f (provisioning at first boot), E6.c (build
  steps run and uncertain names resolved).
- **Why external:** an AOSP build needs about 400 GB of free disk and 64 GB of RAM
  (source.android.com, as recorded in `docs/os/ARCHITECTURE.md`); the build script refused on this
  machine (42 GB free, 16 GB RAM, 4 cores, no /dev/kvm: `docs/progress/VERIFICATION.md`).
- **Software preparation complete:** the product layer (`os/device/ira/ira_watch_cf/`), the
  build and launch scripts (`os/scripts/build_cuttlefish.sh`, `os/scripts/launch_cuttlefish.sh`),
  the boot animation, the launcher override, simulated sensors, and the OTA release server with
  signed manifests (`supracloud-jarvis/ira/care/ota.py`), checked by
  `supracloud-jarvis/ira/tests/readiness/test_os_product_layer.py`. The names still uncertain are
  listed in `docs/os/BUILD.md`.
- **Needed:** a build host of the size above, with KVM for Cuttlefish.
- **Next action:** owner decision to rent or buy one; then run `os/scripts/build_cuttlefish.sh`.
- **Cost:** not estimated.

## D. The target board, suppliers, an ODM and funding

- **Requirements:** E3.f (real sensors HAL), E4.h (fused key hash, locked bootloader), E4.i
  (RPMB rollback storage), E4.j (attestation keys and eSIM), E6.d (supplier access to the BSP),
  D4.c (new sensors selected and validated), F3.d (learned heart selector in the product: needs a
  watch that gives the app raw PPG), F3.e (heart-rate accuracy on the IRA watch against an ECG
  reference), H1–H4.h (physical prototype), H1–H4.i (sensor bench evaluation), H1–H4.j (EVT build
  and tests).
- **Why external:** these need hardware that does not exist yet, and supplier processes under NDA
  (the chip vendor's board support package and signing). Current Wear OS watches give apps
  finished heart-rate values, not raw PPG, so F3.d cannot ship on them.
- **Software preparation complete:** the sensor HAL contract (`docs/hardware/SENSOR_HAL.md`), the
  supplier dependency list (`docs/os/QUALCOMM_DEPENDENCIES.md`), the engagement plan
  (`docs/hardware/QUALCOMM_ENGAGEMENT.md`), the ODM request-for-quotation specification
  (`docs/hardware/ODM_SPECIFICATION.md`), the EVT, DVT and PVT gates
  (`docs/hardware/EVT_DVT_PVT_ROADMAP.md`), the blueprint (`docs/hardware/BLUEPRINT.md`), the
  learned heart selector built and tested (`docs/ai/heart/MODEL_CARD.md`).
- **Needed:** funding; an ODM; a development board and BSP access; an ECG reference set-up and
  consented participants for F3.e.
- **Next action:** after funding, send the ODM specification for quotation and open the chip
  vendor engagement.
- **Cost:** hardware unit cost **not estimated** (no quotation). Certification estimates from
  published sources are in `docs/hardware/BLUEPRINT.md` section 8.9 (for example GCF membership
  €5,000 to €10,000 a year, official; radio testing €1,500 to €3,000, estimate).

## E. Signing keys, created by the founders

- **Requirements:** D1b.c (a signed `aospRelease` APK), E4.g (release, platform and verified-boot
  key custody).
- **Why external:** the owner decided that release keys stay in the founders' custody and are
  never created in a build container or CI runner (`docs/security/SIGNING_KEYS.md`).
- **Software preparation complete:** the build reads keys only from outside the repository;
  `supracloud-jarvis/ira/tests/readiness/test_signing_keys.py` checks that no key material is
  tracked and that every key the code reads is listed; the procedure for each key is written.
- **Needed:** the founders create the keys on a machine they control, following
  `docs/security/SIGNING_KEYS.md`.
- **Next action:** founders.
- **Cost:** none for the keys themselves.

## F. Apple and Google accounts, and a Mac

- **Requirements:** D2.j (Google Play listing), D2.k (App Store listing). D2.h, the iOS build, is
  no longer blocked: the unsigned simulator build compiles on GitHub's free macOS runner
  (`docs/evidence/builds/IOS_SIMULATOR.md`); a signed device build still needs the Apple account.
- **Why external:** store submission needs developer accounts in the company's name; an iOS build
  needs macOS and Apple signing.
- **Software preparation complete:** the store listings are drafted (`docs/store/FAMILY_APP_LISTING.md`,
  `docs/store/WEAR_OS_LISTING.md`); the Android release build runs in CI.
- **Needed:** an Apple developer account, a Google Play developer account, a Mac.
- **Next action:** founders open the accounts.
- **Cost:** not estimated (fees not read at source here).

## G. Dataset authors' permission

- **Requirements:** F1.d (KFall), F1.e (FallAllD), F1.f (SisFall terms, or the owner accepts the
  risk), F1.g (UMAFall: CC BY 4.0 against a CC BY-NC record). F4.f (cross-dataset fall
  evaluation) no longer waits here: on 26 November 2026 it was done on WEDA-FALL, whose author
  added an ODC-By 1.0 licence (`docs/ai/fall/CROSS_DATASET_WEDA.md`). Because a separate check found
  no clear licence, WEDA-FALL's commercial use is recorded as `unclear`, and a permission request
  **was sent to its authors on 26 November 2026** (`docs/legal/dataset_permission_requests/WEDAFALL.md`);
  no further use until they reply.
- **Why external:** only the authors can grant or clarify their terms.
- **Software preparation complete:** four request drafts and one sent request (`docs/legal/dataset_permission_requests/README.md`
  and the files beside it); the shipped fall model uses only CC BY 4.0 data (D-080); SisFall is in
  no build (D-082).
- **Needed:** the founders send the requests.
- **Next action:** see owner item O2 below: the recipients are not yet verified.
- **Cost:** none expected; not confirmed.

## H. A GPU session for the conversation fine-tune

- **Requirements:** F2.e (a new LoRA or QLoRA run with a dated record), F2.f (evaluated on held-out
  prompts and the product evaluation), F2.g (promoted only if it beats the base model on safety
  and product measures).
- **Why external:** the run uses a Kaggle GPU session on the owner's account.
- **Software preparation complete:** the training script (`scripts/training/kaggle_conversation_qlora.py`),
  the runner (`scripts/training/run_kaggle.py`), the synthetic dataset
  (`docs/ai/conversation/DATASET_CARD.md`), and the evaluation set. The previous attempt did not
  beat the base model and was not shipped (`docs/MODEL_CHOICE.md`).
- **Needed:** a GPU session on the owner's account.
- **Next action:** owner.
- **Cost:** the previous runs used Kaggle's free GPU (about 2.6 h over four attempts,
  `docs/MODEL_CHOICE.md`).

## I. Older-adult speech data and listeners

- **Requirements:** F8.c (accuracy on a public older-adult speech set: Mozilla Common Voice
  through the Mozilla Data Collective), F8.d (consented recordings of older people), F8.e
  (synthesis intelligibility with older listeners).
- **Why external:** Common Voice downloads need an account that accepts the Data Collective's
  terms, which is the owner's to accept, and the site was unreachable from the build machine
  (`docs/DATASETS.md`). Recordings and listening sessions need consenting participants.
- **Software preparation complete:** the word-error-rate harness and scoring
  (`docs/ai/speech/RESULTS.md`: 4.16 % to 4.24 % on read adult speech, LibriSpeech test-clean).
- **Needed:** a Mozilla Data Collective account; consented participants under an approved
  protocol (`docs/compliance/PILOT_PROTOCOL.md`).
- **Next action:** owner opens the account.
- **Cost:** not estimated.

## J. A rented server

- **Requirements:** G1.e (staging on a rented server), G2.f (capacity load test on a
  representative deployment), G4.d (server rented and hosting agreed).
- **Why external:** it needs the owner's approval to spend.
- **Software preparation complete:** the deployment guide (`docs/DEPLOY.md`), compose files with
  CPU, GPU and no-model profiles, a clean-clone proof (`scripts/staging/clean_clone_proof.sh`),
  the load generator (`scripts/staging/containers_load.py`), the hosting proposal and cost script
  (`docs/ops/HOSTING_PROPOSAL.md`, `scripts/hosting_cost.py`), runbooks (`docs/ops/RUNBOOKS.md`).
- **Needed:** a UK-region VM (4 vCPU, 16 GB, 100 GB disk for the pilot tier), object storage and a
  domain.
- **Next action:** the owner fills `docs/ops/hosting_prices.json` from the chosen provider's
  published prices and decides.
- **Cost:** not estimated: the providers' price pages were unreachable from the build environment.

## K. Live delivery and a monitoring centre

- **Requirements:** G3.e (live delivery through a real provider), G3.f (monitoring-centre
  contract).
- **Why external:** provider credentials and a decision to pay; a contract with an alarm-receiving
  centre.
- **Software preparation complete:** the text and call adapter (off until
  `IRA_CARE_TWILIO_ACCOUNT_SID`, `IRA_CARE_TWILIO_AUTH_TOKEN` and `IRA_CARE_TWILIO_FROM` are set),
  the SMTP email adapter, the Expo push provider, and the webhook monitoring-centre adapter
  (`supracloud-jarvis/ira/care/providers.py`, `supracloud-jarvis/ira/care/push.py`), tested in
  `supracloud-jarvis/ira/tests/tenancy/test_care_email_and_centre_db.py`. Emergency numbers are
  refused before any provider is reached.
- **Needed:** provider credentials in the server's secret store; a monitoring-centre contract.
- **Next action:** owner chooses providers.
- **Cost:** not estimated.

## L. Independent assurance

- **Requirements:** C-22.d (penetration test by a qualified tester), I.i (compliance documents
  checked against current ICO guidance by the operator's data protection officer; the MHRA half
  is done), I.j (Clinical Safety Officer review and sign-off), I.k (MHRA qualification and
  classification confirmed by a regulatory adviser).
- **Why external:** each is a judgement only a qualified, independent person may give. The ICO's
  guidance pages were also refused by the build environment's network.
- **Software preparation complete:** the security review and STRIDE threat model
  (`docs/security/REVIEW.md`, `docs/security/THREAT_MODEL.md`); the hazard log tied to tests
  (`docs/compliance/CLINICAL_SAFETY_CASE.md`); the intended purpose and the adviser's questions
  (`docs/compliance/INTENDED_PURPOSE.md`, `docs/compliance/MHRA_ADVISER_QUESTIONS.md`); the DPIA,
  privacy policy and terms (`docs/compliance/DPIA.md`, `docs/legal/PRIVACY_POLICY.md`,
  `docs/legal/TERMS_OF_USE.md`).
- **Needed:** a qualified penetration tester, a Clinical Safety Officer, an MHRA regulatory
  adviser, a data protection officer.
- **Next action:** owner decision to engage and spend.
- **Cost:** not estimated.

## M. Not started: the 24-hour wall-clock soak

- **Requirement:** G2.e (24-hour soak with recorded numbers). It is `not_started`, not
  `blocked_external`, because any machine that stays up for a day can run it.
- **Why it has not run:** this build container restarts (it rebooted during this session), so a
  24-hour run cannot complete here. A simulated day for 200 people is recorded instead
  (`docs/evidence/soak/SOAK.md`).
- **Software preparation complete:** `scripts/soak_24h.py`.
- **Needed:** a host that stays up for at least a day; the rented server (group J) would do.
- **Next action:** run it on the first server that is rented.
- **Cost:** none beyond group J.

## Owner actions outside the requirement list

| # | Action | State | Next action |
| --- | --- | --- | --- |
| O1 | Delete merged branches on GitHub | Pending the owner's permission: the session's access cannot delete branches (`docs/progress/FINAL_REPORT.md`, `docs/progress/NEXT.md`) | The owner deletes them from the GitHub branches page, or grants permission |
| O2 | Send the dataset permission requests (KFall, FallAllD, SisFall, UMAFall; WEDA-FALL **sent** 26 November 2026, reply awaited) | Four drafted, **not sent**. The recipients are **not verified**: the publishers' sites were unreachable from the build environment when the drafts were written, so no address was copied into them (`docs/legal/dataset_permission_requests/README.md`) | The founders open each official page, confirm the addressee and route, and send |
| O3 | Create the signing keys | Not created (group E) | Founders, per `docs/security/SIGNING_KEYS.md` |
| O4 | Mozilla Data Collective account | Not opened (group I) | Owner |
| O5 | Apple and Google developer accounts; a Mac | Not opened (group F) | Owner |
| O6 | AOSP build host (the KVM host for emulators is resolved) | None (group C) | Owner |
| O7 | Test devices; ODM | None (groups A and D) | Owner, after funding for the ODM |
| O8 | Provider credentials; monitoring-centre contract | None (group K) | Owner |
| O9 | Penetration tester; Clinical Safety Officer; MHRA adviser; DPO and the ICO check | None engaged (group L) | Owner |
| O10 | A 24-hour soak host | None (section M) | Owner, with group J |
| O11 | Record the Silero VAD model's licence | Found while writing this register: faster-whisper bundles the Silero VAD model for its voice-activity filter, and `docs/ai/MODEL_WEIGHTS.md` does not yet list it | A documentation change: read the licence at source and add a row |
