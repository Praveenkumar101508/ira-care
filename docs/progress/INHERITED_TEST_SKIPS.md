# Inherited tests that skip for a missing file

Seven tests in `supracloud-jarvis/ira/tests` came over from the source project without the
file they exercise. Each skips at import time (or at its first line) with the missing path in
the reason, locally and in CI alike. This page records, for each, what the file was, whether
it protects something IRA CARE ships, and the decision the owner needs to take. None of the
tests has been deleted or changed; every skip is still reported in the `-ra` summary of both
CI jobs.

The split that made IRA CARE dropped 165 test files of the source project's removed parts, and
D-007 records the owner's one deliberate retirement of eight inherited test functions. These
seven are the remainder: they were kept because they looked as if they might matter, and this
page is the evidence for deciding whether they do.

## How the question was decided

For each test the questions were: what does the missing file do in the source project; does
IRA CARE have the thing it measures or governs; and if so, is it checked another way here.
"Relevant" below means IRA CARE ships the behaviour the test protects. A minimal stand-in was
written only where the answer was yes and the stand-in could be honest; a benchmark harness
written to satisfy its unit tests, for a benchmark IRA CARE never runs, would be neither.

## The seven

| Test | Missing file (repository-relative) | Relevant to IRA CARE | Decision needed |
|---|---|---|---|
| `tests/tenancy/test_acceptance_suite.py` | `benchmarks/acceptance/runner.py` (and `scorer.py`, `benchmarks/interactive/`) | No | Retire or keep skipping |
| `tests/test_conditional_memory_retrieval.py::test_every_memory_prompt_of_the_acceptance_dataset_retrieves_and_no_document_prompt_does` | `benchmarks/acceptance/dataset/memory.json`, `documents.json` | No | Retire or keep skipping |
| `tests/tenancy/test_p0_interactive_bench.py` | `benchmarks/interactive/interactive_bench.py`, `trace_proxy.py`, `api_probe/sitecustomize.py`, `benchmarks/datasets/interactive_scenarios.json` | No | Retire or keep skipping |
| `tests/tenancy/test_p17_system_benchmark.py` | `scripts/staging/p17_system_benchmark.py`, `benchmarks/SYSTEM_BENCHMARK_METHODOLOGY.md` | No | Retire or keep skipping |
| `tests/tenancy/test_p17_benchmark_ratchet.py` | `scripts/check_benchmark_ratchet.py`, `benchmarks/.benchmark-baseline.json` | No | Retire or keep skipping |
| `tests/tenancy/test_p17_evaluate_harness.py` | `scripts/models/evaluate.py`, `scripts/models/_selftest_endpoint.py`, `docs/cloud/evidence/MODEL_LICENCE_EVIDENCE.json` | No | Retire or keep skipping |
| `tests/tenancy/test_p9_status_table_is_current.py` | `docs/cloud/CLOUD_PHASE_STATUS.md` | Replaced | Retire |

### `test_acceptance_suite.py` and the acceptance dataset

The source project's "real-user acceptance suite" drove its general chat assistant through a
model and a mail server and scored the replies (`scorer.py`: leaked secrets, refusals, claimed
actions). The tests check the scorer's negative controls and the runner's judgement, and each
has a planted defect in `.github/scripts/verify-acceptance-suite.sh`, which is not here either
(`.github/scripts/` does not exist in this repository). The dataset test in
`test_conditional_memory_retrieval.py` reads the same suite's `memory.json` and
`documents.json` prompts.

IRA CARE's conversation is the care conversation (`care/conversation.py`), checked by its own
corpus tests (`tests/care/test_care_advice_guard_corpus.py` and the rest of `tests/care`) and by
`scripts/demo/real_model_journey.py` with a real model. The acceptance suite's prompts are the
source project's chat prompts, not IRA CARE's. The security prompts that mattered were copied
into `tests/test_security_gate_before_agents.py` when the split happened, and that test runs.
Nothing IRA CARE ships is protected only by these two.

### `test_p0_interactive_bench.py`

The interactive-latency harness measured first-visible-token and percentile latencies of the
source project's chat stream on a declared-quiet owner machine, through a trace proxy. The
tests check the quiet-machine guard, the percentile arithmetic and the proxy's forwarding, each
against a planted defect in `.github/scripts/verify-interactive-bench.sh`. There is no
`benchmarks/` directory in this repository and no such CI step. IRA CARE's latency evidence is
the care journey's own timings (VERIFICATION.md, "The real model in the care journey") and the
wall-clock soak (`docs/evidence/soak/`), which measure the paths IRA CARE serves.

### `test_p17_system_benchmark.py` and `test_p17_benchmark_ratchet.py`

Two halves of one thing: a system benchmark run as a CI step (`system-benchmark`) that drove
the source project's routes over a real socket, and a ratchet gate that compared its output
with a committed baseline (`benchmarks/.benchmark-baseline.json`). The tests pin the
classification of a figure as a measurement and which of the ratchet's four controls are real.
IRA CARE's `tests` workflow has no benchmark step and no baseline; writing a harness and a gate
only so that these tests have something to import would create a benchmark nobody runs.

### `test_p17_evaluate_harness.py`

`scripts/models/evaluate.py` evaluated candidate chat models through an Ollama endpoint and
refused a candidate whose artefact digest did not match `MODEL_LICENCE_EVIDENCE.json`; the
tests start a real HTTP server and run the harness as a subprocess. The evidence file names
the source project's candidates (`qwen3:14b`). IRA CARE's model choice and its digests are
recorded in VERIFICATION.md (`gemma4:e4b`, `gemma4:e2b`) and exercised by
`scripts/demo/real_model_journey.py`; its model licence evidence is in `docs/security/LICENCES.md`.
If the owner wants a digest-pinned evaluation of IRA CARE's own candidates, that is a new
requirement with its own harness and its own evidence file, not this test's file.

### `test_p9_status_table_is_current.py`

The source project's `docs/cloud/CLOUD_PHASE_STATUS.md` was a phase table whose rows had
drifted from the evidence beside them; the test checked the table's structure against
`docs/cloud/evidence/`. IRA CARE keeps its status in `docs/progress/STATUS.md`,
`docs/progress/requirements.json` and `docs/progress/VERIFICATION.md`, and the `docs-truth`
CI job (`scripts/check_docs_truth.py`) makes the same kind of check for them on every run.
The file this test wants will never exist here, and its purpose is served. It is the one of
the seven that is plainly replaced rather than merely absent.

## What this page does not do

It does not retire anything. The owner's rule is that no test is deleted to get green, and
D-007 shows the one form a retirement takes: an owner's decision, recorded in DECISIONS.md,
naming the functions. Until then the seven skip, with their reasons printed by both CI jobs.
