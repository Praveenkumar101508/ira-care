# IRA CARE: next step

**Round 10 is on pull request 132** (`praveen/vigilant-tesla-bhe022`): licences, the OmniVoice
exclusion (D-109), the fall confirmation stage on recorded data (D-110, it stays off), the
conversation fixes and a new hold-out (D-111), the deployment administrator journey in a real
browser with two fixes (D-112), and the authenticated iOS journey (its first run is on the PR).

**Owner restriction: the owner's personal Mac is never used** for IRA CARE testing, hosting,
evaluation, datasets, Docker, AOSP or the soak (too little storage). Everything below runs on
GitHub's free runners, the build machine, or a remote server.

## Mandatory: done (carried from pull request 132)

**Closed on 28 November 2026:** the iOS authenticated journey passed **16 of 16** on run 46106258647
(pull request 133, every check green). Record: `docs/evidence/builds/IOS_SIMULATOR.md`.

The history of the fix:

Pull request 132 was merged on 28 November 2026 with **one check failing**: the iOS simulator job's
authenticated care journey (`.github/workflows/ios-simulator.yml`, `scripts/simulator/ios_care_journey.py`).
`main` shows that red check, and every pull request that touches the phone or watch apps will fail
the same job, until this is fixed. It must not be released, marked verified or reported as passing
before then.

| Run | Where it stopped | Fix |
|---|---|---|
| 46083831505 | 12 of 16 steps; the sign-in was lost at the restart | signed to run locally (in `main`) |
| 46090062926 | sign-in: iOS autocorrect changed the username | no autocorrect on account fields (pull request 133) |
| 46093555159 | sign-in: iOS's "Save Password?" sheet covered the app | answer it "Not Now" (pull request 133) |
| 46100618011 | 10 of 14: sign-in, record, plan, Help acknowledged, appearance across a restart, offline notice all passed; then the care server's restart signed the app out (a new random secret at each start of the test server), and iOS's "Save Password?" sheet covered the wearer's and carer's sign-ins | one secret for the whole journey; the sheet answered before and after every sign-in (on pull request 133) |

Done means: the native-checks run on `main` (or on the follow-up pull request) green, the iOS journey
16 of 16, and D2.h's journey recorded in `docs/evidence/builds/IOS_SIMULATOR.md`.

Each outside need, with exactly what to ask for and what it unblocks: `docs/progress/RESOURCE_REQUESTS.md`.

Owner actions, in order of use:

1. **Pull request 132 is merged** (28 November); its iOS journey now passes on pull request 133 (see above). Merge 133.
   The follow-up pull request carries the two fixes and this round's verification work.
