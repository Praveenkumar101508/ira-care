"""P17-01 — the benchmark harness refuses, and can be watched refusing (and not).

`PRE_PRODUCTION_REQUIRED.md` PP-03 names `scripts/models/evaluate.py` and gives
its definition of done as a `results.json` per candidate. The command did not
exist, so the owner held an action they could not take.

── What these tests drive, and why it is not a unit test ───────────────────

Every test here that matters starts a **real HTTP server on a real socket** and
runs the harness as a **subprocess**, exactly as PP-03's command line invokes
it. Nothing is imported and called with a fake.

That is not thoroughness for its own sake. P14-R1 shipped a lockdown
enforcement point whose fifteen unit tests all passed while the real pipeline
raised `AttributeError`, because the tests called the policy function directly
and the wiring read a field that does not exist. The harness has the same shape:
its whole job is to drive a real provider over a real socket, so a test that
does not do that is testing something else.

── The property that is easy to get backwards ──────────────────────────────

A refusing harness is easy to write and worthless. `test_the_recorded_digest_
is_accepted` exists so that every refusal below is a *decision* rather than the
only thing this code can do — a check nobody has watched succeed is a check that
might be refusing for a reason nobody has understood.
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[4]
EVALUATE = REPO / "scripts" / "models" / "evaluate.py"
ENDPOINT = REPO / "scripts" / "models" / "_selftest_endpoint.py"
EVIDENCE = REPO / "docs" / "cloud" / "evidence" / "MODEL_LICENCE_EVIDENCE.json"

if not EVALUATE.is_file():
    pytest.skip(f"{EVALUATE} not found", allow_module_level=True)

#: A configured Ollama model with a recorded artefact digest. Read from the
#: evidence rather than hard-coded: a test pinned to a tag that leaves the
#: configuration starts skipping silently, which is failure class 12.
CANDIDATE = "qwen3:14b"

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_REFUSED = 2


def _recorded_digest(candidate: str) -> str:
    document = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    for entry in document.get("entries", []):
        if entry.get("configured_as") == candidate:
            return str(entry.get("artifact_digest") or "")
    return ""


@pytest.fixture(scope="module")
def recorded() -> str:
    digest = _recorded_digest(CANDIDATE)
    if not digest:
        pytest.fail(
            f"{CANDIDATE} has no artifact_digest in MODEL_LICENCE_EVIDENCE.json. "
            f"This suite is about the harness cross-checking that digest, so a "
            f"missing one must FAIL rather than skip — a suite that skips "
            f"itself green is exactly what it is here to prevent.")
    return digest


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Endpoint:
    """A real `/v1` server on a real socket, torn down deterministically."""

    def __init__(self, **flags) -> None:
        self.port = _free_port()
        argv = [sys.executable, str(ENDPOINT), "--port", str(self.port)]
        for key, value in flags.items():
            flag = "--" + key.replace("_", "-")
            if value is True:
                argv.append(flag)
            elif value not in (None, False, ""):
                argv.extend([flag, str(value)])
        self.process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        # Block on the socket being up rather than sleeping and hoping. A test
        # with no deadline does not fail, it wedges — this programme has been
        # bitten by that twice, once inside the batch written to hunt it.
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.25):
                    return
            except OSError:
                if self.process.poll() is not None:
                    raise RuntimeError("the self-test endpoint exited at startup")
                time.sleep(0.05)
        raise RuntimeError(f"the self-test endpoint never accepted on {self.port}")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)


def _evaluate(*args: str, timeout: float = 180.0) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("IRA_BENCHMARK_ENDPOINT", None)
    return subprocess.run(
        [sys.executable, str(EVALUATE), *args],
        capture_output=True, text=True, timeout=timeout, check=False, cwd=REPO,
        env=env)


# ── The positive direction, first ───────────────────────────────────────────


def test_the_recorded_digest_is_accepted(tmp_path, recorded):
    """A run whose endpoint offers the RECORDED digest produces a valid file.

    First rather than last on purpose. Everything below is a refusal, and a
    harness that only ever refuses proves nothing about its reasons.
    """
    endpoint = Endpoint(digest=recorded)
    out = tmp_path / "results.json"
    try:
        done = _evaluate("--candidate", CANDIDATE, "--endpoint", endpoint.url,
                         "--out", str(out), "--harness-verification",
                         "--repetitions", "1")
    finally:
        endpoint.close()

    assert done.returncode == EXIT_OK, done.stderr
    assert out.is_file(), "a complete run wrote no file"

    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["model"]["reference"] == recorded
    assert document["latency"]["time_to_first_token_ms_median"] > 0
    assert document["dataset"]["sha256"]
    assert document["environment"]["runtime"] == "ollama"

    sys.path.insert(0, str(REPO / "benchmarks"))
    from results_schema import validate
    assert validate(document) == [], "the harness wrote a file its own schema rejects"


def test_the_scores_block_is_empty_rather_than_invented(tmp_path, recorded):
    """Nothing graded was run, so nothing graded is recorded.

    An empty `scores` makes every candidate fail `selection.evaluate`, which is
    the CORRECT answer while no graded dataset exists. A harness that filled
    these in from a four-prompt latency set would produce a document that reads
    exactly like a real evaluation.
    """
    endpoint = Endpoint(digest=recorded)
    out = tmp_path / "results.json"
    try:
        _evaluate("--candidate", CANDIDATE, "--endpoint", endpoint.url,
                  "--out", str(out), "--harness-verification", "--repetitions", "1")
    finally:
        endpoint.close()

    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["scores"] == {}


def test_the_licence_block_records_a_reading_and_never_a_determination(tmp_path, recorded):
    endpoint = Endpoint(digest=recorded)
    out = tmp_path / "results.json"
    try:
        _evaluate("--candidate", CANDIDATE, "--endpoint", endpoint.url,
                  "--out", str(out), "--harness-verification", "--repetitions", "1")
    finally:
        endpoint.close()

    licence = json.loads(out.read_text(encoding="utf-8"))["licence"]
    assert licence["register_row_id"]
    assert licence["classification"]
    for forbidden in ("commercial_use", "permitted", "approved_by", "classified_by"):
        assert forbidden not in licence, (
            f"licence.{forbidden} was written. A benchmark records the "
            f"classification it READ; permission is L-05, an owner act.")


# ── The refusals, each for its own reason ───────────────────────────────────


def test_an_endpoint_with_no_immutable_reference_is_refused(tmp_path):
    """A tag is not an identity, so an endpoint that cannot name its weights loses."""
    endpoint = Endpoint()          # no --digest: /api/show 404s
    out = tmp_path / "results.json"
    try:
        done = _evaluate("--candidate", CANDIDATE, "--endpoint", endpoint.url,
                         "--out", str(out), "--harness-verification",
                         "--repetitions", "1")
    finally:
        endpoint.close()

    assert done.returncode == EXIT_REFUSED
    assert "could not produce an immutable reference" in done.stderr
    assert not out.exists(), "a refusal wrote a file"


def test_an_endpoint_may_not_assert_its_own_identity(tmp_path, recorded):
    """The digest is cross-checked against the recorded evidence.

    Found by driving this harness rather than by reading it: with `--digest`
    supplied, a self-test endpoint that is not a model produced a COMPLETE,
    schema-valid results.json. Every field present, every number real, about a
    process. The reference was read FROM the endpoint, so an endpoint that will
    say anything could say the right thing — P5-07's lesson one layer down.
    """
    wrong = "sha256:" + "b" * 64
    assert wrong != recorded
    endpoint = Endpoint(digest=wrong)
    out = tmp_path / "results.json"
    try:
        done = _evaluate("--candidate", CANDIDATE, "--endpoint", endpoint.url,
                         "--out", str(out), "--harness-verification",
                         "--repetitions", "1")
    finally:
        endpoint.close()

    assert done.returncode == EXIT_REFUSED
    assert "may not assert its own identity" in done.stderr
    assert recorded in done.stderr, "the refusal did not name the recorded digest"
    assert not out.exists()


def test_a_substituting_backend_reaches_the_providers_own_control(tmp_path, recorded):
    """The harness does not reimplement the substitution check; it REACHES it.

    `OpenAICompatibleProvider._identity` already raises when a backend answers
    as a different model. Carrying an `ApprovedModel` on the request is what
    puts that control on the benchmark path — without it the provider returns
    an empty identity and says nothing.
    """
    endpoint = Endpoint(digest=recorded, serves="a-completely-different-model")
    out = tmp_path / "results.json"
    try:
        done = _evaluate("--candidate", CANDIDATE, "--endpoint", endpoint.url,
                         "--out", str(out), "--harness-verification",
                         "--repetitions", "1")
    finally:
        endpoint.close()

    assert done.returncode == EXIT_REFUSED
    assert "UnsupportedApprovedModel" in done.stderr
    assert "a-completely-different-model" in done.stderr
    assert not out.exists()


def test_an_endpoint_that_will_not_name_its_runtime_is_refused(tmp_path, recorded):
    endpoint = Endpoint(digest=recorded, no_version=True)
    out = tmp_path / "results.json"
    try:
        done = _evaluate("--candidate", CANDIDATE, "--endpoint", endpoint.url,
                         "--out", str(out), "--harness-verification",
                         "--repetitions", "1")
    finally:
        endpoint.close()

    assert done.returncode == EXIT_REFUSED
    assert "environment.runtime" in done.stderr
    assert not out.exists()


def test_an_unknown_candidate_has_no_licence_reading_and_is_refused(tmp_path, recorded):
    endpoint = Endpoint(digest=recorded)
    out = tmp_path / "results.json"
    try:
        done = _evaluate("--candidate", "not-a-configured-model:1b",
                         "--endpoint", endpoint.url, "--out", str(out),
                         "--harness-verification", "--repetitions", "1")
    finally:
        endpoint.close()

    assert done.returncode == EXIT_REFUSED
    assert "has no row in MODEL_LICENCE_EVIDENCE.json" in done.stderr
    assert not out.exists()


def test_an_unreachable_endpoint_is_refused_and_writes_nothing(tmp_path):
    port = _free_port()             # bound, then released: nothing is listening
    out = tmp_path / "results.json"
    done = _evaluate("--candidate", CANDIDATE,
                     "--endpoint", f"http://127.0.0.1:{port}/v1",
                     "--out", str(out), "--harness-verification",
                     "--repetitions", "1", "--timeout", "5")
    assert done.returncode == EXIT_REFUSED
    assert "REFUSED" in done.stderr
    assert not out.exists()


# ── The structural guard ────────────────────────────────────────────────────


def test_a_harness_run_may_not_be_filed_as_a_candidate_evaluation(tmp_path, recorded):
    """Enforced in code, not left to whoever types the path.

    This is the whole reason `--harness-verification` exists. If it could write
    under `artifacts/benchmarks/`, the difference between *the harness works*
    and *this model was evaluated* would be a convention, and conventions are
    what `report_selection.py` would then generate a results table from.
    """
    out = REPO / "artifacts" / "benchmarks" / "9999-99-99" / "x" / "results.json"
    done = _evaluate("--candidate", CANDIDATE,
                     "--endpoint", "http://127.0.0.1:1/v1",
                     "--out", str(out.relative_to(REPO)),
                     "--harness-verification", "--repetitions", "1")
    assert done.returncode == EXIT_REFUSED
    assert "may not write under" in done.stderr
    assert not out.exists()
    # Refused BEFORE any request, so an unreachable endpoint cannot be what
    # produced this exit code.
    assert "REFUSED: --harness-verification" in done.stderr


def test_a_missing_endpoint_is_a_usage_error_not_a_refusal():
    """Distinct exit codes, because the two mean different things to a caller."""
    done = _evaluate("--candidate", CANDIDATE, "--out", "/tmp/unused.json")
    assert done.returncode == EXIT_USAGE
    assert "no --endpoint" in done.stderr


# ── Regressions found by running the thing ──────────────────────────────────


def test_the_refusal_printer_survives_an_out_of_repo_path(tmp_path):
    """FINDING AR — the diagnostic path was the one that could not run.

    `Path.relative_to` raises for a path outside the repository, and the first
    version called it inside the refusal printer. An `--out` under /tmp turned
    "here are the reasons this run is unusable" into a ValueError traceback with
    no reason printed and exit 1 instead of 2 — a caller reading the exit code
    would have concluded it had invoked the tool wrongly.

    ── This test's own first version did not reach the defect ───────────────

    It pointed at a closed port. That takes the EARLY return — the `except`
    around the drive — and never reaches the reasons printer at all, so it went
    green against the restored bug. Found by plant 5, which turned four other
    tests red and left this one passing: the plant fired, and the test written
    for it was the one test that could not see it. Failure class 18, in the
    regression test for the defect.

    So the endpoint here must be REACHABLE and the run UNUSABLE — a live server
    that cannot name its weights — which is the only combination that reaches
    the printer.
    """
    endpoint = Endpoint()          # live, but /api/show 404s: reasons, not an exception
    out = tmp_path / "nested" / "results.json"
    try:
        done = _evaluate("--candidate", CANDIDATE, "--endpoint", endpoint.url,
                         "--out", str(out), "--harness-verification",
                         "--repetitions", "1")
    finally:
        endpoint.close()

    assert done.returncode == EXIT_REFUSED
    assert "Traceback" not in done.stderr
    assert "ValueError" not in done.stderr
    # The reasons actually printed — the thing the crash swallowed.
    assert "could not produce an immutable reference" in done.stderr


def test_the_dataset_hash_is_of_the_file_bytes(tmp_path, recorded):
    """Two datasets differing only in whitespace must not hash the same.

    A hash computed after parsing would call them identical, and the whole point
    of recording it is that two runs over different prompts cannot be mistaken
    for a comparison.
    """
    import hashlib

    original = REPO / "benchmarks" / "datasets" / "latency_smoke.json"
    raw = original.read_bytes()
    reformatted = tmp_path / "reformatted.json"
    reformatted.write_bytes(
        json.dumps(json.loads(raw.decode("utf-8"))).encode("utf-8"))
    assert hashlib.sha256(raw).hexdigest() != hashlib.sha256(
        reformatted.read_bytes()).hexdigest()

    endpoint = Endpoint(digest=recorded)
    out = tmp_path / "results.json"
    try:
        _evaluate("--candidate", CANDIDATE, "--endpoint", endpoint.url,
                  "--out", str(out), "--harness-verification",
                  "--repetitions", "1", "--dataset", str(reformatted))
    finally:
        endpoint.close()

    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["dataset"]["sha256"] == hashlib.sha256(
        reformatted.read_bytes()).hexdigest()
    assert document["dataset"]["sha256"] != hashlib.sha256(raw).hexdigest()


# ── The units that are genuinely units ──────────────────────────────────────


def _module():
    sys.path.insert(0, str(REPO / "scripts" / "models"))
    import importlib
    return importlib.import_module("evaluate")


@pytest.mark.parametrize("reference", [
    "14b", "latest", "main", "qwen3:14b", "", "sha256:abc", "z" * 40,
])
def test_a_tag_is_never_an_immutable_reference(reference):
    assert not _module().IMMUTABLE_REFERENCE.match(reference)


@pytest.mark.parametrize("reference", [
    "sha256:" + "a" * 64, "0" * 40, "7b527af5ee8ed1f9a28d80b19703ed9bb8ba10ca",
])
def test_a_digest_or_a_commit_is_an_immutable_reference(reference):
    assert _module().IMMUTABLE_REFERENCE.match(reference)


def test_a_host_with_no_gpu_records_a_measurement_not_a_gap():
    """0.0 VRAM on a CPU-only host is an answer; None means the GPU would not say.

    Refusing a CPU-only run would refuse the owner's first honest evaluation for
    a reason that is not about them — and defaulting a GPU host's unreadable
    VRAM to 0 would claim a model fits in memory nobody measured. Both branches
    exist because they are different facts.
    """
    module = _module()
    import shutil as _shutil

    if _shutil.which("nvidia-smi"):
        pytest.skip("this host has a GPU; the cpu-only branch cannot be driven here")
    assert module._vram_gb() == 0.0
    assert module._gpu_and_driver() == ("none (cpu-only)", "none (cpu-only)")
