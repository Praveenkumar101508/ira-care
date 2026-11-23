# IRA CARE: next step

**Round 3 (the completion run) is in progress.** Phases and their state are in
`STATUS.md` (table "Round 3"). Phases 0 to 3 are merged (pull requests 20 to 23). Phase 4
waits on its CPU benchmark to write `docs/MODEL_CHOICE.md`; phase 5 waits on two Kaggle
kernels (fall model, QLoRA); phase 6 is built and tested on `feat/scheduled-care`. Next:
finish and merge 4, 5, 6 in order (each rebased on the new `main`), then R3-7 `feat/ira-personal`.

Owner actions found so far in round 3:

1. Delete the merged branches `fix/remove-demo-state`, `fix/trustworthy-sos`,
   `fix/care-push-isolation`, `fix/responder-follow-up` (the session's network may not).
2. If the SisFall mirror or IEEE DataPort sets are wanted locally, allow `api.figshare.com`
   and `ieee-dataport.org`; to download Kaggle kernel output files directly, allow
   `www.kaggleusercontent.com` (the session read kernel logs through the API instead).

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
