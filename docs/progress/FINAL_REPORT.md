# IRA CARE: final report

## Round 6: the final report (the master directive, 25 November 2026)

This is the final report the owner asked for: every requirement that software alone can finish
is now accepted or verified, or is recorded as blocked by something outside the software, with
that blocker named. One requirement is neither: the 24-hour wall-clock soak (G2.e), which needs a
machine that stays up for a day; this build container restarts, and a simulated day is recorded
instead (`docs/evidence/soak/SOAK.md`).

Fourteen pull requests, #81 to #94 (plus this one), each one branch squash-merged into `main`
with every required CI check green. The integrated run is in [`VERIFICATION.md`](VERIFICATION.md).

### GO / NO-GO

| Use | Decision | Why |
|---|---|---|
| Engineering demonstration with fictional people, test notification providers and a clearly labelled virtual or browser watch | **GO** | The 22-step care journey passes end to end against the real API and PostgreSQL, deterministically and with the real model chain (8 of 8 runs); the stack runs as containers with outage recovery, encrypted off-site backups and restore drills; the film was re-recorded on this stack. |
| Supervised pilot with real people | **NO-GO** | No Clinical Safety Officer has reviewed the hazard log (I.j); MHRA qualification is unconfirmed (I.k); nothing has run on a physical watch or phone; fall, heart, activity and speech accuracy are unmeasured on older adults and on the IRA watch; live SMS, call and email delivery is untested; no monitoring-centre contract. |
| Commercial release to real users | **NO-GO** | Everything the pilot needs, plus: signed store builds and store accounts, an iOS build, a hosting agreement on a rented server, an independent penetration test, and the IRA CARE OS image built and booted. (The phone app's build-tool advisories listed here at first were cleared without an SDK upgrade: `docs/security/REVIEW.md`, S6.) |

Area by area: [`GO_NO_GO.md`](GO_NO_GO.md). The owner's constraints held throughout: no real
patient data was collected, no real emergency message was sent, nothing was deployed to real
users, no money was spent, and no uncertain dataset licence was accepted (SisFall stays out of
release builds, D-077).

### Completion (requirements 1.4.0)

Snapshot "main after #94", `b78e6b5`; the denominators are unchanged since round 5.

| Measure | main after #80 | main after #94 | Change |
|---|---|---|---|
| Hardware-independent software implementation | 74.3 % | 87.4 % (160 / 183) | +13.1 points |
| Hardware-independent software acceptance | 63.4 % | 76.0 % (139 / 183) | +12.6 points |
| Software verification | 26.9 % | 32.5 % (64 / 197) | +5.6 points |
| AI development | 53.8 % | 64.1 % (25 / 39) | +10.3 points |
| OS development | 7.4 % | 14.8 % (4 / 27) | +7.4 points |
| Hardware integration | 0.0 % | 0.0 % (0 / 14) | 0 |
| External and commercial readiness | 0.0 % | 2.9 % (1 / 34) | +2.9 points |

140 of 245 requirements are accepted or better; 105 remain:
- **60** are blocked by something outside the software, each with its blocker named;
- **44** are implemented as documents, plans or configuration, and acceptance needs a reviewer, a build host or a device (for example the clinical safety case, the DPIA and the OS makefiles);
- **1**, the 24-hour soak, is not started.

None is device validated.

### Round 6: the merges

| PR | Branch | What it delivers |
| --- | --- | --- |
| 81 | `docs/final-verification` | Round 5 checkpoint: verification on `main`, a completion snapshot, requirement states reconciled with the merged code |
| 82 | `fix/android-build-evidence` | Native Android builds: the watch app built and tested here; the family app's release build added to CI as its own job (D2.g) |
| 83 | `fix/models-in-product` | The activity model running on the watch (F7.d); the learned heart selector recorded as blocked, because no product path carries raw PPG (F3.d) |
| 84 | `fix/ai-fall-v3` | Fall model v3 on licensed data passes its gate and ships in release builds, in recover mode beside the rule detector (D-080) |
| 85 | `fix/search-and-coordinator` | Workspace search; the care coordinator designation (migration 117, D-081); caseload and visit-plan screens |
| 86 | `fix/infra-adapters-backups` | Email (SMTP) and monitoring-centre (webhook) adapters, encrypted off-site backups, a simulated soak day, operations runbooks |
| 87 | `fix/speech-harness-evidence-index` | A speech accuracy harness (WER 4.24 % on LibriSpeech test-clean); the older-speaker evaluation recorded as blocked; an evidence index built in CI |
| 88 | `fix/os-testable-pieces` | IRA CARE OS pieces testable without an image: OTA release server (migration 120), device policy, boot animation, launcher override, simulated sensors |
| 89 | `docs/remaining-documents` | Threat model, pilot protocol, MHRA guidance check, hosting model with no prices, hardware plans, endorsement pack, GO/NO-GO by area |
| 90 | `fix/demo-real-model` | The 22-step care journey with the real model chain (`gemma4:e4b`, `gemma4:e2b`): 8 of 8 runs, every safety assertion held |
| 91 | `fix/conversation-dataset` | A versioned, synthetic care-conversation dataset (398 examples) validated against the product's own readers (F2.d) |
| 92 | `fix/watch-reminders` | Reminders answered on the wrist: Done, Later and Not today; a medicine counts as taken only after a second Done (D1.h) |
| 93 | `fix/film-rerecord` | The narrated film re-recorded live on the remediated stack (J.g) |
| 94 | `fix/licence-check` | An automated licence check of shipped Python, dashboard and phone-app dependencies, run in CI (K.d); one open finding for the owner (below) |

### AI models, honestly

| Model | Data (licence) | Result | Status |
|---|---|---|---|
| Fall v3 | BITS-2 (CC BY 4.0) + UCI HAR (CC BY 4.0) | with the rule detector, on the same 316 held-out falls and 2.233 hours of daily activity: 246 falls caught (sensitivity 0.7785) against 195 (0.6171) for the rule alone; at most 10.30 alarms per hour against 52.84 | ships in release builds (D-080) |
| Fall v1 | SisFall (licence unconfirmed) | evaluated earlier | debug builds only (D-077) |
| Heart | PPG-DaLiA (CC BY 4.0) | classical 13.28 ± 5.01 bpm, learned selector 7.75 ± 3.23 bpm | classical in use; learned selector blocked on raw PPG (F3.d) |
| Activity | WISDM (CC BY 4.0) | macro-F1 0.790 across 51 people, subject-grouped | on the watch; no alert depends on it |
| Speech recognition | faster-whisper `small` (MIT); LibriSpeech test-clean (CC BY 4.0, evaluation only) | WER 4.24 % on read adult speech | in use; older-speaker accuracy unmeasured (F8.c, F8.d) |
| Conversation | `gemma4:e4b`, `gemma4:e2b` (Apache 2.0) behind IRA's rules | 22-step journey 8 of 8 with the real models | in use; the fine-tune awaits a GPU session (F2.e) |

None is validated on older adults or on the IRA watch, and none is a medical device.

### What remains, and who it needs

Not started, and achievable with a machine that stays up: the 24-hour wall-clock soak (G2.e).

Everything else that remains is blocked by something only the owner or a third party can provide;
`completion_summary.py` names the blocker for each one. Grouped:

- **Owner decisions and accounts:** release signing-key custody (D1b.c); a Kaggle GPU session for the conversation fine-tune (F2.e to F2.g); a Mozilla Data Collective account whose terms the owner accepts, for older-speaker evaluation (F8.c); Google Play and Apple developer accounts (D2.j, D2.k); approval to rent a server (G1.e, G2.f, G4.d); provider credentials for live SMS, calls and email (G3.e).
- **Third parties:** a monitoring-centre contract (G3.f); a qualified penetration tester (C-22.d); a Clinical Safety Officer (I.j); an MHRA regulatory adviser (I.k); the operator's data protection officer to read the ICO guidance (I.i); dataset authors for KFall, FallAllD and UMAFall (F1.g, F4.f).
- **Machines and devices:** a Mac with Apple signing (D2.h); a host with KVM for emulator runs (D1.i, K.g); an AOSP build host, about 400 GB of disk and 64 GB of RAM (E2.h onwards); physical watches and phones (C-5.b, D1.j, D2.i, J.f, K.h, and the other device rows); the IRA watch hardware, an ODM and funding (H1–H4.h to H1–H4.j).
- **A licensing decision:** PyAV (`av`), pulled in by the speech recogniser, declares BSD-3-Clause but bundles FFmpeg built with libx264 and libx265, which are GPL-2.0-or-later. It matters if container images are ever given to an operator rather than run by IRA itself. The options are to replace the audio decoding, use an FFmpeg build without them, or take legal advice (`docs/security/LICENCES.md`).
- **Owner actions already drafted:** send the dataset permission requests in `docs/legal/dataset_permission_requests/`; withdraw the Kaggle copy of the SisFall-trained model published under Apache 2.0.


## Round 5 checkpoint (the master directive, 25 November 2026)

A checkpoint, not the final report: the owner asked for the final report only after every
achievable software requirement is done. Work continues on the requirements that are neither
accepted nor blocked by something outside the software.

Eighteen pull requests, #63 to #80, each one branch squash-merged into `main` with every required
CI check green. Final verification ran on `main` at `eec19a0`; its numbers are in
[`VERIFICATION.md`](VERIFICATION.md). Completion is measured by the fixed requirements framework
of #71 (`requirements.json`, seven separate measures), not by the earlier weighted block.

### GO / NO-GO at this checkpoint

| Use | Decision | Why |
|---|---|---|
| Engineering demonstration with fictional people, test notification providers and a clearly labelled virtual or browser watch | **GO** | The 22-step care journey (D-079) passes end to end against the real API and PostgreSQL; the stack runs as containers with health checks, outage recovery and backups proved; every merged change passed CI. |
| Supervised pilot with real people | **NO-GO** | No Clinical Safety Officer has reviewed the hazard log (I.j); MHRA classification is unconfirmed (I.k); nothing has run on a physical watch or phone; heart, fall and activity accuracy are unmeasured on older adults and on the IRA watch; live SMS and call delivery is untested. |
| Commercial release to real users | **NO-GO** | Everything the pilot needs, plus: store accounts and signed builds, a hosting agreement, a monitoring-centre contract, an independent penetration test, and an Expo SDK upgrade to clear the build-tool advisories. |

The owner's own constraints stand: no real patient data was collected, no real emergency message
was sent, nothing was deployed to real users, and no uncertain dataset licence was accepted.

### Completion (requirements 1.4.0, snapshot "main after #80", `eec19a0`)

| Measure | main after #66 | main after #80 | Change |
|---|---|---|---|
| Hardware-independent software implementation | 68.4 % | 74.3 % (136 / 183) | +5.9 points |
| Hardware-independent software acceptance | 58.8 % | 63.4 % (116 / 183) | +4.6 points |
| Software verification | 23.6 % | 26.9 % (53 / 197) | +3.3 points |
| AI development | 45.9 % | 53.8 % (21 / 39) | +7.9 points |
| OS development | 7.4 % | 7.4 % (2 / 27) | 0 |
| Hardware integration | 0.0 % | 0.0 % (0 / 14) | 0 |
| External and commercial readiness | 0.0 % | 0.0 % (0 / 34) | 0 |

116 of 245 requirements are accepted or better and 129 remain; 46 of those are blocked by something
outside the software, each named by `completion_summary.py`. None is device validated. Six
requirements were added during the round and none removed (`COMPLETION_REGISTER.md`).

### Round 5: the merges

| PR | Branch | What it delivers |
| --- | --- | --- |
| 63 | `fix/care-memory-promises` | Check-in answers no longer written to the general memory store; old ones purged (migration 115) |
| 64 | `fix/demo-rerun` | Demo re-run on the remediated stack; two defects found and fixed |
| 65 | `fix/dataset-licence-register` | Dataset and model-weight licence register re-verified; SisFall's Zenodo copy found to be a third-party re-upload |
| 66 | `fix/fall-model-research-only` | The SisFall-trained fall model kept out of every release build (owner decision, D-077) |
| 67 | `fix/demo-pages-production-off` | Demo, presenter and simulator pages answer 404 in production builds |
| 68 | `fix/watch-degraded-storage` | The watch never writes sensitive data unencrypted when its key fails, and never loses a Help request |
| 69 | `fix/register-block-sync` | The old weighted block regenerated (superseded by #71) |
| 70 | `fix/no-false-all-well` | "All well" never shown when monitoring is paused, messages are failing, the page is stale or the watch is offline |
| 71 | `fix/completion-framework` | Fixed, versioned atomic requirements; seven measures; append-only, hash-chained history |
| 72 | `fix/security-review` | Security review: rate limits on the open care routes, dashboard security headers, patched build dependencies |
| 73 | `fix/compliance-traceability` | Hazard log H14 to H20 and a claims register, both checked by tests |
| 74 | `fix/care-plans-staff-roles` | Care plans as versioned, insert-only records; staff roles served to the dashboard (D-078); the table in the tenancy manifest and the adversarial suite |
| 75 | `fix/ai-fall-v2` | Fall model v2 on licensed data only (BITS-2): evaluated, fails its shipping gate by 5 of 316 falls, not shipped |
| 76 | `fix/ai-heart-learned` | Heart figures 13.28 and 7.75 bpm reproduced exactly; the learned selector as checked JSON trees, not yet used by any product path |
| 77 | `fix/ai-activity-wisdm` | Wrist activity model on WISDM: macro-F1 0.790 across 51 people; not yet used by any product path |
| 78 | `fix/staging-containers` | The stack run as containers; two critical image defects fixed; backups that cannot fail silently; images built in CI |
| 79 | `fix/care-journey-22` | The 22-step care journey as one end-to-end test |
| 80 | `fix/requirements-sync` | Requirement states recorded for the merged security, image and claims work |

### Round 5: AI models, honestly

| Model | Data (licence) | Result | Status |
|---|---|---|---|
| Fall v1 | SisFall, Zenodo re-upload (unconfirmed) | evaluated earlier | debug builds only (D-077) |
| Fall v2 | BITS-2 (CC BY 4.0) + UCI HAR (CC BY 4.0) | with the rule detector: sensitivity 0.601 against 0.617 for the rule alone, alarms per hour of activity 10.75 against 52.84 | not shipped: loses 5 of 316 falls; a decision for the owner and a clinical reviewer |
| Heart | PPG-DaLiA (CC BY 4.0) | classical 13.28 ± 5.01 bpm, learned 7.75 ± 3.23 bpm, both reproduced exactly | classical in use; learned selector portable and tested, not yet wired in |
| Activity | WISDM (CC BY 4.0) | macro-F1 0.790, accuracy 0.866, subject-grouped | in the backend, not yet wired in or on the watch |

None is validated on older adults or on the IRA watch, and none is a medical device.

### Round 5: needs the owner

- **Fall model v2:** ship it (fewer false alarms, five more missed falls in 316) or keep the rules alone. A decision for the owner and a clinical reviewer.
- **Withdraw the Kaggle copy** of the SisFall-trained model published under Apache 2.0.
- **Send the dataset permission requests** drafted in `docs/legal/dataset_permission_requests/` (SisFall, KFall, FallAllD).
- **External items, each blocking a named requirement:** a Clinical Safety Officer (I.j), an MHRA adviser (I.k), physical watches and phones, an AOSP build host, signing-key custody, store accounts, SMS and call provider credentials, a server, a monitoring-centre contract, and a penetration tester.


## Round 4 (the polish, 24 November 2026)

Four phases, each one branch and one pull request squash-merged into `main` with CI green,
each started from the latest `main`. Everything below is what a test, a measurement or a
recording in this repository shows.

### Round 4: the merges

| Phase | Branch | Pull request | What it delivers |
| --- | --- | --- | --- |
| R4-1 | `feat/fast-conversation` | 37 | The model kept loaded, one model in memory, a shorter prompt and context, `gemma4:e4b` then `gemma4:e2b`, sentences streamed, an instant acknowledgement, check-in questions prepared five minutes ahead (D-069 to D-071) |
| R4-2 | `fix/watch-design-renders` | 38 | One look for the watch (graphite case, coral Help button, the app's colours) everywhere; the exploded view in stack order with legible labels; eight renders, the 20-second turntable, glTF and viewer rebuilt (D-072) |
| R4-3 | `feat/demo-film-polish` | 39 | The film at 1080p with IRA's own words; four conversation faults and one wording fault found while recording, each fixed with a test (D-073) |
| R4-4 | `docs/polish-final` | 40 | The documents in line with the merged code; the Kaggle GPU measurement; this section |

### Round 4: the conversation, timed and scored

On the deployment-sized machine (4 vCPU Intel Xeon 2.10 GHz, 15.7 GB, no GPU, Ollama 0.40.18),
`docs/MODEL_CHOICE.md` ("Round 4"):

- **Quality, 162-turn evaluation:** `gemma4:e4b` 0.909 (the best measured), red-flag recall
  1.00; `gemma4:12b` 0.895; `gemma4:e2b` 0.882; `gemma4:e4b-it-qat` 0.844; `qwen3.5:2b` 0.834.
  Chosen: `gemma4:e4b`, then `gemma4:e2b`, then the set lines.
- **First words:** at once (0.0 s on the server; under 0.1 s through the talk endpoint on
  staging), from the rules' acknowledgement. Target 1.5 s p50: met.
- **First sentence from the model:** 2.8 s p50 (5.9 s p95) on the evaluation set; 3.5 to 5.5 s
  on whole talk turns on the staging stack (about 270 prompt tokens, read again in full on
  every turn). Target 3 s p50: met on the evaluation set, **not met** on a whole talk turn;
  round 3 took about 10 s, and 15 s when the model had unloaded.
- **On a small GPU (Kaggle's free Tesla T4, measured):** `gemma4:e4b` first sentence 0.18 s
  p50, `gemma4:12b` 0.94 s p50 (`docs/evidence/training/gpu_bench.json`). Server prices with a
  20 to 24 GB card, with sources, are in `docs/MODEL_CHOICE.md`.

### Round 4: what recording the film found

Phase 1's evaluation timed the model directly; the film timed the product. Recording against
the real model on staging showed that every talk turn was being answered from the set lines,
for four reasons, each now covered by a test that fails without its fix (D-073): a cold model
never loaded (each turn aborted its load, and the second model's load pushed the first out);
the warm request loaded the model with a different context, so the first turn loaded it again;
the 3-second first-token budget was shorter than a real turn's prompt takes on four cores (now
8 s); and the browser watch read the whole reply before speaking, holding the acknowledgement
back. A fifth fault was wording: "Have you taken your the evening tablet yet?".

### Round 4: where to look

- The film: `docs/film/ira_care.mp4` (1920 × 1080, 5 min 6 s, 15.2 MB); its words and how it is
  remade: `docs/film/NARRATION.md`.
- The renders: `docs/hardware/renders/` (`hero.png`, `front.png`, `side.png`, `back.png`,
  `exploded.png`, `section.png`, `wrist_dock.png`, `screen_home.png`); the turntable:
  `docs/hardware/turntable.mp4`.
- The viewer: `docs/hardware/viewer.html` (one file; open it in any browser, drag to turn,
  Explode to separate the layers, tap a part).
- The measurements: `docs/MODEL_CHOICE.md`, `docs/evidence/eval/round4/`,
  `docs/evidence/training/gpu_bench.json`, `docs/evidence/staging/clean_clone_proof.json`.

### Round 4: test counts

- CI on every pull request: backend lint, backend unit tests, backend database tests, the
  dashboard (type check, tests, build), the secret scan and the readiness check, all green
  before each merge.
- Local, phase 3: care unit tests 159 passed; care database tests against PostgreSQL 16 with
  pgvector 366 passed (8 skipped); dashboard 36 passed.
- Staging from a clean clone of the merged phase 3 tree, with the end-to-end browser tests:
  passed in 186 s.

### Round 4: the GPU profile (added after the round, 24 November 2026)

The owner chose the GPU option. `deploy/compose.gpu.yml` (D-074) runs the model server on an
NVIDIA GPU with `gemma4:12b` then `gemma4:e4b`, both loaded. Measured with the backend's own
code on Kaggle's Tesla T4 over the 162-turn evaluation, three passes: quality 0.903, red-flag
recall 1.00, check-in answers read right 12 of 15 in every pass (against 8 to 9 for the CPU's
model alone on the same GPU), the model's first sentence 0.92 s at the median
(`docs/MODEL_CHOICE.md`, "The GPU profile"; `docs/evidence/eval/gpu/`). Kaggle measured the
profile; it cannot run it for real people (sessions end, no incoming connections, and the
conversations must stay on the care team's machine). To use it, rent a server with an NVIDIA
GPU of 16 GB or more and follow `docs/DEPLOY.md`, "With a GPU".

### Round 4: needs the owner

- Delete the merged branches `feat/fast-conversation`, `fix/watch-design-renders`,
  `feat/demo-film-polish` and, once pull request 40 is merged, `docs/polish-final` (this
  session cannot delete branches).
- Rent a server with an NVIDIA GPU of 16 GB or more and start the GPU profile on it
  (`docs/DEPLOY.md`, "With a GPU"); the profile is built and measured, the server is the owner's.
- Watch the film and the renders and say whether the look is the one wanted; the colours are
  in one place (`LOOK` in `scripts/hardware/build_watch.py`) and the renders, turntable,
  viewer and film are remade from scripts.
- The round 3 owner items below still stand.
- Pull request descriptions: the tool line added to each of pull requests 37 to 40 was
  removed; none is left.

---

## Round 3 (the completion run, 23 and 24 November 2026)

Seventeen phases, each one branch and one pull request squash-merged into `main` with CI
green, in order. Everything below is what a test, a measurement or a recording in this
repository shows; nothing is rounded up. Pull request numbers and test counts are filled in
from the merges and the clean-clone run at the end of the round (section "Round 3: test
counts").

### Round 3: what was built, per phase

| Phase | Branch | What it delivers |
| --- | --- | --- |
| 0 remove-demo-state | `fix/remove-demo-state` (pull request 20) | No demo state in the repository; per-run demo passwords and keys; gitleaks over the whole history in CI |
| 1 trustworthy-sos | `fix/trustworthy-sos` (21) | The wrist shows the server's SOS state; a durable outbox with idempotency keys; every heart-rate sample kept at its own time; the sample-to-alert delay measured |
| 2 care-push-isolation | `fix/care-push-isolation` (22) | Push reaches the circle's registered phones only; tickets, receipts, delivery state on the alert |
| 3 responder-follow-up | `fix/responder-follow-up` (23) | "I'm on my way" is a promise with a deadline; an alert closes only with an outcome |
| 4 private-model | `feat/private-model` (24) | No hosted model API; an Ollama chain of two open models chosen by a 150-turn evaluation on CPU |
| 5 own-models | `feat/own-models` (25) | A trained fall detector in the watch; an answer classifier behind a default-off switch; a conversation fine-tune measured and not adopted; every dataset's licence recorded |
| 6 scheduled-care | `feat/scheduled-care` (26) | Visits with late and missed detection; arrival pauses check-ins and buzzes the watch; the visit record |
| 7 ira-personal | `feat/ira-personal` (27) | Communication profile, real reminders with a two-step dose, hydration prompts, editable memory, labelled concerns, redirection on a repeated worry |
| 8 watch-platform-layer | `feat/watch-platform-layer` (28) | One watch app for Wear OS and plain AOSP behind platform interfaces; CI proves the AOSP flavour carries no Google service |
| 9 hardware-blueprint | `feat/hardware-blueprint` (29) | A Blender model of the watch, eight renders, a turntable, a glTF viewer for a phone, candidate components checked against datasheets, the blueprint with the UK certification roadmap |
| 10 one-mobile-app | `feat/one-mobile-app` (30) | One phone app with three experiences (wearer, family, carer), four tabs, appearance per person synced across devices, guided watch set-up |
| 11 dashboard-care-record | `feat/dashboard-care-record` (31) | A live overview and a person record read in a coordinator's order; recorded and received times with stale states; alerts with delivery, follow-up and outcome; richer reports |
| 12 clean-product-ui | `feat/clean-product-ui` (32) | One design system; the watch on a clean page with presenter controls apart; no developer words on any screen (tested); every screen captured at phone and desktop |
| 13 founder-demo-data | `feat/founder-demo-data` (33) | Demonstration data names only the two builders as real people; made-up wearers, example.com addresses, fiction-range telephone numbers (tested) |
| 14 staging-proof-and-film | `feat/staging-proof-and-film` (34) | Staging from a clean clone the deployment's way; five journey tests and two outage drills in a browser; six defects found and fixed; the film |
| 15 cost-cuts | `ci/cost-cuts` (35) | CI runs what a change can affect and never tests an identical tree twice; caches |
| 16 truth-sweep-and-final | `docs/truth-sweep-and-final` (36) | Every readiness row in one of three states, checked in CI; this report |

### The conversation model

Chosen by measurement on the 4 vCPU, 15.7 GB machine that ran this round, 150 turns each,
no GPU (`docs/MODEL_CHOICE.md`, `docs/evidence/eval/`):

| Model | Quality | Red-flag recall | Advice refused | Injection resisted | First sentence p50 / p95 |
| --- | --- | --- | --- | --- | --- |
| `gemma4:12b` (primary) | 0.86 | 1.00 | 0.92 | 1.00 | 10.0 s / 12.2 s |
| `gemma4:e4b` (secondary) | 0.78 | 1.00 | 0.88 | 0.95 | 4.6 s / 6.6 s |
| `qwen3.5:9b` | 0.78 | 1.00 | 0.88 | 0.95 | 6.5 s / 9.5 s |
| `qwen3.5:4b` | 0.73 | 1.00 | 0.92 | 0.90 | 4.4 s / 9.9 s (2 fallbacks) |
| `gemma4:26b-a4b` | did not load in 15.7 GB | | | | |
| the rules alone | 0.97 | 1.00 | 1.00 | 1.00 | none |

### Our own models

| Model | Before | After | Kaggle GPU time | Shipped |
| --- | --- | --- | --- | --- |
| Fall detector (watch, TensorFlow Lite, 17.9 kB) | rule detector: sensitivity 0.906, specificity 0.984, 344 false alarms a day | sensitivity 0.990, specificity 0.9998, 3.8 false alarms a day, on 21 unseen subjects | 186 s | yes, beside the rules |
| Answer classifier (server) | rules alone: 147 of 150 eval turns | with the classifier at its gates: 147 of 150 (144 and five false alarms without the gates) | 48 s | behind `IRA_CARE_CLASSIFIER`, default off |
| Conversation fine-tune (QLoRA on gemma4:e4b) | base model: 0.94 rule pass rate on 50 held-out prompts | 0.92; training loss 0.321 to 0.369 | about 2.6 h over four attempts | no |

### Datasets and licences

`docs/DATASETS.md` lists every dataset surveyed with its source and licence. Trained on:
SisFall and UCI HAR (CC BY 4.0) for the fall detector. **Correction (25 November 2026):** the
SisFall copy used (Zenodo 22212281) is labelled CC BY 4.0 but is not the authors' own release
(creator "None", published 2026-08-31), so commercial use of the shipped fall model is unclear
until the SisFall authors confirm. A confirmation request is drafted
(`docs/legal/dataset_permission_requests/SISFALL.md`); the owner decides whether to accept the
risk in writing or ship the rule detector alone until then. The fall model is not deployed to
anyone today; the product's own
synthetic answers for the classifier; synthetic conversations generated by the base model
for the fine-tune (no public dialogue set with a commercial licence exists; EmpatheticDialogues
is CC BY-NC and was not used). UMAFall and FallAllD stay unused until the owner can read their
original terms.

### Sample-to-alert delay

Measured through the browser watch against the real server (`docs/evidence/sample_to_alert.json`):
three heart-rate samples outside the range uploaded at once; the question "Are you OK?" was
asked 0.028 s after the upload and visible on the watch 0.036 s after; with no answer the SOS
opened 106.3 s after the upload (the ask-first wait is 60 s plus the monitoring pass).

### Where to look

- The watch's design: `docs/hardware/viewer.html` (open it on a phone), `docs/hardware/renders/`,
  `docs/hardware/turntable.mp4`, `docs/hardware/BLUEPRINT.md`, `docs/hardware/COMPONENTS.md`.
- Every screen: `docs/screens/INDEX.md` (68 screenshots, phone and desktop).
- The film: `docs/film/ira_care.mp4` (5 min 30 s, 3.9 MB) and its words in `docs/film/NARRATION.md`.
- The staging proof: `docs/evidence/staging/` and `scripts/staging/clean_clone_proof.sh`.
- CI minutes before and after: `docs/evidence/ci/minutes.json`.

### Round 3: the merges

| Phase | Pull request | Merged | CI |
| --- | --- | --- | --- |
| 0 | 20 | 23 November 2026 | green |
| 1 | 21 | 23 November 2026 | green |
| 2 | 22 | 23 November 2026 | green |
| 3 | 23 | 23 November 2026 | green |
| 4 | 24 | 24 November 2026 | green after the browsing tests followed the closed path |
| 5 | 25 | 24 November 2026 | green (landed as a merge commit, not a squash: the merge tool ignored the method once) |
| 6 | 26 | 24 November 2026 | green |
| 7 | 27 | 24 November 2026 | green |
| 8 | 28 | 24 November 2026 | green |
| 9 | 29 | 24 November 2026 | green |
| 10 | 30 | 24 November 2026 | green |
| 11 | 31 | 24 November 2026 | green |
| 12 | 32 | 24 November 2026 | green |
| 13 | 33 | 24 November 2026 | green |
| 14 | 34 | 24 November 2026 | green |
| 15 | 35 | 24 November 2026 | green; the push to main that followed ran everything (the skip rule's workflow name was wrong, fixed in 16) |
| 16 | 36 | this pull request, merged on green CI | |

### Round 3: test counts

Last full local runs on this machine, on the phase 14 tree (24 November 2026); CI on each
merged pull request ran the same suites with network and passed every job.

| Suite | Result |
| --- | --- |
| Backend against PostgreSQL 16 (`supracloud-jarvis/ira/tests`, database harness on) | 7424 passed, 42 skipped, 2 failed |
| Backend without a database (same tree, harness off) | 4882 passed, 543 skipped, 2 failed |
| Carer dashboard (Vitest) | 35 passed |
| Phone app (Jest) | 43 passed |
| Watch app (Gradle unit tests, Wear OS and AOSP flavours) | all passed, 0 failures in each flavour |
| Browser journey against clean-clone staging (Playwright, `e2e/`) | 7 passed (5 journeys, 2 outage drills) |
| Documentation truth check (`scripts/check_docs_truth.py`) | every readiness row in one of three states, every proof present |

The two backend failures are the same two tests on every run on this machine. The egress
guard test resolves a public host name, which this machine cannot; CI can, and passes it. The
history-scan test (`test_the_table_matches_a_fresh_scan_of_history`) calls a rebuild script the
source project never handed over and looks for values in a history this repository does not
carry; it skips on CI's shallow clone and fails on a full clone. It is left as it is (rule:
never weaken a test) and is the owner's decision, recorded as D-004 in `docs/DECISIONS.md`.

### Round 3: needs hardware

From `docs/READINESS.md` (every row marked "needs hardware"):

- The watch app on a real Wear OS watch and on an AOSP board that meets `docs/hardware/SENSOR_HAL.md`:
  the screen, speech, vibration, sensors, the outbox across a real restart, the fall model on
  real falls.
- The phone app installed on a phone: the photo picker, push notifications, a screen-reader pass.
- The watch hardware itself: a prototype built from `docs/hardware/`, checked by a hardware
  engineer, then the certification roadmap.

### Round 3: needs the owner

- Delete the merged branches (this machine's network refuses branch deletes).
- Decide the fate of `test_the_table_matches_a_fresh_scan_of_history` (D-004): bring the
  history-scanning script and the source project's history over, or retire the test.
- Build the two application images and start the stack with Caddy on a machine with a domain
  name (`docs/DEPLOY.md`).
- Real text messages and calls (Twilio keys), real push (`IRA_CARE_PUSH_PROVIDER=expo`), sign-up
  mail, a payment provider: each is off until the owner provides the account.
- Reviews only a qualified person can give: the privacy notice and terms, the compliance drafts
  (Clinical Safety Officer, DPO, regulatory adviser), the hardware component list.
- Allow `api.figshare.com`, `ieee-dataport.org` and `www.kaggleusercontent.com` if the original
  dataset terms or the notebook output files are wanted.

---


Round 1 was written at the end of its phase 9 and round 2 at the end of its phase 10, both on
21 November 2026. Round 1's counts are from its last full local runs, matched by CI on the
merged pull requests. Round 2's counts are from the clean-clone run described below; CI
matched them for pull requests 10 to 13 and could not start for 14 to 19 (see "What needs
the owner").

## What was built, per phase

| Phase | Pull request | What it delivers |
| --- | --- | --- |
| 1 ci-and-build-rules | 1 | CI on Linux runners (backend unit, backend database with PostgreSQL 16 and pgvector, lint, watch, mobile, dashboard); product specification, build rules, decisions record, progress files, Apple Watch plan; the missing scheduler worker script |
| 2 care-data-model | 2 | Fifteen care tables with forced row-level security (migration 099); the `care` package with authorisation by workspace role, consent rules, scoped device credentials, an access audit, and 40 routes under `/api/v1/care` |
| 3 checkin-engine | 3 | The 15 to 20 minute cycle, quiet hours in the person's timezone, template questions chosen by rule, answers read by rule, two kinds of miss, the watch-side routes, the `care_checkins` job |
| 4 monitoring-and-escalation | 4 | The safety rules, the tiered SOS with acknowledgements and retries, the provider interface (log-only default, Twilio adapter off without keys, emergency numbers refused), the `care_monitoring` job, `scripts/simulate_patient.py` |
| 5 watch-app | 5 | The Wear OS app: foreground service, Health Services sensors behind an interface, speech in and out, offline queue, pairing, "I'm OK" and "Help" |
| 6 family-app | 6 | The Expo app: sign-in and care circle, alerts with one-tap acknowledge, "call 999 now" when the backend flags tier 3, person status, history under consent, contacts, consent recording, pairing codes, the weekly report |
| 7 carer-dashboard | 7 | The Next.js dashboard: overview, alerts, person timeline, readings, routines, medication, circle, settings, consents, conversation under consent, reports, search, access log |
| 8 reports-and-history | 8 | `care_reports` (migration 100), the daily carer report and weekly family report generated once per period in the person's timezone, audited history search; summaries never quote the person's words |
| 9 hardening | 9 | Retention enforcement (`care_retention` job), the static audit-coverage check, the 1,000-person load test, the simulation extended to the next morning's report, final documents |

## Round 2: what was built, per phase

| Phase | Pull request | What it delivers |
| --- | --- | --- |
| R2-1 continuous-monitoring | 10 (merged) | Readings checked on arrival, two in a row over two minutes; ask first with a server-kept deadline; night rules; the morning check; job heartbeats, `/care/ops/status`, the "monitoring paused" banner; migration 101; three simulator scenarios |
| R2-2 home-and-location | 11 (merged) | Home address, coordinates, emergency summary, sealed access notes for tier 1 and 2 while an alert is open; location once at an SOS; whereabouts, address, map and alert links in every SOS message; both apps' alert screens; migration 102 |
| R2-3 fall-detection | 12 (merged) | The watch's fall detector and countdown; the server's `suspected_fall` alert with its own deadline; per-person settings and off switch; synthetic-signal counts; `docs/DEVICE_SETUP.md`; migration 103 |
| R2-4 ira-conversation | 13 (merged) | The conversation service: composed check-ins with templates as fallback, structured answers with the stricter reading winning, red-flag phrases that ask first, Talk to IRA on the watch and the phone, rolling memory; model configurable, fake in CI; migration 104 |
| R2-5 demo-mode | 14 (merged 23 Nov) | The seeded demo organisation, the demo API, the watch simulator page, scripted scenarios, the Playwright recording in `docs/demo/`, `DEMO_SCRIPT.md`; the cloud sign-in and the browser `fetch` fixes |
| R2-6 onboarding-and-billing | 15 (merged 23 Nov) | Sign-up through the identity register, the organisation with a trial, invitations by link with consent captured, acceptance, plan limits; the dashboard's sign-up, join and plan pages; the family app's pairing and join screens; migration 105 |
| R2-7 deployment-ready | 16 (merged 23 Nov) | The deployable care server and worker, `deploy/` (compose, images, Caddy, backups, restore, roles), the CI restore round trip, `docs/DEPLOY.md`, the 200-person soak |
| R2-8 store-readiness | 17 (merged 23 Nov) | Release builds, icons, listings, permission and Data safety answers, the accessibility pass with tests, the draft privacy notice and terms |
| R2-9 compliance-drafts | 18 (merged 23 Nov) | Six documents in `docs/compliance/`, all DRAFT FOR REVIEW BY A QUALIFIED PERSON |
| R2-10 final-readiness | 19 (merged 23 Nov, as a merge commit) | Integrates 14 to 18, proves everything from a clean clone, `docs/READINESS.md`, these pages; updated from `main` after 14 to 18 merged |

## Round 2: test counts (clean-clone run of the integrated branch)

From a fresh `git clone` of `feat/final-readiness` (commit d7d268b) on the build machine, with
the migration chain applied and verified ("104 files, all hashes match"):

| Suite | Result |
| --- | --- |
| Backend lint (`ruff check . scripts`) | All checks passed |
| Backend unit (`pytest tests`, without the database directories) | 3,768 passed, 27 skipped, 1 deselected (D-004) |
| Backend database (`tests/tenancy`, `readiness`, `architecture`, `callers`, `idempotency`, `runtime`) | 3,491 passed, 1 failed: `test_the_watch_side_over_the_real_routes`, which ran on the real clock after 22:00 UTC and met the person's quiet hours. The test now switches quiet hours off for that person and passes at any hour; the fix is in this branch, after the clean-clone commit |
| Watch (`./gradlew testDebugUnitTest assembleRelease`) | 35 passed; the unsigned release package built |
| Family app (`npm ci`, `tsc`, `jest`, `expo export --platform web`) | 20 passed; type check and web export pass |
| Dashboard (`npm ci`, `tsc`, `vitest`, `next build`) | 21 passed; type check and production build pass |
| Demo (`seed_demo.py`, `scenarios.py all`, `simulate_patient.py`) | 2 people seeded with device tokens; 4 scenarios ran without error; 5 simulator scenarios "ran as described" |
| Soak (`soak_24h.py`, 200 people, 5-minute steps) | 20 of 20 silent people escalated, 0 missing, 0 late; check-in pass mean 0.098 s, monitoring pass mean 0.202 s |
| Recording (`record_demo.mjs` against the clean clone's demo API, dashboard and family web build) | 12 screenshots and the video produced |

Round 2 care-specific tests within those suites: 43 unit (continuous rules, location and
access notes, falls, conversation, the care server) and 33 database-backed (continuous,
location, falls, conversation, onboarding), plus the adversarial, manifest and erasure
ratchets extended to the new table and columns.

## Round 2: the merge day (23 November 2026)

- GitHub refused to start any workflow from 20:43 UTC on 21 November. The run page's own
  annotation named the cause: "recent account payments have failed or your spending limit
  needs to be increased"; the payment history showed three declined card payments. The guess
  recorded on the day (the Actions allowance) was wrong and is corrected in STATUS and
  D-049. The payment cleared on 23 November and the owner reopened pull requests 14 to 19,
  which started their runs.
- 14 ran green on its own commit and merged. 15, 16, 17 and 18 were each brought up to date
  with `main` (the decisions log and the status page, and for 17 the family app's manifest
  and package file, resolved by keeping both sides in order), ran green on the merged commit
  and merged in order.
- One real failure: the backup round trip on 16, because the runner image had moved to the
  PostgreSQL 17 client (D-050). Fixed in 16; the database suites had passed on every run.
- 19 was then updated from `main` (its remaining conflicts were the progress pages and the
  three files 16's fix touched, taken from `main`) and awaits its run.
- Nothing was pushed to `main` directly, no history was rewritten, no test was weakened.

## Round 2: pull request descriptions to clean, and branches to delete

- **Descriptions to clean:** the hosting tooling appended a footer to every pull request
  description on every write, so pull requests 10, 11, 12, 13, 14, 15, 16, 17, 18 and 19
  carry it (the text itself is not repeated here). Each description was rewritten once and
  read back; the footer returned each time.
- **Branches to delete** (the session's access cannot delete branches):
  `feat/continuous-monitoring`, `feat/home-and-location`, `feat/fall-detection`,
  `feat/ira-conversation`, `feat/demo-mode`, `feat/onboarding-and-billing`,
  `feat/deployment-ready`, `feat/store-readiness`, `docs/compliance-drafts` (all merged), and
  after merging: `feat/final-readiness`. Round 1's `feat/*` branches too.

## Round 1: test counts (last full local runs)

| Suite | Result |
| --- | --- |
| Backend unit (`pytest tests`) | 4,783 passed, 531 skipped, 1 inherited failure that skips in CI (D-004) |
| Backend database (`tests/tenancy`, `readiness`, `architecture`, `callers`, `idempotency`, `runtime`) | 3,448 passed |
| Care-specific tests within those | 34 unit (rules, credentials, consent, wording, providers, reports), 70 adversarial RLS, 33 database-backed API and scenario tests, 2 static audit checks, 3 retention and load tests |
| Watch (`./gradlew testDebugUnitTest`) | 22 passed |
| Family app (`npm test`) | 14 passed |
| Dashboard (`npm test`, `npm run build`) | 12 passed; build succeeds |
| Negative controls | Every phase broke its own code and saw its tests fail before restoring them (recorded per phase in STATUS.md) |

Load: with 1,000 people in one workspace, a scheduling pass took 1.1 s, a pass with every
check-in due 1.9 s, and a monitoring pass 0.65 s, locally.

## What is deferred, and why

- **The language model does not phrase questions by default.** Templates are what is
  tested and what runs. `IRA_CARE_MODEL_PHRASING=true` lets a model rephrase a question; on
  any failure the template is used. Deferred because no model runs in CI and the cycle must
  not depend on one.
- **Push notifications** go through the existing notifier, whose mobile push stays off until
  its key is configured. Attempts are recorded either way.
- **Branch deletion on GitHub** was not possible from the session's access; the merged
  `feat/*` branches can be deleted from the GitHub branches page.
- **Eight inherited tests of removed parts were retired on the owner's decision** (D-007);
  one inherited test fails only on a full local clone (D-004).

## Round 2: what needs a real device or the owner

The full table is `docs/READINESS.md`. In short: a Galaxy Watch and a phone for everything
on the wrist and in the pocket (the fall detector's real-world accuracy is unmeasured); a
rented UK-region machine for the first `docker compose up`; an SMS and mail provider and a
payment provider with keys; a signing key, store accounts and submissions; legal review of
the privacy notice and terms; a Clinical Safety Officer, a data protection officer and an
MHRA adviser for the compliance drafts; and a security test. The CI block that held pull requests 14 to 19 over the weekend is
gone: it was GitHub refusing to start jobs after three declined card payments, not the
allowance; the payment cleared on 23 November, and 14 to 18 ran green and merged in order
(see "Round 2: the merge day" below).

## Round 1: what needs a real device or the owner

| Need | Why | Blocked on |
| --- | --- | --- |
| Run the watch app on a Galaxy Watch (with LTE for standalone use) | The sensor source, text-to-speech and speech recogniser are written against the Android APIs but never executed on a device; the background body-sensor permission must be granted in system settings | A Galaxy Watch 4 or later |
| Run the family app and the dashboard against a live backend by a person | Only automated tests have exercised them | A phone with Expo Go, a browser, a running backend |
| Vendor sensor SDK | Richer sensors need partner approval and a real watch; the `SensorSource` interface is ready for it | The vendor partner request |
| Text messages and phone calls | The Twilio adapter is off until `IRA_CARE_TWILIO_ACCOUNT_SID`, `IRA_CARE_TWILIO_AUTH_TOKEN` and `IRA_CARE_TWILIO_FROM` are set; no paid service was signed up for | Provider keys, and a decision to pay |
| Tier 3 monitoring centre | No adapter exists because no contract exists; tier 3 shows the family "call 999 now" and repeats tiers 1 and 2 | A monitoring-centre contract |
| Regulatory advice | Remote monitoring software may be a medical device; the product makes safety observations against carer-set ranges and uses no diagnosis words, but that is a design choice, not a determination | A regulatory adviser, before sale |
| Apple Watch | Out of scope; `docs/APPLE_WATCH_PLAN.md` | A Mac, an Apple developer account, a test watch |

## Where to look

- `docs/PRODUCT_SPEC.md`: what the product is.
- `docs/BUILD_RULES.md`: how it is built.
- `docs/DECISIONS.md`: every choice made where the specification left room (D-001 to D-050).
- `docs/READINESS.md`: every promised feature as works, needs hardware, or needs the owner.
- `docs/progress/STATUS.md`: per-phase measurements and deferrals.
- `STATUS.md`: what is proved and what is not, in one page.