2. **After the merge, three manual workflows** (Actions, the workflow, "Run workflow" on `main`;
   each needs the file on the default branch first):
   - `deployment guide (manual)`: follows `docs/DEPLOY.md` sections 2 to 7 on a free runner (G4.a);
   - `wall-clock soak (manual)`: 5.5 hours of the server under load (G2.e's interim);
   - `dataset provenance (manual)`: fetches UMAFall version 8 and checks it against both official
     records (F1.g).
   (C-7.b moved to verified on phone emulator run 46100618011.)
3. **A Linux server for the 24-hour soak (G2.e)**, from a sponsor or a university (Essex research
   computing has been asked), or registered as a GitHub self-hosted runner (jobs up to 5 days).
   Meanwhile the `wall-clock soak (manual)` workflow runs 5.5 hours on a free runner: Actions,
   "wall-clock soak (manual)", "Run workflow" (`docs/evidence/soak/HOW_TO_RUN_24H.md`).
4. **The AOSP build host** (E2.h onwards): about 64 GB of memory, 400 GB of disk, x86-64 and KVM
   (`docs/os/BUILD.md`).
5. **Common Voice:** decide whether to accept the Mozilla Data Collective's terms. Common Voice 27.0
   English is CC0; with an account, only the older-speaker subset is fetched, on a remote machine
   (`docs/ai/speech/OLDER_ADULT_EVAL_PLAN.md`).
6. **Dataset replies:** KFall, SisFall, FallAllD and WEDA-FALL (`docs/legal/`). SisFall stays
   excluded until its authors reply; UMAFall version 8 is CC BY 4.0 on two official records.
7. **Devices, keys, reviewers and accounts:** unchanged from `READINESS.md`.

To continue in a new session: read `STATUS.md`, this file, `docs/BUILD_RULES.md` and
`docs/DECISIONS.md`, then carry on with the same rules.

---

## Round 4 record (kept for history)


**Round 4 (the polish) is complete** once pull request 40 merges: pull requests 37 to 40,
each squash-merged on green CI from the latest `main`. What was built and measured, and what
is left for the owner, is in `FINAL_REPORT.md` (section "Round 4"). The four phases:

1. `feat/fast-conversation` (R4-1): IRA's first words within 1.5 s and first sentence within
   3 s on 4 vCPU; the 162-turn evaluation re-run for every variant; `docs/MODEL_CHOICE.md`
   ("Round 4"), D-069 to D-071.
2. `fix/watch-design-renders` (R4-2): one product look (graphite case, coral Help button, the
   app's colours) across the Blender model, the eight renders, the turntable, the viewer and
   the browser watch; the exploded view in the right order with legible labels.
3. `feat/demo-film-polish` (R4-3): the film again at 1080p, 4 to 6 minutes, under 60 MB.
4. `docs/polish-final` (R4-4): the documents in line with the merged code; a dated round 4
   section in `FINAL_REPORT.md`; the truth check.

The state of each phase is in `STATUS.md` (table "Round 4"). To continue in a new session:
read `STATUS.md`, this file, `docs/BUILD_RULES.md` and `docs/DECISIONS.md`, then carry on with
the same rules.

---

## Round 3 record (kept for history)

**Round 3 (the completion run) is complete.** Phases 0 to 15 are merged (pull requests 20
to 35) and phase 16 is pull request 36, which merges on green CI. The state of every phase
is in `STATUS.md` (table "Round 3"); what was built, measured and left for the owner is in
`FINAL_REPORT.md` (section "Round 3").

Owner actions from round 3, in order of use:

1. Delete the merged branches `fix/remove-demo-state`, `fix/trustworthy-sos`,
   `fix/care-push-isolation`, `fix/responder-follow-up`, `feat/private-model`, `feat/own-models`,
   `feat/scheduled-care`, `feat/ira-personal`, `feat/watch-platform-layer`, `feat/hardware-blueprint`,
   `feat/one-mobile-app`, `feat/dashboard-care-record`, `feat/clean-product-ui`,
   `feat/founder-demo-data`, `feat/staging-proof-and-film`, `ci/cost-cuts` and, once pull
   request 36 is merged, `docs/truth-sweep-and-final` (this machine's network refuses branch
   deletes).
2. Read the Actions run for the push that merges pull request 36: it is the first run that
   can show the heavy jobs skipping on `main` for an identical tree (phase 15's rule, fixed in
   phase 16; `docs/evidence/ci/minutes.json`).
3. Build the two application images and start the stack with Caddy on a machine with a
   domain name (`docs/DEPLOY.md`); this machine could not build images, so staging ran
   everything the images run from the same files instead.
4. A hardware engineer verifies the rows of `docs/hardware/COMPONENTS.md` marked not verified
   (those manufacturers' sites refuse automated readers) and a regulatory adviser reviews
   `docs/hardware/BLUEPRINT.md` section 8 before any submission.
5. Decide the fate of `test_the_table_matches_a_fresh_scan_of_history` (D-004): bring the
   history-scanning script and the source project's history over, or retire the test.
6. Allow `api.figshare.com`, `ieee-dataport.org` and `www.kaggleusercontent.com` on the
   environment only if the original terms of UMAFall and FallAllD, the SisFall mirror or the
   Kaggle notebook output files are wanted; nothing shipped depends on them.
7. Then the "needs the owner" rows of `docs/READINESS.md`: devices, keys, reviews.

To continue in a new session: read `docs/progress/STATUS.md`, this file, `docs/BUILD_RULES.md`
and `docs/DECISIONS.md`, then carry on with the same rules.

---

## Round 2 record (kept for history)

**Round 2 is built.** Phases 1 to 4 are merged to `main` (pull requests 10 to 13). Phases 5
to 9 are open pull requests 14 to 18, each from `main`; `feat/final-readiness` (pull request
19) integrates all of them and is proved from a clean clone (`docs/READINESS.md`).

**State on 23 November 2026:** CI started again once the account's payment cleared (the
`startup_failure` runs from 21 November were GitHub refusing to start jobs after three
declined card payments, not the workflow and not the minutes). Pull requests 14 to 18 were
re-run, brought up to date with `main` one at a time, and squash-merged in order. One real
finding on the way: the runner image now ships the PostgreSQL 17 client, whose dump a 16
server refuses on restore; CI pins the 16 client and `deploy/backup.sh` refuses a dump tool
newer than the server (merged with pull request 16).

**Exact next step for the owner:**

1. Pull request 19 (this branch, updated from `main`) merges on green CI.
2. Delete the merged branches (the session could not); the list is in `FINAL_REPORT.md`.
3. Set the card as the default payment method on GitHub so the December charge does not
   stop CI again.
4. Then the "Needs the owner" list in `docs/READINESS.md`: devices, keys, reviews.

To continue in a new session: read `docs/progress/STATUS.md`, this file, `docs/BUILD_RULES.md`
and `docs/DECISIONS.md`, then carry on with the same rules.
