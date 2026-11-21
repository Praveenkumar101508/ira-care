# IRA CARE: next step

**Open branches:** `feat/ci-and-build-rules` (phase 1, pull request 1) and `feat/care-data-model` (phase 2, stacked on phase 1).

**Exact next step:** once CI is green on pull request 1, squash-merge it, rebase
`feat/care-data-model` onto `main`, open its pull request, and merge when green. Then start
phase 3 (`feat/checkin-engine`): the 15 to 20 minute check-in cycle, quiet hours,
elderly-friendly wording using the existing chat engine and memory, answers stored and
summarised into `care_checkins` and `care_transcripts`.

To continue in a new session: read `docs/progress/STATUS.md`, this file, `docs/BUILD_RULES.md`
and `docs/DECISIONS.md`, then carry on with the same rules.
