"""Phase 8 batch P8-06 — the adversarial truth pass over Phase 8's own claims.

The brief is to challenge this phase's work, not re-run it. For every Phase 8
claim: does the code exist, is it reachable, does anything call it, can the test
that covers it genuinely fail, and does the documentation match the tree.

── What it found ───────────────────────────────────────────────────────────

**1. There was no `ModelProvider` implementation at all.** Three closure reports
said "nothing in the product constructs an `IRAModelGateway`" and every one of
them read as an oversight in the wiring. It was not: `core/interfaces/
model_provider.py` defined the protocol, `core/models/gateway.py` consumed it,
and the only implementations in the tree were test fixtures. Nothing could have
constructed a gateway, because there was nothing to put in it.

**2. Two route modules had never been on the governed path at all.** Every
Phase 5 and 6 guard was written about `utils/llm.py`, so
`api/routes/multimodal.py` and `api/routes/video_gen.py` — which each built
their own `AsyncOpenAI` against the vision endpoint and streamed user images to
it — were invisible to all of them. The gap those phases recorded was slightly
wider than they could have known.

**3. `model_usage.outcome` rejected two values the gateway writes.** `029`
closed the vocabulary to four; `gateway.complete()` has written
`refused_policy` since Phase 5, and `record_usage` is fail-soft, so every one of
those rows was dropped with a log line. The registry refusing an unapproved
model — the single most important thing the usage table could record — had never
been recorded. Fixed in `075`.

**4. Metering an abandoned stream depended on garbage collection.** The
gateway meters in a `finally`; the consumer dropped the inner generator rather
than closing it, so the `finally` ran whenever CPython collected it. A test
caught it as "no usage row"; in production it would have been metering that
sometimes happens.
"""
from __future__ import annotations

import ast
import inspect
import io
import pathlib
import re
import tokenize

IRA_ROOT = pathlib.Path(__file__).resolve().parents[2]
REPO = IRA_ROOT.parents[1]

REPORT = REPO / "docs" / "cloud" / "evidence" / "PHASE_8_CLOSURE_REPORT.md"
LEDGER = REPO / "docs" / "cloud" / "evidence" / "PHASE_8_LEDGER.md"


def _executable_lines(source: str) -> list[str]:
    """`source` with comments and docstrings blanked, line numbers preserved.

    Copied in shape from `test_p6_truth_pass._executable_lines` for the reason
    that file records: this module's own prose names the things it forbids, and
    a guard that fires on its own documentation gets deleted.
    """
    lines = source.splitlines()
    blanked = list(lines)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT and tok.start[0] == tok.end[0]:
                row, col = tok.start
                if row - 1 < len(blanked):
                    blanked[row - 1] = blanked[row - 1][:col]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return lines
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return blanked
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            for row in range(first.lineno, (first.end_lineno or first.lineno) + 1):
                if row - 1 < len(blanked):
                    blanked[row - 1] = ""
    return blanked


# ─────────────────────────────────────────────────────────────────────────────
# 1. The chokepoint is a chokepoint
# ─────────────────────────────────────────────────────────────────────────────


def test_a_provider_implementation_exists_and_is_not_a_test_fixture():
    """Claim: the gateway can be constructed. It could not be before P8-01,
    and the reason was not wiring — there was nothing to wire."""
    impl = IRA_ROOT / "core" / "models" / "providers" / "openai_compatible.py"
    assert impl.is_file(), (
        "there is no ModelProvider implementation outside the tests again, so "
        "nothing can construct a gateway and every Phase 5/6/7 guarantee "
        "describes a path nothing takes")
    from core.interfaces.model_provider import Locality, ModelProvider
    from core.models.providers import OpenAICompatibleProvider

    built = OpenAICompatibleProvider(
        provider_id="p", display_name="p", locality=Locality.LOCAL,
        base_url="http://127.0.0.1/v1", api_key="", model_for_tier={},
        retains_prompts=False, client_factory=lambda *a: None)
    assert isinstance(built, ModelProvider), (
        "OpenAICompatibleProvider no longer satisfies the ModelProvider "
        "protocol, so the gateway would reject it at the type boundary and "
        "there would again be nothing to construct a gateway around")


def test_only_the_provider_module_speaks_to_an_inference_endpoint():
    """Claim: the gateway is the only thing that calls a provider.

    This is the check that found `multimodal.py` and `video_gen.py`. It reads
    executable lines only — this file's own prose names the call it forbids.

    `utils/llm.py` is exempt because it holds the bounded fallback for a
    deployment with no provider configured, and `test_the_fallback_is_only_a_
    fallback` below is what bounds it.
    """
    allowed = {"openai_compatible.py", "llm.py"}
    offenders = []
    for path in sorted(IRA_ROOT.rglob("*.py")):
        s = str(path)
        if "__pycache__" in s or "/tests/" in s or "/scripts/" in s:
            continue
        if path.name in allowed:
            continue
        for line in _executable_lines(path.read_text(encoding="utf-8",
                                                     errors="replace")):
            if "chat.completions.create(" in line:
                offenders.append(str(path.relative_to(IRA_ROOT)))
                break
    assert offenders == [], (
        f"{offenders} call an inference endpoint directly. Every one bypasses "
        f"the privacy policy, the quota, the registry, the licence gate and "
        f"the usage record — and would be invisible to every guard written "
        f"about utils/llm.py, which is exactly how two of them survived three "
        f"phases.")


