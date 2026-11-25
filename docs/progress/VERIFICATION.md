# Integrated verification on `main` (register K)

Run on 25 November 2026 on `main` at `eec19a0`, after PRs #63 to #80 were merged. Every number
below comes from a run whose output was read; nothing is estimated. The previous run (after
#50 to #61, at `eed43f1`) is in this file's history.

## Passed, failed and skipped

| Check | Where | Result |
|---|---|---|
| Backend lint (`ruff check .`) | this container | clean |
| Migration chain applied as `ira_migrator` to an empty database; applied migrations unchanged | this container, PostgreSQL 16 | 115 migrations applied (to `116_care_plans`); integrity check: all hashes match |
| Backend database suites (`tests/tenancy tests/readiness tests/architecture tests/callers tests/idempotency tests/runtime`, as CI runs them) on that fresh database | this container | 3814 passed, 10 skipped, **0 failed** (14 min). An earlier run on the session's shared, long-used test database had 1 failure, `test_no_suite_left_a_tenant_holding_memberships`: residue left by parallel runs on that database, which a fresh database does not reproduce |
| Backend unit suite (`pytest`, everything outside `tests/tenancy`) | this container | 5010 passed, 28 skipped, **2 failed**, both environmental and both passing in CI: `test_public_url_allowed_by_egress_guard` (this container has no outbound DNS) and `test_the_table_matches_a_fresh_scan_of_history` in `test_published_credentials.py` (the checkout is shallow, the scan needs the full history) |
| Readiness and completion framework (`tests/readiness`) | this container | 171 passed, 1 skipped |
| Documents tell the truth (`check_docs_truth.py`); requirements valid (`completion_summary.py`) | this container | passed; requirements 1.4.0 valid |
| The 22-step care journey (`test_care_journey_db.py`, D-079) | database suite above | passed |
| Tenant, workspace and person isolation; medication safety; escalation; signed provider callbacks; concurrency and retries; outage recovery | database suite above (`test_3c06_*`, `test_care_person_access_db`, `test_care_safety_answers_db`, `test_care_escalation_db`, `test_care_delivery_db`, `test_care_outages_db`) | passed |
| Dashboard: `tsc`, vitest, `next build` | this container | typecheck clean; 91 tests passed; production build succeeded |
| Phone app: `tsc`, jest | this container | typecheck clean; 75 tests passed |
| Watch app (Wear OS and AOSP builds, JVM tests), family app, dashboard, backend unit and database, lint, container images (API, dashboard, backup), readiness, secret scan | CI on #74's head `f2ff33c`, the last change with code before `eec19a0` (#80 changed `requirements.json` only) | all 10 jobs passed |
| Container stack: build, health, care flow, outage drills, backup and restore, load | #78, `docs/evidence/staging/CONTAINERS.md` | passed (load figures are a single-machine baseline) |
| Python dependencies (`pip-audit` over `requirements.lock`) | this container | no known vulnerabilities |
| Dashboard production dependencies (`npm audit --omit=dev`) | this container | 0 vulnerabilities |
| Phone app production dependencies (`npm audit --omit=dev`) | this container | 20 (1 critical, 3 high, 16 moderate), all in Expo's CLI and Metro build tooling, none in the app bundle; fixing them needs an Expo SDK major upgrade (`docs/security/REVIEW.md`, residual risk) |
| Static analysis (bandit over the care backend and its shared modules) | this container | 0 high, 31 medium, 58 low; the medium "SQL built from strings" findings in the care code were reviewed by hand in #72: column names from fixed allow-lists, values bound |
| Secret scan (gitleaks) | CI | passed |
| Development-tool branding, generated footers, session links | `git grep` over the tree; the bodies of PRs #63 to #80; commit messages and authors since #66 | none; the only match is the test that guards against it |

## Not passed here, and why

| Check | Why | What it needs |
|---|---|---|
| `test_public_url_allowed_by_egress_guard` | this container has no outbound DNS (`example.com` does not resolve); passes in CI | nothing: environmental |
| `test_published_credentials.py::test_the_table_matches_a_fresh_scan_of_history` | the checkout is shallow; the scan needs the full git history; passes in CI | nothing: environmental |
| IRA CARE OS image (Cuttlefish build and boot) | `os/scripts/build_cuttlefish.sh` refused, as designed: 42 GB free disk (needs 400 GB), 16 GB RAM (needs 64 GB), 4 cores (IRA's floor is 8); there is no `/dev/kvm` to boot it either. Nothing was downloaded or built | an AOSP build host |
| Native Android builds of the watch and phone apps on this container | started with the owner's approval of the Android SDK licence; recorded separately in `docs/evidence/builds/ANDROID.md` when complete | — |
| iOS build | no macOS and no Apple signing | macOS with Apple signing |
| Device, emulator and screen-reader runs | no physical watch or phone, no emulator image | physical devices |
| Live SMS and call delivery | only the recording and log-only providers are used; no credentials | provider credentials and a decision to pay |
| Capacity on a representative deployment; 24-hour soak | one shared 4-CPU container; the load test in #78 is a baseline only | a representative server |

## Licences

Unchanged since the previous run except for the two datasets added this round, both verified at
their source on 25 November 2026: BITS-2 (Zenodo 10013090, CC BY 4.0, the authors' own record)
for the fall model v2 candidate, and WISDM (UCI 507, CC BY 4.0) for the activity model.
`docs/DATASETS.md` is the register; `test_dataset_register.py` checks that nothing shipped is
trained on a dataset whose commercial use is "no" or unconfirmed without a confirmation request.
