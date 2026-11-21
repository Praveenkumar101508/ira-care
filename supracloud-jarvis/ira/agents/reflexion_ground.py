"""
IRA Reflexion — grounded verifiers.

This is the point of the whole feature: where a REAL verifier exists, the
critic's score comes from the verifier, not from one model grading another.

  • code tasks    → write the draft + the caller-supplied test into a sandboxed
                    temporary workdir and run pytest through the executor's hardened
                    runner. The pass/fail + captured errors ARE the score.
  • factual tasks → check the drafted claim against pgvector memory. The answer
                    only passes when it is supported by retrieved facts.

A verifier returns a grounded Critique, or None when it cannot apply to this
task (empty draft, no test supplied, nothing in memory) — in which case
run_reflexion falls back to the LLM critic. A draft that merely *claims* success
(including an injection payload telling the judge to "PASS") cannot flip a
grounded verdict: the verifier never asks a model whether it passed.

Importing this module installs grounded_verifier() into reflexion via
set_grounded_verifier(); run_reflexion imports it lazily on first use.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile

from agents.reflexion import (
    Critique,
    ReflexionState,
    set_grounded_verifier,
)
from memory.namespaces import VAULT_NAMESPACE

logger = logging.getLogger("ira.reflexion.ground")


def _strip_code_fences(text: str) -> str:
    """Remove a single leading/trailing Markdown code fence if present."""
    t = (text or "").strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


# ── Code grounding: run the tests, the result is the score ─────────────────────

async def _code_verifier(state: ReflexionState) -> Critique | None:
    """Execute the draft against the caller-supplied pytest, in the executor sandbox.

    Requires state["verifier_test"] (pytest source that imports `solution`). Without
    it we cannot ground a code task — return None to fall back to the LLM critic.
    """
    test_src = (state.get("verifier_test") or "").strip()
    draft = _strip_code_fences(state.get("draft", ""))
    if not test_src or not draft:
        return None

    # Hardened, allowlisted runner (30s timeout and restricted PATH/env).
    from agents.executor import _run_command, _is_allowed

    # Let tempfile select the platform-native secure temporary root. Hard-coding
    # /tmp breaks Windows and can bypass an operator's configured TMPDIR.
    workdir = tempfile.mkdtemp(prefix="reflexion_")
    try:
        with open(os.path.join(workdir, "solution.py"), "w", encoding="utf-8") as f:
            f.write(draft + "\n")
        with open(os.path.join(workdir, "test_solution.py"), "w", encoding="utf-8") as f:
            f.write(test_src + "\n")
        cmd = f"pytest -q {workdir}"
        if not _is_allowed(cmd):       # defence-in-depth: must stay on the allowlist
            return None
        output, returncode = await _run_command(cmd)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    passed = returncode == 0
    return Critique(
        score=1.0 if passed else 0.0,
        passed=passed,
        issues=[] if passed else [f"tests failed (exit {returncode})", output[:600]],
        suggestion="" if passed else "fix the code so the failing tests pass",
        grounded_by="tests",
    )


# ── Factual grounding: the answer must be supported by memory ──────────────────

async def _factual_verifier(state: ReflexionState) -> Critique | None:
    """Score a factual draft by how well pgvector memory supports it.

    retrieve() already applies the cosine floor, so an empty result means there is
    nothing in memory to verify against → return None (fall back to the LLM critic).
    """
    claim = (state.get("draft") or "").strip()
    if not claim:
        return None

    from memory.store import retrieve
    from config import get_settings

    # No principal means no workspace to bind, so there is nothing to ground
    # against. Returning None hands the turn to the LLM critic, which is a
    # weaker check and is reported as such (`grounded_by="llm"`) rather than
    # being passed off as memory grounding. Retrieving unscoped to have
    # something to say would be the alternative, and it is the wrong one.
    principal = state.get("principal")
    if principal is None:
        logger.debug("reflexion factual grounding skipped: no verified workspace")
        return None

    try:
        memories = await retrieve(
            principal, claim, user_id=state.get("user_id", VAULT_NAMESPACE)
        )
    except Exception as exc:  # noqa: BLE001 — fail soft to the LLM critic
        logger.debug("reflexion factual grounding unavailable: %s", exc)
        return None
    if not memories:
        return None

    best = max(float(m["similarity"]) for m in memories)
    threshold = state.get("pass_threshold", get_settings().reflexion_pass_threshold)
    passed = best >= threshold
    return Critique(
        score=best,
        passed=passed,
        issues=[] if passed else [f"claim weakly supported by memory (best similarity {best:.2f})"],
        suggestion="" if passed else "ground the answer in retrieved facts or state the uncertainty",
        grounded_by="memory",
    )


# ── Dispatcher (stateless: safe to share as the single installed verifier) ─────

async def grounded_verifier(state: ReflexionState) -> Critique | None:
    kind = (state.get("task_kind") or "general").lower()
    if kind == "code":
        return await _code_verifier(state)
    if kind == "factual":
        return await _factual_verifier(state)
    return None


# Install on import so run_reflexion picks it up.
set_grounded_verifier(grounded_verifier)
