# IRA CARE: next step

**Open branches:** `feat/ci-and-build-rules` (phase 1, pull request 1), `feat/care-data-model` (phase 2, pull request 2, stacked on 1), `feat/checkin-engine` (phase 3, pull request 3, stacked on 2), `feat/monitoring-and-escalation` (phase 4, pull request 4, stacked on 3), `feat/watch-app` (phase 5, pull request 5, stacked on 4), `feat/family-app` (phase 6, stacked on 5).

**Exact next step:** once CI is green on pull request 1, squash-merge it, merge
pull request 2, then the phase 3 pull request, each after rebasing onto `main` and seeing
green. Then the phase 4 pull request. Then the phase 5 pull request. Then the phase 6 pull request. Then start phase 7 (`feat/carer-dashboard`): the Next.js
dashboard in `dashboard/` (many people at once, timelines of vitals, check-ins, routines,
medication and alerts, conversation history under consent, roles), with a type check, tests
and a build, and enable the dashboard CI job.

To continue in a new session: read `docs/progress/STATUS.md`, this file, `docs/BUILD_RULES.md`
and `docs/DECISIONS.md`, then carry on with the same rules.
