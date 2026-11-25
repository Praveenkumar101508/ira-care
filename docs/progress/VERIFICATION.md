# Integrated verification on `main` (register K)

Run on 25 November 2026 on `main` at `04451d7` (PRs #63 to #88 merged), against a database created
empty for the run and migrated as `ira_migrator`. Every number below comes from a run whose output
was read; nothing is estimated. Earlier runs (after #61 at `eed43f1`, after #80 at `eec19a0`) are in
this file's history. PRs merged after `04451d7`, #89 to #94, each passed every CI job before merging; their CI
runs are their verification. #94 adds a licence check of shipped dependencies to CI (below).

## Passed, failed and skipped

| Check | Where | Passed | Skipped | Failed |
|---|---|---|---|---|
| Migration chain to an empty database as `ira_migrator`; integrity of applied migrations | this container, PostgreSQL 16 | 117 migrations applied, to `120_os_releases`; all hashes match | | 0 |
| Database suites as CI runs them (`tests/tenancy tests/readiness tests/architecture tests/callers tests/idempotency tests/runtime`) | this container | 3903 | 10 | **0** |
| Backend unit suite (everything outside `tests/tenancy`) | this container | 5113 | 28 | **2**, both environmental and both passing in CI (below) |
| Backend lint (`ruff check .`) | this container | clean | | 0 |
| Documents tell the truth; requirements valid; evidence index generated | this container | all three pass | | 0 |
| Dashboard: `tsc`, vitest, `next build` | this container | typecheck clean; 99 tests (19 files); production build | | 0 |
| Phone app: `tsc`, jest | this container | typecheck clean; 79 tests (17 suites) | | 0 |
| Watch app (Wear OS and AOSP builds, JVM tests); family app native Android build; container images (API, dashboard, backup); secret scan | CI on #88's head, the last code change before `04451d7` | all 11 jobs | | 0 |
| The 22-step care journey, deterministic | database suite above | passed | | 0 |
| The 22-step care journey with the real model chain (`gemma4:e4b`, `gemma4:e2b` on CPU) | #90, `docs/evidence/demo/REAL_MODEL_RUN.md` | 8 of 8 runs, every safety assertion held | | 0 |
| Container stack: health, care flow, outage drills, backup and restore, load baseline | #78, `docs/evidence/staging/CONTAINERS.md` | all | | 0 |
| Off-site encrypted backup and restore drill | #86, `docs/evidence/staging/OFFSITE.md` | identical counts after restore | | 0 |
| Python dependencies (`pip-audit` over `requirements.lock`) | this container | no known vulnerabilities | | |
| Dashboard production dependencies (`npm audit --omit=dev`) | this container | 0 vulnerabilities | | |
| Licences of shipped dependencies (`scripts/licence_check.py --check`, K.d) | CI on #94 and this container | 1,103 declared packages (Python 166, dashboard 24, phone 803, watch Gradle classpath 109): none forbidden. What the artefacts contain (`scripts/supply_chain.py`, this container on 25 November 2026): the watch release APKs (2 native libraries for 4 ABIs each, both traced to their module; 58 and 56 library version files matched to the classpath; the v3 fall model); the API image (128 Python packages, 87 Debian, 37 native libraries bundled in wheels, 13 linked OS libraries), the dashboard image (23 npm, 19 Alpine) and the backup base image (45 Alpine). Fifteen reviewed exceptions. The scan found the base image's Python linking GNU readline, GDBM and Berkeley DB; the three modules are now removed (D-087). Image snapshots lack the OS package steps (no mirror here); CI scans the real images. SBOMs and notices in `docs/security/sbom/` and `docs/legal/` (`docs/security/LICENCES.md`) | | |
| Phone app production dependencies | this container | 0 vulnerabilities (was 20: 1 critical, 3 high, 16 moderate, all in Expo's CLI and Metro build tooling); cleared by npm overrides and two small patches, `docs/security/REVIEW.md` S6 | | |

## Not passed here, and why

| Check | Why | What it needs |
|---|---|---|
| `test_public_url_allowed_by_egress_guard` | no outbound DNS in this container; passes in CI | nothing: environmental |
| `test_published_credentials.py::test_the_table_matches_a_fresh_scan_of_history` | the checkout is shallow; the scan needs the full git history; passes in CI | nothing: environmental |
| IRA CARE OS image (Cuttlefish build and boot) | the build script refused: 42 GB free disk (needs 400), 16 GB RAM (needs 64), 4 cores (floor 8), no `/dev/kvm` | an AOSP build host |
| Emulator runs of the watch and phone apps | no `/dev/kvm` | a host with KVM |
| iOS build | no macOS, no Apple signing | macOS with Apple signing |
| Device runs, screen-reader pass, real-world model accuracy | no physical watch or phone; no consented recordings of older people | devices; consented recordings |
| Live SMS, call, email and monitoring-centre delivery | stand-ins only; no credentials, no contract | provider credentials, a monitoring-centre contract |
| 24 hours of wall-clock soak; capacity on a representative server | one shared container that restarts; a simulated day is recorded (`docs/evidence/soak/SOAK.md`) | a host that stays up; a representative server |

## Licences

Datasets added in rounds 5 and 6, each verified at its source on 25 November 2026 and recorded in
`docs/DATASETS.md` (checked by `test_dataset_register.py`): BITS-2 (Zenodo 10013090, CC BY 4.0),
WISDM (UCI 507, CC BY 4.0), LibriSpeech test-clean (CC BY 4.0, evaluation only). The care
conversation dataset is synthetic and written in this repository. SisFall stays excluded from
release builds (D-077); the shipped fall model v3 is trained only on licensed data (D-080).
