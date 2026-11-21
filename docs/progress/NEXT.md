# IRA CARE: next step

**Round 2 is built.** Phases 1 to 4 are merged to `main` (pull requests 10 to 13). Phases 5
to 9 are open pull requests 14 to 18, each from `main`; `feat/final-readiness` (pull request
19) integrates all of them and is proved from a clean clone (`docs/READINESS.md`).

**Why they are not merged:** GitHub Actions stopped starting workflows at 20:43 UTC on
21 November 2026 (`startup_failure` on an unchanged, valid workflow; no GitHub incident),
which matches the private repository's monthly Actions allowance running out. Merging
requires green CI, so the owner's first step is to restore it.

**Exact next step for the owner:**

1. Check the account's Actions minutes or spending limit, or make the repository public,
   then re-run the checks on pull requests 14 to 19.
2. Either squash-merge 14 to 18 in order and then 19 (resolving the progress pages the way
   19 already has), or squash-merge 19 alone, which contains all of them.
3. Delete the merged branches (the session could not).
4. Then the "Needs the owner" list in `docs/READINESS.md`: devices, keys, reviews.

To continue in a new session: read `docs/progress/STATUS.md`, this file, `docs/BUILD_RULES.md`
and `docs/DECISIONS.md`, then carry on with the same rules.
