# IRA CARE: next step

**Open branch:** `feat/ci-and-build-rules` (phase 1).

**Exact next step:** once CI is green on pull request 1, squash-merge it, then continue with
phase 2 (`feat/care-data-model`): the care tables with
row-level security, the `ira/care` package, routes under `/api/v1/care`, the tenancy
classification manifest, the migration hash record and the erasure rules, with RLS tests.

To continue in a new session: read `docs/progress/STATUS.md`, this file, `docs/BUILD_RULES.md`
and `docs/DECISIONS.md`, then carry on with the same rules.
