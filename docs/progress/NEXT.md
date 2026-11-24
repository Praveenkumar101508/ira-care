# IRA CARE: next step

**Round 4 (the polish) is in progress.** Four phases, each one branch and one pull request
squash-merged on green CI, from the latest `main`:

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
