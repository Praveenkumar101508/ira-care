# Integrated verification of round 9 (pull request 128, 27 November 2026)

Run on the round 9 branch (`praveen/vigilant-tesla-bhe022`) after every round 9 change was in it.
Machine: one Linux container, 4 CPUs, 15 GB RAM, no GPU, no `/dev/kvm`; Python 3.13.18 (CI uses
3.12); PostgreSQL 16.15 with pgvector built from source, Redis 7; Node 22; OpenJDK 21 with a local
Android SDK (platform 35, build tools 34 and 35). Every number below is from a run whose output was
read. Overlapping runs are not added together.

| Check | Result | Notes |
| --- | --- | --- |
| Backend unit suite, as CI's unit job (`pytest -ra tests --ignore tests/tenancy`, no database) | **5,831 passed, 85 skipped, 1 failed** in 5 min 45 s | The one failure is the egress-guard test that needs outbound DNS (`example.com`); environmental, passes in CI. The D-004 failure of every earlier round is gone (D-097) |
| Backend database suites, as CI's database job (`tests/tenancy tests/readiness tests/architecture tests/callers tests/idempotency tests/runtime`, as `ira_app`, Redis db 9, role passfile, `PGPASSWORD` unset) | **4,511 passed, 7 skipped, 3 failed** in 14 min 44 s | The three failures were the conversation dataset's labels after the red-flag rule changed (two tests) and one source-reading test of the fall wiring; both fixed after the run (dataset 1.1.1, the test reading the confirmation-stage wiring), and those three files plus the journey tests then **71 passed**. Skips: the six inherited missing-file tests (`INHERITED_TEST_SKIPS.md`) and the destructive failover test, which passed separately against its own database (5 passed, D-098) |
| Backend lint (`ruff check .`) | **0 findings** | |
| Secret scan (`scripts/ci/gitleaks.sh`, whole history to HEAD) | **no leaks found** | |
| Register rules (`scripts/completion_summary.py`) and documents (`scripts/check_docs_truth.py`) | **pass** | register 1.10.0, 258 items |
| Dashboard (`npm ci`, `typecheck`, `vitest`, `build`) | **160 passed** in 24 files; typecheck clean; build exit 0 with `/admin/devices` | 138 before the round |
| Phone app (`tsc --noEmit`, `jest --ci`) | **94 passed** in 20 suites; typecheck clean | 83 before the round |
| Watch app, both flavours (`./gradlew testWearosDebugUnitTest testAospDebugUnitTest`) | **207 passed per flavour**, 0 failed, 0 skipped | includes `FallConfirmationTest` (8), `FallInjectionTest` (3), `SyntheticFallReleaseTest` (4) |
| 1-hour wall-clock soak (`scripts/soak_wallclock.py --start-demo --no-quiet-hours --hours 1`) | **PASS on every criterion; API memory 1.996 % an hour** | `docs/evidence/soak/WALLCLOCK_1H_ROUND9.md`; not the 24-hour run (G2.e) |
| Encrypted backup and restore round trip | 169 tables and 120 migrations before and after | `docs/evidence/ops/DRILLS_ROUND9.md` |
| The 22-step care journey, deterministic model | 1 passed | the same page |
| Conversation evaluation, deterministic model | the 162-turn set unchanged turn for turn (red-flag recall 1.0); round 9 development set 129 of 129; hold-out 37 of 41 | `docs/evidence/eval/round9/` |
| Fall pipeline on the synthetic scenarios, shipped model through LiteRT | 154 of 160 falls caught with and without the confirmation stage; alarms 88 to 62 | `docs/ai/fall/synthetic_confirmation.json` |

## Native journeys on GitHub's standard runners (the pull request's `native checks`)

| Journey | Latest completed run | Result | Fix since, awaiting its run |
| --- | --- | --- | --- |
| Wear OS, 44 steps | 45910155640 | **43 of 44**: everything but `unpair_warning_shown` | the journey scrolls to the Unpair button; the job has 60 minutes |
| Android phone, sign-in smoke plus 17 journey steps | 45974160883 | **smoke 4 of 4; journey 16 of 17**: everything but `carer_signed_in` | the journey leaves the Talk screen before signing out |
| iOS simulator | 45910155640 | built with its JavaScript bundled, booted, installed, launched, **still running, no crash report**; `sign_in_screen_shown` failed (the console probe line went unseen) | the probe also posts to a loopback listener |

