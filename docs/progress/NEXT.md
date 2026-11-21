# IRA CARE: next step

**Open branches:** `feat/ci-and-build-rules` (phase 1, pull request 1), `feat/care-data-model` (phase 2, pull request 2, stacked on 1), `feat/checkin-engine` (phase 3, stacked on 2).

**Exact next step:** once CI is green on pull request 1, squash-merge it, merge
pull request 2, then the phase 3 pull request, each after rebasing onto `main` and seeing
green. Then start phase 4 (`feat/monitoring-and-escalation`): the "is this normal?" rules
over readings and misses, the tiered SOS with acknowledgements and retries, the SMS and
call provider interface (log-only default), and `scripts/simulate_patient.py` driving a
"person does not answer and stops moving" scenario end to end in a test.

To continue in a new session: read `docs/progress/STATUS.md`, this file, `docs/BUILD_RULES.md`
and `docs/DECISIONS.md`, then carry on with the same rules.
