# IRA CARE: next step

**Open branches:** `feat/ci-and-build-rules` (phase 1, pull request 1), `feat/care-data-model` (phase 2, pull request 2, stacked on 1), `feat/checkin-engine` (phase 3, pull request 3, stacked on 2), `feat/monitoring-and-escalation` (phase 4, stacked on 3).

**Exact next step:** once CI is green on pull request 1, squash-merge it, merge
pull request 2, then the phase 3 pull request, each after rebasing onto `main` and seeing
green. Then the phase 4 pull request. Then start phase 5 (`feat/watch-app`): the Wear OS app in
`watch/` (Kotlin, Compose for Wear OS, Health Services behind an interface, foreground
service, text-to-speech and speech recognition, offline queue, pairing), with unit tests,
and enable the watch CI job.

To continue in a new session: read `docs/progress/STATUS.md`, this file, `docs/BUILD_RULES.md`
and `docs/DECISIONS.md`, then carry on with the same rules.