No run found a defect in the apps themselves; every failure was the journey script's or the
runner's (its network after boot, a launcher hang, the timeout). The results of the runs that carry
the fixes are recorded on the evidence pages (`WEAR_EMULATOR_JOURNEY.md`, `PHONE_EMULATOR_SMOKE.md`,
`IOS_SIMULATOR.md`) as they complete.

# Integrated verification on `main` (register K, round 8)

Run on 26 November 2026 on `main` at `540c6e5` (PRs up to #122 merged; the unit suite ran on
`cd20743`, which differs from `540c6e5` only by #122's workflow, smoke script and documents). The
database for the run was created empty and migrated as `ira_migrator` (123 migrations). Every
number below comes from a run whose output was read. The machine is the same shared Linux container
as round 7 (4 CPUs, 15 GB RAM, no GPU, no `/dev/kvm`); Python 3.13.18 here, CI uses 3.12.

| Suite | Result | Note |
|---|---|---|
| Backend unit and readiness (`tests`, without `tests/tenancy`) | **5,671 passed, 82 skipped, 2 failed** in 5 min 24 s | The same two failures as round 7, both explained below; nothing new |
| Backend database suite (`tests/tenancy`) on the fresh database | **2,977 passed, 9 skipped** in 10 min 57 s | Includes the new fall-overlap (D-092) and push-settlement tests |
| Readiness suite alone, on #122's tree | 512 passed, 38 skipped | |
| Dashboard (`vitest`) | **138 passed** in 23 files | |
| Family phone app (`jest`) | **83 passed** in 18 suites, after `npm ci` | A first run on a stale local `node_modules` failed one test (`tar` 6.2.1 where the override pins 7.5.23); a clean install passes. CI always installs clean |
| `ruff` | clean | |
| `check_docs_truth.py`, `completion_summary.py` | pass; 154 of 253 complete, 54 blocked | |
| Wear OS emulator care journey | **passes**, 11 steps | On GitHub's KVM runner, five runs (`docs/evidence/emulator/WEAR_EMULATOR_JOURNEY.md`) |
| Family app on an Android phone emulator | **passes**, 4 steps | Two runs (`docs/evidence/emulator/PHONE_EMULATOR_SMOKE.md`) |
| iOS simulator build | **compiles** | Free macOS runner, six runs (`docs/evidence/builds/IOS_SIMULATOR.md`) |

### The two unit failures, unchanged from round 7

- `test_prompt_injection_guard.py::test_public_url_allowed_by_egress_guard`: this container has no
  outbound DNS, so the guard cannot resolve `example.com`. Environmental; passes in CI.
- `test_published_credentials.py::test_the_table_matches_a_fresh_scan_of_history`: runs
  `scripts/security/published_credential_digests.py`, which was never carried over (D-004). It
  skips on CI's shallow clone and fails on a full clone. The owner's decision is still open.

### Watched by CI rather than run here

The watch's JVM tests (188 per flavour), the release builds and the AOSP dependency check ran in
the `tests` workflow on every merged pull request; the emulator and simulator jobs ran in the
`native checks` workflow on #119 and #122. All green at each merge.

### Wall-clock soak

The 1-hour soaks from #117 failed only the memory criterion (6.65 and 6.45 % an hour against 5 %).
#123 fixes three causes with regression tests; its complete 1-hour run on the fixed code
(`docs/evidence/soak/WALLCLOCK_1H_FIXED.md`) passed every criterion but memory, at 5.18 % an hour:
lower, still a FAIL. The 24-hour run (G2.e) has not been started: this container restarted during
the night.

# Integrated verification on `main` (register K, round 7)

Run on 25 November 2026 on `main` at `200533e` (PRs up to #111 merged; the same tree as #109's head
`b49ebef`, tree `5bc8a2c`). The database for the run was created empty and migrated as
`ira_migrator`. Every number below comes from a run whose output was read; nothing is estimated.
Counts from different runs overlap (the unit run collects the database files too, where they
skip), so they are never added together. Earlier runs (after #61 at `eed43f1`, after #80 at
`eec19a0`, after #88 at `04451d7`) are in this file's history.

Machine: one shared Linux container, 4 CPUs, 15 GB RAM, no GPU, no `/dev/kvm`, about 22 GB of
free disk. Other work was running on it at the same time. Python 3.13.18 here; CI uses 3.12.
Local PostgreSQL 16.15; CI uses `pgvector/pgvector:pg16`. Node 22.23.5. OpenJDK 17.0.21.

## Passed, failed and skipped

| Check | Where | Passed | Skipped | Failed | Warnings |
|---|---|---|---|---|---|
| Migration chain to an empty database as `ira_migrator`; `scripts.check_migration_integrity` | this container | 120 migrations applied, `init.sql` to `123_device_admin_factory_reset_approval`; 169 tables; 120 files, all hashes match | | 0 | |
| Database suites as CI runs them (`tests/tenancy tests/readiness tests/architecture tests/callers tests/idempotency tests/runtime`, CI's environment including `IRA_REQUIRE_DB_HARNESS=1`, plus a 300 s per-test limit added here) | this container, 947 s | 4,199 | 9 (below) | 1: the SOS stream test reached the 300 s limit added here. Run on its own it **passed in 301.26 s** (below) | 213: test keys shorter than 32 bytes (`InsecureKeyLengthWarning`), one pydantic field name, one test helper class pytest cannot collect |
| The same suites in CI on `b49ebef` (identical tree) | CI job "Backend database tests" | 4,191 | 18 | 0 | 214 |
| Backup and restore round trip (`deploy/backup.sh`, `deploy/restore.sh`, as CI's step runs them) | this container, `pg_dump` 16.15 | 256 KB encrypted file, checksum verified; restored into a new database: 169 → 169 tables, 120 → 120 migrations, 2,962 → 2,962 rows | | 0 | |
| Backend unit suite as the CI job runs it (`pytest tests`, no database: the database tests skip) | this container, 325 s | 5,740 | 558 | **2**, both explained below | 87 |
| The same, outside `tests/tenancy` (`--ignore=tests/tenancy`) | this container, 295 s | 5,430 | 82 | **2** (the same two) | 87 |
| The same suite in CI on `b49ebef` | CI job "Backend unit tests" | 5,753 | 196 | 0 | 86 |
| Backend lint (`ruff check .`, ruff 0.16.9) | this container | clean | | 0 | |
| Dashboard: `npm ci`, `tsc --noEmit`, vitest, `next build` | this container | typecheck clean; 137 tests (23 files); production build of every route | | 0 | `npm ci`: 1 deprecation notice (`whatwg-encoding`) |
| Phone app: `npm ci`, `tsc`, jest | this container | typecheck clean; 83 tests (18 suites) | | 0 | `npm ci`: 17 deprecation notices in build tooling |
| Watch app: `assembleWearosDebug assembleAospDebug testWearosDebugUnitTest testAospDebugUnitTest` (CI's command) | this container, Android SDK 35 installed for the run and removed afterwards | both debug APKs built; 171 JVM tests per flavour (342 test runs), all executed here, not taken from a cache | 0 | 0 | 2 Kotlin opt-in warnings in one test file (`ActivitySharingTest.kt:261`), reported once per flavour |
| Watch: the AOSP flavour has no Google service dependency (`scripts/ci/check_watch_aosp_deps.sh`) | this container | 626 dependency lines, none from a Google service | | 0 | |
| Watch: release APKs (`assembleWearosRelease assembleAospRelease`) and what they contain (`scripts/supply_chain.py apk --check`) | this container | both built, unsigned (Wear OS 17,640,387 bytes, AOSP 17,428,594 bytes); 8 native libraries and 5 assets each; 58 and 56 library version files matched to the classpath | | 0 | Gradle reports features deprecated for Gradle 9 |
| The 22-step care journey, deterministic model | database suite above (`test_care_journey_db.py`) | passed | | 0 | |
| The 22-step care journey with the real model chain (`gemma4:e4b`, then `gemma4:e2b`, Ollama 0.40.18, CPU only) | this container; its own empty database | **8 of 8 runs**, every safety assertion held (below) | | 0 | |
| API and dashboard images (`scripts/staging/sandbox_build.sh`), CI's image checks | this container | API runs as uid 10001 and carries 120 migrations; the dashboard runs as uid 10001; both images' third-party notices are byte-identical to `docs/legal/notices/`; the API image imports `care.server` and `care.worker` with no network | | 0 | |
| Container stack (`docker-compose.yml` + `compose.staging.yml` + `compose.nomodel.yml`): Postgres, Redis, API, worker, dashboard, Caddy | this container | up in 21 s from empty volumes, 120 migrations applied at start; `/health` 200, `/login` 200, `/health/jobs` 200 after the worker's first pass; care flow over HTTPS (`containers_smoke.py`) **13 of 13 steps** | | 0 | Caddy's container health check never passes (below) |
| Images in CI on `b49ebef`: all three built (backup image included), the same checks, image inventories against the licence policy, syft SBOMs | CI job "Container images build" | passed | | 0 | |
| Watch, family app (Expo, Android build), dashboard, images, secret scan, documents | CI on `b49ebef`: all 11 jobs | 11 of 11 | | 0 | |
| Secret scan (`scripts/ci/gitleaks.sh`, gitleaks 8.24.3, full history) | this container | 119 commits, no leaks found | | 0 | |
| Documents tell the truth (`scripts/check_docs_truth.py`) | this container | every row in one of the three states; every proof exists | | 0 | |
| Requirements (`scripts/completion_summary.py`) | this container | 253 requirements valid: implemented 44, accepted 79, verified 71, blocked by something external 58, not started 1 | | 0 | |
| Evidence index (`scripts/evidence_index.py`; it has no check mode, so it was generated and read) | this container | generated | | 0 | |
| Python dependencies (`pip-audit -r <file> --no-deps --disable-pip`, as in `docs/security/REVIEW.md`) | this container | `requirements.lock` (166 packages), `requirements-api.lock` (110), `requirements-nodeps.txt` (1): no known vulnerabilities | | | |
| npm dependencies (`npm audit`, development dependencies included, and `--omit=dev`) | this container | dashboard 0 and 0; phone app 0 and 0 | | | |
| Licences of shipped dependencies (`scripts/licence_check.py --check --only <ecosystem>`) | this container | Python 166 (161 allowed, 5 reviewed exceptions; read from a virtual environment holding exactly `requirements.lock`); dashboard 24 (22, 2); phone 803 (802, 1); watch Gradle classpath 109 (109): none forbidden, inventory up to date | | 0 | |
| SBOMs and notices rendered from the inventories (`scripts/supply_chain.py render --check`) | this container | up to date | | 0 | |

### Skips, by reason

**Database suites here (9).** Six tests whose harness or input file is not in this repository:
`benchmarks/acceptance`, `benchmarks/interactive/interactive_bench.py`,
`scripts/check_benchmark_ratchet.py`, `scripts/models/evaluate.py`,
`scripts/staging/p17_system_benchmark.py` and `docs/cloud/CLOUD_PHASE_STATUS.md`. These tests came
over from the source project without their files, and they skip in CI the same way. Two need
`IRA_TEST_REDIS_URL` (global logout across processes), which CI does not set either. One is the
destructive failover test, which stops every database connection and runs only with
`IRA_TEST_ALLOW_DESTRUCTIVE`.

**Why CI skips 9 more (18).** CI also skips three `test_no_pyav.py` tests, because PyAV is not
installed there ("could not import faster_whisper: No module named av"). It also skips six
`test_migration_runner_separation.py` tests ("cannot connect as ira_app / ira_worker /
ira_migrator"). Here those nine ran and passed. The total collected is the same in both places:
4,209.

**Unit suite here (558).** 525 database tests with no database. 18 Windows installer tests with no
PowerShell. 2 with no `livekit-agents`. One each: Playwright browser test, OCR without
`pdftoppm`/`tesseract`, faster-whisper installed (so the alternative path is live), dataset not
checked out, no `torch`, brain-affect layer off, no Cortex gateway. The six missing-file skips
are the same as above. Outside `tests/tenancy` there are 82: 55 database tests, 18 PowerShell and
the nine single-reason skips above.

**Why the unit totals differ from CI (6,300 collected here, 5,949 in CI).** CI's unit job does not
install `psycopg2`. 21 test modules call `pytest.importorskip("psycopg2")` at module level, so in
CI each whole module counts as one skip, while here their 364 tests are collected one by one.
That accounts for 343 of the 351. The last eight were not traced.

### The SOS stream test does not hang: it waits 300 s

`tests/tenancy/test_care_sos_db.py::test_the_stream_route_answers_with_the_current_state_first`
was reported as hanging on `main`. It does not hang. The test client (httpx 0.27.2
`ASGITransport`) buffers the whole response body before it returns. The route keeps the stream
open for `max_seconds=300` (`care/sos.py`, after which the watch reconnects). So the test always
takes just over 300 s. In the full run it hit the 300 s limit added here and was counted as a
failure. Run on its own with a longer limit, it passed in 301.26 s. CI runs it with no limit, so it
passes there and adds about five minutes to the database job. This does not depend on the
environment. It is a slow test, not a defect in the product.

Fixed after this verification (branch `fix/verification-defects-round7`): the stream's duration is
now read from `care/sos.py::STREAM_MAX_SECONDS` when each stream starts (production keeps 300 s,
pinned by `tests/readiness/test_sos_stream_duration.py`). The test shortens it to 1 s and checks
that the first line equals the current state. It took 301.21 s before and 2.55 s after; it fails
when the stream's first line is not the current state.

### The two unit failures

- `test_prompt_injection_guard.py::test_public_url_allowed_by_egress_guard`: the guard could not
  resolve `example.com` because this container has no outbound DNS. It is environmental and passes
  in CI.
- `test_published_credentials.py::test_the_table_matches_a_fresh_scan_of_history`: the test runs
  `scripts/security/published_credential_digests.py`, which was never carried over into this
  repository (D-004). This checkout has the full history, so the test runs and fails. CI checks out
  a shallow clone, so there the test **skips**. (The previous version of this file said it
  passes in CI; that was wrong.) Bringing the script over or retiring the test is still the
  owner's decision (D-004).

### The real model in the care journey (8 runs)

`scripts/demo/real_model_journey.py --runs 8` used the product's own chain and time budgets, the
real care API over ASGI, and an empty database migrated as `ira_migrator`. Models were pulled
for the run and removed afterwards: `gemma4:e4b` (digest `dc35e8d9c606`, 6.58 GB) and `gemma4:e2b`
(`b37049369adf`, 4.59 GB), the same digests as the round-6 run. All people were fictional. SMS
went to a recording provider and push to a fake service, and nothing left the machine.

| Run | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| Result | passed | passed | passed | passed | passed | passed | passed | passed |
| Time | 49.9 s (includes 9.6 s of model loading) | 36.5 s | 35.2 s | 36.1 s | 35.9 s | 35.9 s | 35.5 s | 36.7 s |
| Step 5 question used | template | model | template | model | template | model | model | template |

The test asserted these in every run:

- **Step 5:** one question at a time, asked by name, and the answer recorded.
- **Step 6:** the consented preference ("her garden roses") reached the model's prompt.
- **Step 8:** "maybe, I can't remember" was read as unclear, with `medication_taken: unknown`. No
  dose was written and the reminder was snoozed.
- **Step 19:** the daily report contains neither "garden roses" nor "can't remember".

The raw model output, checked afterwards with the product's own guard (`care/conversation.py`):

- **Diagnosis words:** none in any of the 8 talk replies or 8 questions.
- **Medication answers:** `medication_taken: unknown` in all 16 answer readings; no reading came
  back empty.
- **The one-question rule did its job:** in runs 1, 3, 5 and 8 the model wrote two questions in
  one (for example "…how was your dinner this evening? Did you enjoy it?"). The guard refused
  them, and the product asked its template question instead.
- **Over-cautious advice rule:** in runs 1, 3 and 5 the talk reply's second sentence ("I hope you
  have a peaceful evening, Edith.") matched the advice rule's `you have` pattern and was refused.
  Edith heard only the first sentence. The rule fails safe, but it is broader than it needs to be.
  It is recorded here and not changed in this round. Fixed afterwards (D-091): the rule now
  refuses a "you have" only when it cannot place it as plainly social, so this sentence is spoken,
  and hedged diagnoses the old pattern let through ("you may have a urine infection") are refused.
  Pinned by `tests/care/test_care_advice_guard_corpus.py`.
- **Her preference in replies:** five of the eight talk replies mentioned her roses, and in each
  of those five the sentence that mentioned them was spoken.

| Call | Calls | Median total | Worst total | Median first token |
|---|---|---|---|---|
| answer reading | 16 | 9.50 s | 12.20 s | (not streamed) |
| check-in question | 8 | 4.37 s | 7.57 s | 1.19 s |
| talk | 8 | 7.54 s | 9.10 s | 1.56 s |

### Images and the container stack

`scripts/staging/sandbox_build.sh` builds from the real Dockerfiles with two changes. This
container cannot reach the Debian and Alpine package mirrors, so the `apt-get` and `apk add` steps
were replaced by a minimal `curl` stand-in. Those steps were therefore **not exercised here**, and
the backup image (which needs `apk add`) was **not built here**. CI built all three images
unchanged on `b49ebef` and ran the same checks, plus the image inventories and SBOMs.
Some build layers were reused from Docker's cache where their content was unchanged.

In the stack, Caddy served HTTPS correctly: every request above went through it. Yet its
container never became healthy. Its health check fetches `http://127.0.0.1:80/health`, Caddy
answers with a redirect to `https://127.0.0.1/health`, and that TLS handshake fails because no
certificate exists for an IP address. This reproduces with the Caddy image alone. Nothing waits
on Caddy's health, so traffic is unaffected, but the check reports the proxy as unhealthy even
when it is serving. It is recorded here and not changed in this round. Fixed afterwards: the
check now asks Caddy's admin API inside the container whether the site's server is loaded
(`wget -q -O - http://127.0.0.1:2019/config/apps/http/servers | grep -q '"listen"'`). With the
Caddy image alone the container became healthy, the same command fails when no site is loaded, and
the HTTPS answers (redirect, status codes, HSTS) were identical before and after. Pinned by
`tests/readiness/test_caddy_healthcheck.py`.

## Not passed here, and why

| Check | Why | What it needs |
|---|---|---|
| `test_public_url_allowed_by_egress_guard` | no outbound DNS in this container; passes in CI | nothing: environmental |
| `test_the_table_matches_a_fresh_scan_of_history` | the script it runs is not in the repository (D-004); skips on CI's shallow clone | the owner's decision (D-004) |
| `apt-get`/`apk add` image steps; the backup image | package mirrors unreachable from this container | nothing: CI built all three images on `b49ebef` |
| Off-site encrypted backup drill | not repeated here; #86 (`docs/evidence/staging/OFFSITE.md`) restored with identical counts. The local round trip above and CI's backup step on `b49ebef` passed | nothing new |
| IRA CARE OS image (Cuttlefish build and boot) | `os/scripts/build_cuttlefish.sh` preflight refused: 22 GB free disk (needs 400), 16 GB RAM (needs 64), 4 cores (IRA's floor is 8); no `/dev/kvm` | an AOSP build host |
| Emulator runs of the watch and phone apps | no `/dev/kvm` | a host with KVM |
| iOS build | no macOS, no Apple signing | macOS with Apple signing |
| Device runs, screen-reader pass, real-world model accuracy | no physical watch or phone; no consented recordings of older people | devices; consented recordings |
| Live SMS, call, email and monitoring-centre delivery | stand-ins only; no credentials, no contract | provider credentials, a monitoring-centre contract |
| 24 hours of wall-clock soak | this container restarted during the session (last boot 25 November 2026 18:39 UTC; up 4 h 37 min when checked at 23:16); a simulated day is recorded in `docs/evidence/soak/SOAK.md` | a host that stays up |
| Capacity on a representative server | one shared 4-CPU container | a representative server (the owner's approval to spend) |
| Penetration test | none commissioned | a contracted, qualified tester (C-22.d) |

## Licences

No dataset was added in this round. The licence check above covers every ecosystem that ships:
Python, dashboard and phone npm, and the watch's Gradle classpath. None is forbidden, and the
reviewed exceptions are listed in `docs/security/LICENCES.md`. Datasets stay as recorded in
`docs/DATASETS.md` (checked by `test_dataset_register.py`). SisFall stays excluded from every
build, debug builds included (D-077, D-082). The shipped fall model v3 is trained only on licensed
data (D-080). The Gemma 4 models used in the real-model run are Apache-2.0
(`docs/MODEL_CHOICE.md`), and no model weights are in this repository.

## Round 10 (27 November 2026): what moved to verified, and why

Each item below moved only because integration-level evidence that shows its whole title exists
and is now cited; nothing was reclassified to raise the figure. Software verification: 82 to 91
of 210.

| Item | Evidence |
|---|---|
| D3.l Deployment administration screen | the four-test browser journey against a running server (`docs/evidence/browser/DEPLOYMENT_ADMIN_BROWSER.md`) |
| C-1.b The 18 mandatory ambiguous dose answers | each recorded through the real check-in cycle on PostgreSQL (`test_each_mandatory_answer_is_recorded_as_the_rules_read_it`, 18 cases) |
| F2.b The conversation evaluation runner | the round 10 hold-out run (`docs/evidence/eval/round10/holdout_fake.json`) |
| F3.a The classical heart-rate estimator | its leave-one-subject-out evaluation on PPG-DaLiA (`docs/ai/heart/results.json`) |
| F4.a The rule-based fall detector | its held-out evaluation on BITS-2 (`docs/ai/fall/results_v3.json`, `rule_detector`) |
| F5.d The round 9 scenario families | run through the real conversation code, with the round 10 hold-out |
| G2.j API memory within 5 % an hour | the complete 1-hour wall-clock soak (`WALLCLOCK_1H_ROUND9.md`) |
| D3.f The audit trail of care actions | the access log append-only, kept on erasure and written for plan reads and writes, on PostgreSQL |
| C-22.a Every person-scoped route guarded | the isolation suites refusing another family's person on real routes, on PostgreSQL |

Left as accepted after review, because their integration evidence shows only part of the title:
D1.f and D1.g (the server half of the watch's location and battery reports), G2.c (the heartbeat,
not the dashboard's banner), G2.g and G2.h (drills not on staging or off-site), E2.o (the watch's
side of an instruction), F4.i (switched off), the three model cards and C-22.c.

## Round 10, continued (28 November 2026): nineteen more moved to verified

Seven by new integration tests, run on the build machine against the running care server and the
production dashboard build (`docs/evidence/browser/ROUND10_VERIFICATION_BROWSER.md`), and one by
correcting an evidence label. Software verification: 91 to 110 of 210.

| Item | Evidence |
|---|---|
| J.h Demo pages refused in production | `e2e/verification.spec.ts`: 404 for `/watch`, `/presenter`, `/simulator` from `next start`; the carer pages answer 200 |
| G2.c Monitoring heartbeat shown on the dashboard | the banner absent while the care jobs run, and present on the home page and the record once a server started with no care jobs has had none run for five minutes (the half left out above) |
| C-11.a Unknown and stale wrist status | a three-hour-silent watch reads "last known on the wrist", a never-reported one "wrist unknown", a just-synced one "on the wrist" |
| D4.b Unavailable measurements | blood oxygen and blood pressure from the person's own watch refused (422) and not stored; listed as not available, with no value |
| C-16.b The dashboard renews an expired session | with one-minute access tokens, the first token refused by the server, and the record still loads through a renewal |
| C-23.a Automated accessibility checks on the dashboard | axe-core in Chromium on six pages, no serious or critical violation, after fixing the unnamed controls it found on a person's record |
| C-23.d Contrast in dark and high-contrast appearances | axe's colour-contrast rule in Chromium on six pages in light, dark and each with "more contrast": 24 checks, no violation |
| F7.i Activity trend on the health tab, labelled an estimate | the watch's summaries refused without the sharing consent (nothing stored, "not shared" shown) and stored and shown as "Activity (estimated)" with it, in Chromium against the running server |
| C-16.a The phone renews an expired session | the phone app's web build with one-minute access tokens: the expired token refused, renewed through `/auth/refresh`, still signed in |
| C-11.b, D2.b, D2.c, D2.d The phone app: stale wrist, appearance on every device, reminders, visits | the phone app's web build in Chromium against the running server (`docs/evidence/browser/ROUND10_PHONE_WEB.md`), 4 of 4, after fixing the phone's stale-wrist wording |
| C-6.a Care logs carry no personal data | `tests/tenancy/test_care_logs_no_personal_data_db.py`: the 22-step care journey on PostgreSQL, every logger and the log-only provider read, nothing personal found; with a negative control |
| C-6.b Exception text kept out of logs | `tests/tenancy/test_log_exception_text_db.py`: real errors from PostgreSQL and asyncpg whose messages quote a private sentence (invalid input, a refused argument, a duplicate key's detail), logged five ways through the production handler in both formats: the sentence never appears, the type always does; with the developer switch on, the same records carry it (the control) |
| G2.a Backup files are private | the deployment's `deploy/backup.sh` run on PostgreSQL under umask 022: folder 0700, files 0600, still restores (`test_deploy_backup_private_db.py`), after the runbook drill found them 0644 and the script was fixed |
| G2.d Behaviour under concurrency | **a label corrected, not new evidence**: `tests/readiness/test_behaviour_under_load.py` connects to PostgreSQL (it skips without `IRA_TEST_DATABASE_DSN`), runs in CI's database job, and passed 5 of 5 here against a real database; it had been filed as a unit test. A sweep of every accepted item found no other test filed this way |
| D2.a Wearer, family and carer experiences from one app | the Android journey, 18 of 18 on GitHub's emulator (run 45990580023, `docs/evidence/emulator/PHONE_EMULATOR_SMOKE.md`): one build signed in as the family member (person card, record, plan, Help banner, "I'm on my way"), as the wearer (Talk to IRA and its reply) and as the carer (the carer's cards, none of the family experience's) |

C-7.b (Talk offered only to the wearer) moved to verified on the phone emulator run 46100618011
(18 of 18), the first with the checks that the family member and the carer are not offered Talk.

G4.c (the operations runbooks) moved from implemented to accepted, not verified: the drills ran
sections 2 and 5 against the production server and worker (`docs/evidence/ops/DRILLS_ROUND10.md`),
but the provider-secret rotations and the compose wrappers themselves were not exercised.

## Round 10, continued again (28 November 2026, after pull request 133)

Software verification: 110 to 118 of 210. Every accepted-but-not-verified requirement is now read one
by one in `docs/progress/VERIFICATION_MATRIX.md`, with its single route to integration evidence.

| Item | Evidence |
|---|---|
| C-10.c A failed section is named and the rest shows | the visits request made to fail in Chromium: named, the rest of the record shown, and nothing named once it works (`docs/evidence/browser/ROUND10_RECORD_AND_PHONE_A11Y.md`) |
| C-23.b Automated accessibility checks on the phone app | axe in the phone app's web build on the sign-in screen and each experience's home and settings, after fixing an invalid autocomplete token and names that did not contain their visible words (WCAG 2.5.3) |
| C-12.b Talk shows IRA's answer as it arrives | the wearer's Talk screen in the phone app's web build, the server's real reply released a line at a time: the first sentence shows before the rest arrive; a control with streaming switched off fails (`docs/evidence/browser/ROUND10_TALK_AND_SWITCH.md`) |
| C-10.b Overlapping refreshes show only the newest person | the first person's 15 requests held back six seconds while the dashboard moves to the second: only the second person's heading ever shows, before and after the late answers land |
| D1.g Watch reports battery | the Wear emulator's battery set to 23 %: the server's device row reads 23, after fixing the watch's battery reading (`docs/evidence/emulator/WEAR_EMULATOR_JOURNEY.md`, run 46146704537) |
| D1.b Watch visit state | a visit planned, arrived and departed through the API: the watch shows "Uday is coming", "Uday is here." (and acknowledges it to the server), then "All well" |
| C-21.b Unpair refused while something urgent is unfinished | a fall while the unpair screen is open: refused, no Unpair button; the countdown answered and the watch still paired |
| D1.f Watch sends location with an SOS | Help on the Wear emulator with a GPS fix set: the alert carries 51.5071983, −0.1275983 (5 m), run 46154458625, after the watch was made to ask a high-accuracy fix and to send a late position when Help goes without one |