def test_the_fallback_is_only_a_fallback():
    """Claim: the direct client path is reachable only when no provider is
    configured.

    Asserted on the source rather than behaviourally, because the failure this
    guards against is a SECOND unconditional path — and a behavioural test
    would happily pass while one existed, since the governed path would still
    work.
    """
    src = inspect.getsource(__import__("utils.llm", fromlist=["x"]))
    for fn in ("_complete_via_gateway", "_stream_via_gateway"):
        body = src[src.index(f"async def {fn}("):]
        body = body[:body.index("\n\nasync def ") if "\n\nasync def " in body
                    else len(body)]
        assert "if gw is None:" in body, (
            f"{fn} no longer guards its direct call with 'gw is None'. The "
            f"fallback has become an alternative path rather than a last "
            f"resort, which is the gap Phase 8 closed, reopened.")


def test_there_is_no_switch_that_turns_the_gateway_off():
    """Claim: the route is single; only the depth of checking varies.

    Phase 5's `registry_required` defaults to False and that is deliberate. A
    flag that bypassed the GATEWAY would be a different thing, and it is how
    the gap this phase closed was created in the first place.
    """
    src = (IRA_ROOT / "utils" / "llm.py").read_text(encoding="utf-8")
    for banned in ("use_gateway", "gateway_enabled", "ira_use_gateway",
                   "bypass_gateway", "disable_gateway"):
        for line in _executable_lines(src):
            assert banned not in line, (
                f"utils/llm.py reads {banned!r}. A deployment that can switch "
                f"the chokepoint off has no chokepoint.")


# ─────────────────────────────────────────────────────────────────────────────
# 2. What Phase 8 does NOT claim
# ─────────────────────────────────────────────────────────────────────────────


def test_no_external_provider_is_implemented():
    """`029`'s catalogue can describe a third party. Nothing implements one.

    Recorded as an assertion because the natural misreading of "the gateway is
    now on the live path" is "IRA can now route to external providers", and it
    cannot. The day somebody builds one, this fails and the closure report has
    to be corrected in the same change — the shape P5-08 and P6 used.
    """
    providers = IRA_ROOT / "core" / "models" / "providers"
    modules = sorted(p.name for p in providers.glob("*.py")
                     if p.name != "__init__.py")
    assert modules == ["openai_compatible.py"], (
        f"a new provider module exists: {modules}. If it can reach a third "
        f"party, PHASE_8_CLOSURE_REPORT.md — which states that no external "
        f"provider is implemented — is now wrong.")


def test_streams_record_no_token_counts():
    """An OpenAI-compatible stream carries no usage block. The honest options
    are zero or an estimate, and an estimate becomes a number in a bill."""
    from core.models.gateway import IRAModelGateway

    src = inspect.getsource(IRAModelGateway._metered_stream)
    assert "prompt_tokens" not in src and "completion_tokens" not in src, (
        "the streaming meter now records token counts. If they are measured, "
        "say where from; if they are estimated, PHASE_8_CLOSURE_REPORT.md must "
        "stop saying streams report no tokens.")



# ─────────────────────────────────────────────────────────────────────────────
# 3. The pre-existing defect this phase surfaced
# ─────────────────────────────────────────────────────────────────────────────


