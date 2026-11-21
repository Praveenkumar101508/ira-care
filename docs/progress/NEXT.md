# IRA CARE: next step

**Open branches:** `feat/ci-and-build-rules` (phase 1, pull request 1), `feat/care-data-model` (phase 2, pull request 2, stacked on 1), `feat/checkin-engine` (phase 3, pull request 3, stacked on 2), `feat/monitoring-and-escalation` (phase 4, pull request 4, stacked on 3), `feat/watch-app` (phase 5, pull request 5, stacked on 4), `feat/family-app` (phase 6, pull request 6, stacked on 5), `feat/carer-dashboard` (phase 7, pull request 7, stacked on 6), `feat/reports-and-history` (phase 8, stacked on 7).

**Exact next step:** once CI is green on pull request 1, squash-merge it, merge
pull request 2, then the phase 3 pull request, each after rebasing onto `main` and seeing
green. Then the phase 4 pull request. Then the phase 5 pull request. Then the phase 6 pull request. Then the phase 7 pull request. Then the phase 8 pull request. Then start phase 9 (`feat/hardening`): consent and
retention enforcement (a retention purge for the care tables honouring
`care_consents.retention_months`), audit coverage checks, a load test of the check-in cycle
for 1,000 people, the full end-to-end simulation in CI, and the final README.md, STATUS.md
and `docs/progress/FINAL_REPORT.md` that say honestly what is built and tested.

To continue in a new session: read `docs/progress/STATUS.md`, this file, `docs/BUILD_RULES.md`
and `docs/DECISIONS.md`, then carry on with the same rules.