def test_every_outcome_the_gateway_writes_is_representable():
    """The defect that had been silently dropping rows since Phase 5.

    Read from the migrations rather than from a database, so it fails in review
    on a tree where the code writes a value the schema will reject — which is
    the state that produced it.
    """
    from core.models.gateway import IRAModelGateway

    written = set()
    for method in (IRAModelGateway.complete, IRAModelGateway._admit,
                   IRAModelGateway._metered_stream):
        for match in re.finditer(r'outcome\s*=\s*"([a-z_]+)"',
                                 inspect.getsource(method)):
            written.add(match.group(1))
    # `_metered_stream` assigns its outcome to a variable first.
    written |= set(re.findall(r'outcome = "([a-z_]+)"',
                              inspect.getsource(IRAModelGateway._metered_stream)))

    migrations = REPO / "supracloud-jarvis" / "postgres"
    vocabulary: set[str] = set()
    for path in sorted(migrations.glob("*.sql")):
        text = path.read_text(encoding="utf-8", errors="replace")
        # SQL comments first. `075`'s CHECK carries an explanation containing
        # `gateway.complete()`, and a naive `[^)]*` stops at that bracket — it
        # read a truncated vocabulary and reported values as missing that were
        # right there. Found by this test failing on a tree where 075 was
        # already applied.
        text = re.sub(r"--[^\n]*", "", text)
        # Only `model_usage`'s constraint, and this scoping is load-bearing.
        #
        # `outcome` is a common column name: 010, 028, 029, 070, 075 and 080 all
        # constrain one. The vocabulary is ASSIGNED rather than unioned, so that
        # a later migration WIDENING the set replaces the earlier one rather
        # than both being accepted forever — that part is right. But it was
        # applied to the last match in the whole DIRECTORY, which meant the
        # answer depended on 075 happening to sort last.
        #
        # It stopped being last when `080` added `payment_events.outcome`, and
        # the test then reported EVERY gateway outcome as missing — a check
        # failing for a reason that had nothing to do with what it tests. So the
        # statement must be attributed to its table rather than assumed.
        for statement in text.split(";"):
            if "model_usage" not in statement:
                continue
            for match in re.finditer(r"outcome IN \(([^)]*)\)", statement, re.S):
                found = set(re.findall(r"'([a-z_]+)'", match.group(1)))
                if found:
                    vocabulary = found

    missing = sorted(written - vocabulary)
    assert not missing, (
        f"the gateway writes usage outcomes the schema rejects: {missing}. "
        f"record_usage is fail-soft, so those rows are dropped with a log line "
        f"and nothing fails — which is how `refused_policy` went unrecorded "
        f"from Phase 5 until 075.")


def test_an_abandoned_stream_is_metered_without_waiting_for_the_collector():
    """Claim: abandonment is metered deterministically.

    Asserted on the consumer rather than behaviourally, because the
    behavioural version passes on a tree where the `finally` runs at collection
    time — it just happens to run before the assertion.
    """
    src = inspect.getsource(__import__("utils.llm", fromlist=["x"]).stream_tokens)
    assert "aclose" in src, (
        "stream_tokens no longer closes the gateway's stream. The gateway "
        "meters in a finally, so metering has gone back to depending on when "
        "CPython collects a generator.")


def test_the_disclosure_catalogue_is_not_writable_by_the_runtime_role():
    """Claim: the process that answers requests cannot edit the record of where
    it sent them.

    `api/routes/models.py` states this and enforces it by having no endpoint.
    That is a control that holds because of what has not been written yet —
    `067` calls the same shape "a gate, not an answer" — and the database
    disagreed with it: `029` predates `067`/`068`, which each revoked
    deliberately, so `model_providers` kept the DML that `042`'s default
    privileges hand out.

    Asserted here on the migration rather than on a database, so it fails in
    review on a tree where the revoke has been removed. The privilege itself is
    checked against a real chain by the `076` CI step and by
    `scripts/staging/p8_acceptance.sh`.
    """
    migrations = REPO / "supracloud-jarvis" / "postgres"
    revoke = next((p for p in migrations.glob("076_*.sql")), None)
    assert revoke is not None, (
        "076 is gone. Without it `ira_app` can INSERT, UPDATE and DELETE "
        "`model_providers`, so the process that answers requests can rewrite "
        "whether a provider retains prompts.")
    body = revoke.read_text(encoding="utf-8")
    for role in ("ira_app", "ira_worker"):
        assert re.search(rf"REVOKE[^;]*ON model_providers FROM {role}", body), (
            f"076 no longer revokes write access to model_providers from "
            f"{role}")
    assert re.search(r"GRANT SELECT ON model_providers TO ira_app", body), (
        "076 no longer grants the READ. Revoking the read would take the "
        "Privacy Centre offline in order to protect it, which is a worse "
        "outcome than the defect.")


def test_the_disclosure_surface_shows_the_provider_that_actually_serves():
    """Claim: `/models/providers` answers "where do my requests go?".

    It answered from the catalogue alone, which was fine while nothing routed
    anywhere. `029` seeds `ollama-local` with `enabled_in_cloud = FALSE`, so a
    cloud deployment serving from a local Ollama would have shown a provider
    list that did not contain the provider answering every one of its requests.

    The seed was NOT edited — whether cloud may serve from a local Ollama is a
    product decision, and flipping a flag would make the catalogue agree with
    the code by changing what the catalogue claims.
    """
    route = (IRA_ROOT / "api" / "routes" / "models.py").read_text(encoding="utf-8")
    assert "_live_disclosures" in route, (
        "the providers endpoint no longer merges the gateway's live providers, "
        "so a deployment can serve from a provider its own Privacy Centre does "
        "not list")
    assert "serves_requests" in route, (
        "the providers endpoint no longer marks which providers actually serve")
