"""
Chat endpoints — the core IRA interface.

POST /chat          → standard JSON response; 504 "model_timeout" past IRA_CHAT_TIMEOUT_S
POST /chat/stream   → Server-Sent Events (token-by-token)
GET  /chat/history  → conversation history

Biometric gate: every request carries an `is_owner` flag. The LangGraph
biometric_gate node uses this to allow or block restricted domain access.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from api.middleware.auth import dev_mode_active, is_owner, is_voice_service, require_auth
from api.middleware.workspace import require_workspace_principal
from core.identity.principal import Principal
from core.identity.namespacing import redis_workspace_key
from core.runtime.errors import MODEL_TIMEOUT, IRAError
from core.models import served as served_models
from core.models.served import NOT_REPORTED
from agents.graph import run_graph
from agents.security import answer_lockdown_command, short_refusal, SHORT_REFUSAL_MODEL
from utils.action_claims import TOOL_AGENTS, append_no_action_note, with_no_action_claim_rule
from utils.answer_length import with_brief_answer_rule
from utils.prompt_safety import frame_user_message, strip_pasted_material, with_quoted_material_rule
from memory.chat_commands import NEEDS_OWNER, guard_chat_memory, memory_reply, memory_save_content
from memory.store import ensure_conversation, get_recent_messages, retrieve
from owner_profile import get_profile_summary
from utils.llm import stream_tokens, should_use_deep, should_use_reasoning
from utils.prompt_safety import wrap_external_content
from utils.redis_client import cache_get, cache_set, get_redis
from config import get_settings


async def _workspace_username(
    principal: Principal = Depends(require_workspace_principal),
) -> str:
    """The account name, derived from the verified principal.

    `user_id` on `memory_embeddings` is a free-form namespace string, not an
    identity — it separates one person's notes from the shared corpus WITHIN a
    workspace, and the workspace is what separates customers. Taking it from
    the verified principal rather than from a second `Depends(require_auth)`
    means the namespace and the bound workspace cannot come from two different
    resolutions of who the caller is.

    FastAPI caches dependencies per request, so asking for the principal here
    and in the handler resolves it once.
    """
    return principal.username or str(principal.user_id)


router = APIRouter(prefix="/chat", tags=["chat"])

logger = logging.getLogger("ira.chat")

#: Strong references to the turn-save tasks the stream routes start after the
#: done frame, so the event loop cannot drop one before it finishes (as
#: `memory.store._background_tasks` does for the embedding writes).
_turn_save_tasks: set[asyncio.Task] = set()


async def _save_turn(
    principal: Principal,
    conv_id: str,
    user_text: str,
    assistant_text: str,
    *,
    user_id: str,
    model_used: str | None = None,
    latency_ms: int | None = None,
) -> None:
    """Save the user row, then the assistant row, in that order.

    `get_recent_messages` replays history `ORDER BY created_at`, and
    `messages.created_at` defaults to `NOW()` at insert. The stream routes used
    to start the two saves as concurrent tasks, so whichever insert ran first
    got the earlier timestamp: in 14 of 25 replayed follow-up turns of the
    November acceptance run the assistant's answer came back before the user's
    question (roles `sssauu` instead of `sssuau`). Awaiting the user row before
    the assistant row gives the rows the order the conversation had. A failed
    user save is logged and the assistant save still runs, as before.
    """
    from memory.store import save_message

    try:
        await save_message(principal, conv_id, "user", user_text, user_id=user_id)
    except Exception as exc:
        logger.warning(f"save_message failed: {type(exc).__name__}")
    try:
        await save_message(
            principal, conv_id, "assistant", assistant_text,
            model_used=model_used, latency_ms=latency_ms, user_id=user_id,
        )
    except Exception as exc:
        logger.warning(f"save_message failed: {type(exc).__name__}")


def _save_turn_in_background(
    principal: Principal,
    conv_id: str,
    user_text: str,
    assistant_text: str,
    *,
    user_id: str,
    model_used: str | None = None,
    latency_ms: int | None = None,
) -> asyncio.Task:
    """Start `_save_turn` after the done frame has been yielded: the stream's
    bytes are unaffected, and the two rows still land user-first."""
    task = asyncio.create_task(_save_turn(
        principal, conv_id, user_text, assistant_text,
        user_id=user_id, model_used=model_used, latency_ms=latency_ms,
    ))
    _turn_save_tasks.add(task)
    task.add_done_callback(_turn_save_tasks.discard)
    return task


# ── Engine selection (cutover — Phase 7.2) ─────────────────────────────────────
# IRA_USE_CORTEX routes chat — and voice, which calls /chat/stream over HTTP — through
# the Cortex bridge skills instead of the legacy LangGraph agents. DEFAULT OFF: when off,
# behaviour is byte-identical to before. Read once at startup. The legacy path
# (agents/graph.py, supervisor.py, state.py) stays in place as the instant rollback.
_USE_CORTEX = os.getenv("IRA_USE_CORTEX", "false").strip().lower() in ("1", "true", "yes", "on")

# Classifier outputs that have a matching ira/skills/<name>/ persona (others → conversational).
_VALID_SKILLS = frozenset({
    "conversational", "researcher", "security", "executor",
    "creator", "website", "tutor", "career", "digital",
})


# ── Request / Response models ──────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=32_000)
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    stream: bool = False
    is_voice_owner: bool = False   # Set True by voice agent after biometric verification
    is_voice: bool = False         # Set True by voice pipeline for concise reply routing
    mode: str = "assistant"        # "assistant" | "tutor"
    image_b64: str | None = Field(None, description="Base64-encoded image for vision queries")
    mime_type: str = Field("image/jpeg")
    grok_mode: bool = False        # Use Grok personality + auto search + image gen tools
    engineer_mode: bool = False    # the assistant-style 4-step engineering workflow (analysis→plan→diffs→verify)
    think_mode: bool = False       # Show step-by-step reasoning in collapsible panel before answer
    deep_search: bool = False      # Multi-step iterative web search (3 rounds) like Grok DeepSearch


class ChatResponse(BaseModel):
    # `model_used` collides with pydantic's protected "model_" namespace; opting out
    # of the namespace check silences the UserWarning without renaming the field.
    model_config = ConfigDict(protected_namespaces=())

    response: str
    session_id: str
    conversation_id: str
    agent_used: str
    # The model that answered, as its provider named it (`qwen3:4b`); "not
    # reported" when no provider named one; or the step that answered without
    # a model (`deterministic`, `security_gate`). Never a tier name: that is
    # `model_tier` (finding P1-05).
    model_used: str
    latency_ms: int
    memory_decision: dict | None = None
    # Confidence signalling (reasoning/confidence.py): {level, reason,
    # support_score, expects_grounding, hedge}. None when the layer is disabled
    # or the path never retrieved anything to grade.
    confidence: dict | None = None
    # The tier IRA asked the answering model for ("fast", "deep", "reasoning");
    # None when no model was reported.
    model_tier: str | None = None


class HistoryMessage(BaseModel):
    role: str
    content: str


class HistoryResponse(BaseModel):
    session_id: str
    conversation_id: str
    messages: list[HistoryMessage]


#: The tier names the agents write into `model_used` (agents/*.py). They say
#: which tier was asked for, not which model answered.
_TIER_LABELS = frozenset({"qwen3-fast", "qwen3-deep", "qwen3-reasoning"})


def _answered_by(state) -> str:
    """`model_used` for a turn the agent graph ran (finding P1-05).

    The model its provider named, which `run_graph` records. Without one, the
    name of a step that answered without a model, such as `security_gate`. A
    tier name is never reported as the model: on 31 October 2026 the readout
    said `qwen3-deep` while Ollama served `qwen3:4b`.
    """
    served = state.get("served_model")
    if served:
        return str(served)
    label = str(state.get("model_used") or "")
    if label and label not in _TIER_LABELS:
        return label
    return NOT_REPORTED


#: Owner-ness is decided in one place. `is_owner` refuses in cloud, where a
#: username is per-tenant and names one tenant's user, not the deployment's
#: owner. This flag is passed to the agent layer as `is_owner=`, which is what
#: decides which tools the model may execute — so a name collision granting it
#: would be a tool-authority escalation, not just a data read. See 3C-04.
_is_owner = is_owner


#: Whether the request genuinely comes from the voice service — the only
#: identity permitted to assert `is_voice_owner` from the request body. Decided
#: in one place, and False in cloud: a deployment-wide name compared against a
#: per-tenant username would let a tenant's user claim owner authority through
#: the body, walking around the `is_owner` fix entirely. See 3C-08.
_is_voice_service = is_voice_service


def _turn_acted(state) -> bool:
    """True when this graph turn may hold a real tool result or an approval step:
    a tool-running agent answered, or a deterministic gate did (its text is IRA's
    own, not a model's claim). Only then is a completion claim left unannotated."""
    label = str(state.get("model_used") or "")
    return state.get("active_agent") in TOOL_AGENTS or label.endswith("_gate") or label == "deterministic"


def _stable_hash(text: str) -> str:
    """SHA-256-based stable hash for cache keys (not Python's randomised hash)."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _compose_quality_layer(
    prompt: str,
    message: str,
    *,
    think_mode: bool = False,
    deep_search: bool = False,
) -> str:
    """Append model-tier voice + task-policy guidance to a system prompt.

    Fail-soft and flag-gated: any error, or ``IRA_USE_MODEL_ROUTER=false``,
    returns ``prompt`` untouched, so existing behaviour never breaks. Uses
    the same task classifier the model router uses to pick a tier
    (``reasoning.model_router.classify_task``) so the tier voice matches
    whichever local model actually answers.

    The tier and the answer policy are chosen on the user's own words, with
    any pasted material stripped (``IRA_TIER_CLASSIFY_OWN_WORDS``, default
    on): a pasted document is always over the router's 120-word line, so a
    one-sentence question above it used to reach the reasoning tier and
    answer with a reasoning trail nobody asked for. The toggles (think mode,
    deep search) still force reasoning, and a long question with nothing
    pasted still counts as long.
    """
    cfg = get_settings()
    if not getattr(cfg, "ira_use_model_router", True):
        return prompt
    try:
        from reasoning.model_router import classify_task
        from reasoning.model_system_prompts import system_prompt_for
        from reasoning.answer_policy import policy_for_prompt

        words = message
        if getattr(cfg, "ira_tier_classify_own_words", True):
            own = strip_pasted_material(message)
            # A paste with no words around it keeps the old classification.
            words = own if own.strip() else message
        cls = classify_task(words, think_mode=think_mode, deep_search=deep_search)
        tier_prompt = system_prompt_for(cls.mode)
        policy = policy_for_prompt(words)
        return (
            f"{prompt}\n\n## Model-tier guidance\n{tier_prompt}"
            f"\n\n## Response policy ({policy.task_type})\n{policy.instructions}"
        )
    except Exception:  # noqa: BLE001 — the answer-quality layer must never break chat
        return prompt


def _grade_confidence(message: str, state: dict) -> tuple[str, dict | None]:
    """Attach a confidence level to a finished answer and shape it accordingly.

    Fail-soft and flag-gated (``CONFIDENCE_ENABLED``): any error returns the
    answer untouched with no confidence field, so chat can never break because
    of this layer. Derived from the retrieval scores the memory node kept plus
    the existing rule-based verifier — no extra model call.
    """
    answer = state.get("final_response", "")
    cfg = get_settings()
    if not getattr(cfg, "confidence_enabled", True):
        return answer, None
    try:
        from reasoning.answer_policy import classify_task_type, get_policy
        from reasoning.answer_verifier import verify_answer
        from reasoning.confidence import assess_and_shape

        task_type = classify_task_type(message)
        verification = verify_answer(
            message, answer, task_type=task_type, policy=get_policy(task_type),
        )
        shaped, assessment = assess_and_shape(
            answer,
            task_type=task_type,
            retrieval=state.get("retrieval_support"),
            verification=verification,
        )
        return shaped, assessment.as_dict()
    except Exception:  # noqa: BLE001 — confidence must never break chat
        return answer, None


def _confidence_payload(message: str, answer: str, memories: list | None) -> dict | None:
    """Confidence for an already-streamed answer — badge data only.

    A streamed answer cannot be rewritten after the fact, so the hedge travels
    as data in the ``done`` event and the UI renders it next to the badge. Same
    signals, same thresholds, still no extra model call. Fail-soft: None.
    """
    cfg = get_settings()
    if not getattr(cfg, "confidence_enabled", True):
        return None
    try:
        from reasoning.answer_policy import classify_task_type, get_policy
        from reasoning.answer_verifier import verify_answer
        from reasoning.confidence import derive_confidence

        task_type = classify_task_type(message)
        verification = verify_answer(
            message, answer, task_type=task_type, policy=get_policy(task_type),
        )
        return derive_confidence(
            task_type=task_type,
            retrieval=memories,
            verification=verification,
        ).as_dict()
    except Exception:  # noqa: BLE001
        return None


# ── Expert Mode rate limiting ─────────────────────────────────────────────────

_EXPERT_RATE_LIMIT = 20    # max Expert Mode calls per user per hour (personal AI — raised from 3)
_EXPERT_RATE_WINDOW = 3600  # sliding window in seconds

#: The limit applied when the shared backend cannot be reached. Deliberately
#: much lower than `_EXPERT_RATE_LIMIT`, because a per-PROCESS limiter admits up
#: to this many calls *per replica*: with an unknown replica count the only
#: honest way to keep the fleet-wide total near the real limit is to make the
#: per-process share small. It is a floor on damage, not an equivalent control.
_EXPERT_DEGRADED_LIMIT = 3

#: Cap on how many users the degraded limiter will track in this process. An
#: unbounded map keyed by an attacker-influenced string is a memory-exhaustion
#: hole, and reaching for one during a Redis outage would trade a rate-limit
#: failure for an availability failure.
_DEGRADED_MAX_TRACKED = 10_000

#: username → monotonic timestamps of admitted calls inside the window.
_degraded_calls: dict[str, list[float]] = {}


class RateLimitState(str, Enum):
    """Which control produced the decision below.

    P14-01b, finding V. `(True, _EXPERT_RATE_LIMIT)` — allowed, full quota — is
    byte-identical to a user's genuine first call of the window, so "the limit
    was checked and you are well within it" and "the limit is not being enforced
    at all right now" were the same event to every caller and every log reader.
    A boolean cannot carry the difference; this does.
    """

    #: The shared Redis counter answered. The real limit was applied.
    ENFORCED = "enforced"
    #: Redis was unreachable and a conservative PER-PROCESS limiter answered.
    #: Weaker than ENFORCED and stated as such — never to be read as "enforced".
    DEGRADED = "degraded"


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    #: The limit that was actually applied, which is NOT `_EXPERT_RATE_LIMIT`
    #: when degraded.
    limit: int
    state: RateLimitState

    @property
    def enforced(self) -> bool:
        return self.state is RateLimitState.ENFORCED


def _check_degraded_rate_limit(username: str, *, now: float) -> RateLimitDecision:
    """The fallback control: a conservative sliding window in this process.

    The owner's Phase 14 instruction:

        An unavailable rate-limit backend must not silently become unlimited.
        Use a conservative local/degraded limiter, or explicitly degrade/refuse.

    So this is a real limiter, not a placeholder — it counts, it refuses, and it
    reports `DEGRADED` even while allowing, so nothing downstream can mistake it
    for the shared control. Its weaknesses are inherent and deliberately not
    papered over: it is per-process, so it resets on deploy and multiplies by
    replica count, which is why its limit is a small fraction of the real one.
    """
    cutoff = now - _EXPERT_RATE_WINDOW
    calls = [t for t in _degraded_calls.get(username, ()) if t > cutoff]

    if username not in _degraded_calls and len(_degraded_calls) >= _DEGRADED_MAX_TRACKED:
        # Prune first — most entries are usually expired — and only refuse if
        # the table is genuinely full of live windows.
        for name in [n for n, ts in _degraded_calls.items()
                     if not any(t > cutoff for t in ts)]:
            del _degraded_calls[name]
        if len(_degraded_calls) >= _DEGRADED_MAX_TRACKED:
            logger.error(
                "degraded expert-mode limiter is tracking %d users and cannot "
                "take another; REFUSING %r rather than admitting an uncounted "
                "call", len(_degraded_calls), username)
            return RateLimitDecision(allowed=False, remaining=0,
                                     limit=_EXPERT_DEGRADED_LIMIT,
                                     state=RateLimitState.DEGRADED)

    if len(calls) >= _EXPERT_DEGRADED_LIMIT:
        _degraded_calls[username] = calls
        return RateLimitDecision(allowed=False, remaining=0,
                                 limit=_EXPERT_DEGRADED_LIMIT,
                                 state=RateLimitState.DEGRADED)

    calls.append(now)
    _degraded_calls[username] = calls
    return RateLimitDecision(allowed=True,
                             remaining=max(0, _EXPERT_DEGRADED_LIMIT - len(calls)),
                             limit=_EXPERT_DEGRADED_LIMIT,
                             state=RateLimitState.DEGRADED)


async def _check_expert_rate_limit(username: str) -> RateLimitDecision:
    """
    Increment the per-user Expert Mode counter and check against the limit.

    Fix #46: INCR and EXPIRE are sent in a single pipeline so the TTL is
    always set, even if two concurrent first-calls race or the process
    crashes between the two commands. Without the pipeline the key could
    become immortal, permanently locking a user after _EXPERT_RATE_LIMIT sessions.

    P14-01b, finding V. This no longer fails open. When Redis is unreachable the
    decision comes from `_check_degraded_rate_limit` — a real, conservative,
    per-process limiter — and says `DEGRADED` so that a weaker control is never
    mistaken for the shared one.
    """
    key = f"expert_rate:{username}"
    try:
        pipe = get_redis().pipeline(transaction=False)
        pipe.incr(key)
        pipe.expire(key, _EXPERT_RATE_WINDOW)
        count, _ = await pipe.execute()
        remaining = max(0, _EXPERT_RATE_LIMIT - count)
        return RateLimitDecision(allowed=count <= _EXPERT_RATE_LIMIT,
                                 remaining=remaining,
                                 limit=_EXPERT_RATE_LIMIT,
                                 state=RateLimitState.ENFORCED)
    except Exception as exc:
        # P13-01 finding V made this audible and left the direction to the
        # owner, because failing closed would lock every user out of Expert Mode
        # during a Redis outage. The owner's Phase 14 instruction chose neither
        # of the two options the finding posed: not "unlimited", and not
        # "locked out", but a weaker control that still counts and still
        # refuses, labelled as weaker.
        decision = _check_degraded_rate_limit(username, now=time.monotonic())
        logger.error(
            "expert-mode rate limit could NOT be checked against Redis for %r "
            "(%s: %s) — DEGRADED to a per-process limiter of %d/%ds, which "
            "%s this call. This is NOT the enforced limit: it resets on deploy "
            "and admits up to this many calls per replica.",
            username, type(exc).__name__, exc, _EXPERT_DEGRADED_LIMIT,
            _EXPERT_RATE_WINDOW, "allowed" if decision.allowed else "REFUSED")
        return decision


# ── Cortex engine path (Phase 7.2) ─────────────────────────────────────────────


def _framed_history(recent: list[dict]) -> list[dict]:
    """Prior turns as the model should see them: each earlier user turn goes
    through `frame_user_message` (idempotent; a turn with no pasted material comes
    back unchanged), so a paste from an earlier turn is still marked as data when
    it is replayed. Assistant turns are the model's own words and stay as they
    are. The stored rows are untouched: `GET /chat/history` keeps showing what
    the user typed."""
    return [{**m, "content": frame_user_message(m["content"])} if m.get("role") == "user" else m
            for m in recent]


async def _cortex_route(
    principal,
    message: str,
    *,
    conv_id: str,
    owner: bool,
    user: str,
    mode: str = "assistant",
    is_voice: bool = False,
) -> tuple[str, str]:
    """New engine path: router owner-gate → classify → skill via the Cortex bridge.

    Returns (response_text, agent_name). All real tools/DB stay in IRA; only the
    reasoning runs on the gateway. enforce_owner_gate stays fail-closed. The bridge
    call is synchronous, so it runs in a worker thread to keep the event loop free.
    """
    from router import enforce_owner_gate

    refusal = enforce_owner_gate(message, owner)
    if refusal:
        return refusal, "security_gate"

    # Reuse the existing classifier (read-only) to pick the skill; ira/skills/<name>/
    # mirrors the classifier's agent names. Fall back to conversational if unmatched.
    from agents.graph import make_initial_state
    from agents.supervisor import classify
    from skills._common import run_skill

    temp_state = make_initial_state(
        session_id="cortex-route", conversation_id=conv_id, user_query=message,
        user_id=user, is_owner=owner, mode=mode, is_voice=is_voice,
    )
    classified = await classify(temp_state)
    skill = classified.get("active_agent", "conversational")
    if skill not in _VALID_SKILLS:
        skill = "conversational"

    # Thread continuity is IRA-owned on the subprocess engine. `cortex -z` one-shots
    # cannot resume a per-conversation session (verified: `--continue <name> -z`
    # neither stores nor recalls across calls), so IRA loads the recent turns from its
    # own Postgres history, passes them as context, and persists this turn below. The
    # owner profile (who-I-am / goals / prefs) is IRA-owned business data, injected every
    # turn. (IRA's pgvector semantic retrieve() is still NOT read here — that long-term
    # recall stays a single-owner concern for a later phase; this only restores thread
    # memory.)
    from memory.store import save_message

    profile_summary = await get_profile_summary()
    recent = _framed_history(await get_recent_messages(principal, conv_id, limit=10))
    blocks: list[str] = []
    if profile_summary:
        blocks.append(profile_summary)
    # PR #67: relationship memory rides along as labelled reference DATA
    # (never instructions); fail-soft empty when the table is missing.
    from memory.relationships import get_people_summary
    people_summary = await get_people_summary(principal)
    if people_summary:
        blocks.append(people_summary)
    if recent:
        history = "\n".join(f"{m['role']}: {m['content']}" for m in recent)
        blocks.append("Recent conversation so far (oldest first):\n" + history)

    text = await asyncio.to_thread(
        run_skill, skill, message, context_blocks=blocks or None, session_id=conv_id, user_key=user,
    )

    # Persist this turn so the next one can recall it (awaited so a sequential next
    # request sees it; best-effort — never fail the reply on a memory write).
    try:
        await save_message(principal, conv_id, "user", message, user_id=user)
        await save_message(principal, conv_id, "assistant", text, model_used="cortex", user_id=user)
    except Exception:
        import logging
        logging.getLogger("ira.chat").warning("cortex-route: save_message failed", exc_info=True)

    return text, skill


# ── Standard (non-streaming) chat ─────────────────────────────────────────────

def _chat_timeout_s() -> float:
    """IRA_CHAT_TIMEOUT_S, or its default when a stand-in settings object lacks it."""
    from config import Settings

    default = Settings.model_fields["ira_chat_timeout_s"].default
    return float(getattr(get_settings(), "ira_chat_timeout_s", default))


@router.post("", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    _p: Principal = Depends(require_workspace_principal),
    _user: str = Depends(_workspace_username),
):
    # Only trust is_voice_owner when the request genuinely originates from the voice service
    owner = _is_owner(_user) or (_is_voice_service(_user) and req.is_voice_owner)

    # A typed lockdown command is acted on first, before the response cache and
    # before any agent is chosen: no agent the classifier picks for "IRA, lift
    # lockdown" ran the lift (clean install, 1 November 2026).
    t0 = time.monotonic()
    lockdown_reply = await answer_lockdown_command(
        req.message, is_owner=owner, is_voice=req.is_voice, user_id=_user)
    if lockdown_reply is not None:
        return ChatResponse(
            response=lockdown_reply,
            session_id=req.session_id,
            conversation_id=await ensure_conversation(_p, req.session_id),
            agent_used="security",
            model_used="security_gate",
            latency_ms=int((time.monotonic() - t0) * 1000),
        )

    # A prompt injection, a credential request or a request to send IRA's data
    # out gets the security agent's fixed refusal before any agent is chosen
    # (P1-12). On 5 November 2026 the classifier sent 68 of 100 such prompts to
    # the conversational agent, which answered 35 of them. After the lockdown
    # command, so "lockdown now" still engages whatever else the message says.
    refusal = short_refusal(req.message)
    if refusal is not None:
        return ChatResponse(
            response=refusal,
            session_id=req.session_id,
            conversation_id=await ensure_conversation(_p, req.session_id),
            agent_used="security",
            model_used=SHORT_REFUSAL_MODEL,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )

    # Namespaced by tenant AND workspace, through the module that exists for it.
    #
    # This used to be `f"chat:{_user}:{req.session_id}:{hash}"`. `_user` is a
    # username, and usernames are per-tenant — two tenants' `alice` shared a key
    # prefix, with a client-supplied `session_id` in the middle. A cached
    # RESPONSE was therefore addressable across the tenant boundary by anyone
    # who could supply a username, a session id and the same message.
    #
    # `redis_workspace_key` derives both scopes from the verified principal, so
    # neither is something a request can state. It raises rather than falling
    # back when a workspace is missing, which is what stops an unscoped key from
    # ever being written.
    #
    # And the verified user (ADR-0009): within one workspace, two people who
    # sent the same session id and the same message shared a cached answer —
    # one of them received a reply built from the other's history.
    cache_key = redis_workspace_key(
        _p, "chat", "u", str(_p.user_id), req.session_id, _stable_hash(req.message)
    )
    # Memory writes must never be replayed from the conversational response cache.
    if memory_save_content(req.message) is None:
        cached = await cache_get(cache_key)
        if cached:
            return ChatResponse(**cached)

    conv_id = await ensure_conversation(_p, req.session_id)
    if owner:
        from cognition.ingest import feed_cognitive_percept
        await feed_cognitive_percept(
            get_settings().ira_admin_username, "voice" if req.is_voice else "chat", req.message,
        )

    memory_result = await guard_chat_memory(
        _p,
        req.message, user=_user, is_owner=_is_owner(_user),
    )
    if memory_result is not None:
        return ChatResponse(
            response=memory_reply(memory_result),
            session_id=req.session_id,
            conversation_id=conv_id,
            agent_used="memory_guard",
            model_used="deterministic",
            latency_ms=0,
            memory_decision=(memory_result
                             if memory_result.get("status") in NEEDS_OWNER
                             else None),
        )

    if _USE_CORTEX:                                  # NEW engine path (default OFF)
        t0 = time.monotonic()
        text, agent = await _cortex_route(
            _p,
            req.message, conv_id=conv_id, owner=owner, user=_user,
            mode=req.mode, is_voice=req.is_voice,
        )
        return ChatResponse(
            response=text,
            session_id=req.session_id,
            conversation_id=conv_id,
            agent_used=agent,
            model_used="cortex",
            latency_ms=int((time.monotonic() - t0) * 1000),
        )

    # Bounded, so the caller hears "it ran out of time" from IRA rather than an
    # empty 500 from the web app's proxy (finding P1-02). A model call that
    # times out on its own raises ProviderTimeout, which the error contract
    # answers with the same 504; this deadline covers a turn that makes more
    # than one call. On expiry the turn is cancelled and its connection to the
    # model is closed.
    limit = _chat_timeout_s()
    try:
        async with asyncio.timeout(limit) as deadline:
            state = await run_graph(
                _p,
                session_id=req.session_id,
                conversation_id=conv_id,
                user_query=req.message,
                is_owner=owner,
                mode=req.mode,
                is_voice=req.is_voice,
                user_id=_user,
            )
    except TimeoutError as exc:
        if not deadline.expired():
            raise
        raise IRAError(MODEL_TIMEOUT, safe_details={"limit_s": limit},
                       log_message=f"chat turn passed IRA_CHAT_TIMEOUT_S ({limit:g} s)") from exc

    shaped, confidence = _grade_confidence(req.message, state)
    # An answer that claims an action when no tool ran and no approval token was
    # issued gets one sentence appended (P1-13); the model's text stays whole.
    shaped = append_no_action_note(shaped, acted=_turn_acted(state))

    result = ChatResponse(
        response=shaped,
        session_id=req.session_id,
        conversation_id=conv_id,
        agent_used=state.get("active_agent", "conversational"),
        model_used=_answered_by(state),
        latency_ms=state.get("latency_ms", 0),
        confidence=confidence,
        model_tier=state.get("served_tier"),
    )

    if state.get("active_agent") in ("conversational", "researcher"):
        await cache_set(cache_key, result.model_dump(), ttl=60)

    return result


# ── Streaming chat (SSE) ───────────────────────────────────────────────────────

@router.post("/stream")
async def chat_stream(
    req: ChatRequest,
    _p: Principal = Depends(require_workspace_principal),
    _user: str = Depends(_workspace_username),
):
    """
    Token-by-token streaming via SSE, routed through supervisor + biometric gate.
    Client receives: data: {"token": "..."} events, then data: {"done": true, ...}.
    """
    conv_id = await ensure_conversation(_p, req.session_id)
    # Only trust is_voice_owner when the request genuinely originates from the voice service
    owner = _is_owner(_user) or (_is_voice_service(_user) and req.is_voice_owner)

    # A typed lockdown command is acted on first, before any agent is chosen. This
    # is the chat page's endpoint, and it ran the security agent's tools for no
    # message at all (clean install, 1 November 2026). The tool runs here, before
    # the response starts, so the change does not depend on the client reading it.
    t0 = time.monotonic()
    lockdown_reply = await answer_lockdown_command(
        req.message, is_owner=owner, is_voice=req.is_voice, user_id=_user)
    if lockdown_reply is not None:
        latency = int((time.monotonic() - t0) * 1000)

        async def _lockdown_stream_gen():
            yield {"data": json.dumps({"token": lockdown_reply})}
            yield {"data": json.dumps({
                "done": True, "agent": "security", "model": "security_gate",
                "latency_ms": latency, "session_id": req.session_id,
            })}

        return EventSourceResponse(_lockdown_stream_gen())

    # A prompt injection, a credential request or a request to send IRA's data
    # out is refused in two sentences, by fixed text, before any agent is chosen
    # and before any model (P1-10, P1-12). Until 5 November 2026 this ran only
    # once the classifier had picked the security agent; it picked it for 8 of
    # the run's 100 security prompts and sent 68 to the conversational agent.
    refusal = short_refusal(req.message)
    if refusal is not None:
        refusal_latency = int((time.monotonic() - t0) * 1000)

        async def _refusal_stream_gen():
            yield {"data": json.dumps({"token": refusal})}
            yield {"data": json.dumps({
                "done": True, "agent": "security", "model": SHORT_REFUSAL_MODEL,
                "latency_ms": refusal_latency, "session_id": req.session_id,
            })}

        return EventSourceResponse(_refusal_stream_gen())

    if owner:
        from cognition.ingest import feed_cognitive_percept
        await feed_cognitive_percept(
            get_settings().ira_admin_username, "voice" if req.is_voice else "chat", req.message,
        )

    memory_result = await guard_chat_memory(
        _p,
        req.message, user=_user, is_owner=_is_owner(_user),
    )
    if memory_result is not None:
        async def _memory_stream_gen():
            text = memory_reply(memory_result)
            yield {"data": json.dumps({"token": text})}
            payload = {
                "done": True, "agent": "memory_guard", "latency_ms": 0,
                "session_id": req.session_id,
            }
            if memory_result.get("status") in NEEDS_OWNER:
                payload["memory_decision"] = memory_result
            else:
                payload["memory_result"] = memory_result
            yield {"data": json.dumps(payload)}

        return EventSourceResponse(_memory_stream_gen())

    # Sovereign coding agent (Phase 1): route coding intents (voice or text) to Aider
    # on a fresh branch. Owner-gated, branch-only. Speak an ack, then the result.
    # Off unless IRA_CODING_AGENT_ENABLED is set: the detector also matches everyday
    # asks ("find the file I saved yesterday"), which then reach normal chat.
    from agents.coding_agent import (
        coding_agent_enabled, is_coding_request, run_coding_task, spoken_result, default_repo_path,
    )
    if coding_agent_enabled() and is_coding_request(req.message):
        async def _coding_stream_gen():
            t0 = time.monotonic()
            yield {"data": json.dumps({"token": "On it — I'll branch off, make the change, run the tests, and report back."})}
            result = await run_coding_task(req.message, default_repo_path(), is_owner=owner)
            yield {"data": json.dumps({"token": " " + spoken_result(result)})}
            yield {"data": json.dumps({
                "done": True,
                "agent": "coding",
                "coding": {k: result.get(k) for k in
                           ("ok", "reason", "action", "branch", "commit", "files_changed", "tests_passed")},
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "session_id": req.session_id,
            })}

        return EventSourceResponse(_coding_stream_gen())

    # Strategy mode (Phase 5): explicit strategic asks get bounded, ranked, honest
    # deliberation. Multi-call, so it only runs when explicitly invoked (detector),
    # never on the normal low-latency fast path. Spoken summary for voice.
    from agents.strategy_mode import is_strategy_request, run_strategy
    if is_strategy_request(req.message):
        async def _strategy_stream_gen():
            t0 = time.monotonic()
            yield {"data": json.dumps({"token": "Let me think this through… "})}
            result = await run_strategy(req.message, context="", author=_p.user_id)
            body = result.to_spoken_summary() if req.is_voice else result.to_markdown()
            for word in body.split(" "):
                yield {"data": json.dumps({"token": word + " "})}
                await asyncio.sleep(0.005)
            yield {"data": json.dumps({
                "done": True,
                "agent": "strategy",
                "strategy": {"confidence": result.confidence, "degraded": result.degraded,
                             "options": len(result.options)},
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "session_id": req.session_id,
            })}

        return EventSourceResponse(_strategy_stream_gen())

    if _USE_CORTEX:                                  # NEW engine path (default OFF) — also serves voice (HTTP → here)
        text, agent = await _cortex_route(
            _p,
            req.message, conv_id=conv_id, owner=owner, user=_user,
            mode=req.mode, is_voice=req.is_voice,
        )

        async def _cortex_stream_gen():
            t0 = time.monotonic()
            for word in text.split(" "):
                yield {"data": json.dumps({"token": word + " "})}
                await asyncio.sleep(0.01)
            yield {"data": json.dumps({
                "done": True,
                "agent": agent,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "session_id": req.session_id,
            })}

        return EventSourceResponse(_cortex_stream_gen())

    from agents.state import IRAState
    from agents.supervisor import classify, is_restricted_domain
    from agents.graph import make_initial_state  # Fix P23: shared factory prevents drift

    # Fix P23: use shared factory so temp_state and run_graph() initial_state
    # can never have different required fields or wrong defaults.
    temp_state = make_initial_state(
        session_id=req.session_id,
        conversation_id=conv_id,
        user_query=req.message,
        user_id=_user,
        is_owner=owner,
        mode=req.mode,
        is_voice=req.is_voice,
    )
    classified = await classify(temp_state)
    active_agent = classified["active_agent"]
    use_deep = classified["use_deep_model"]
    # Reasoning tier: Think Mode + DeepSearch + explicit reasoning queries route here
    use_reasoning = should_use_reasoning(
        req.message,
        think_mode=req.think_mode,
        deep_search=req.deep_search,
    )

    # Voice path: force the fast tier (qwen3:8b) for low time-to-first-audio. Spoken
    # replies are short, so trade the deep model's depth for latency (Phase 4).
    if req.is_voice:
        use_deep = False
        use_reasoning = False

    # ── Biometric gate: block restricted domains for non-owners ───────────────
    cfg = get_settings()
    if not dev_mode_active() and is_restricted_domain(req.message) and not owner:
        async def blocked_generator():
            owner_name = cfg.owner_name
            blocked_msg = (
                f"I'm sorry, I'm an automated assistant for SupraCloud. "
                f"That administrative information is strictly restricted to "
                f"{owner_name}, the company founder. "
                f"I cannot share security logs, personal data, or system credentials "
                f"with anyone else. Is there something else I can help you with?"
            )
            for word in blocked_msg.split(" "):
                yield {"data": json.dumps({"token": word + " "})}
                await asyncio.sleep(0.02)
            yield {"data": json.dumps({
                "done": True, "agent": "security_gate",
                "latency_ms": 0, "session_id": req.session_id,
            })}
        return EventSourceResponse(blocked_generator())

    # Career, digital, and tutor agents need full graph execution (tool calls).
    # Run the graph first, then word-stream the pre-computed response.
    if active_agent in ("career", "digital", "browser"):
        full_state = await run_graph(
            _p,
            session_id=req.session_id,
            conversation_id=conv_id,
            user_query=req.message,
            is_owner=owner,
            mode=req.mode,
            is_voice=req.is_voice,
            user_id=_user,
        )
        final_text = full_state.get("final_response", "I encountered an issue processing that request.")

        async def tool_result_streamer():
            t0 = time.monotonic()
            for word in final_text.split(" "):
                yield {"data": json.dumps({"token": word + " "})}
                await asyncio.sleep(0.018)
            latency = int((time.monotonic() - t0) * 1000)
            yield {"data": json.dumps({
                "done": True,
                "agent": full_state.get("active_agent", active_agent),
                "latency_ms": latency,
                "session_id": req.session_id,
                "model": _answered_by(full_state),
                "model_tier": full_state.get("served_tier"),
            })}

        return EventSourceResponse(tool_result_streamer())

    from utils.search_tools import (
        get_search_context, deep_search_context,
    )
    from agents.grok_personality import build_grok_system_prompt
    from agents.engineer_agent import build_engineer_prompt
    from agents.architect_agent import (
        architect_enabled, is_architect_command,
        is_architect_trigger, is_implement_trigger, is_apply_trigger,
        extract_feature_name, stream_architect_proposal, stream_auto_implement,
    )
    # Fix P24: feature routing moved to ordered handler registry in _dispatch.py
    from api.routes._dispatch import dispatch as _feature_dispatch
    from api.routes.video_gen import is_video_understand_request

    # ── Architect self-modification: off unless IRA_ARCHITECT_ENABLED is set ──
    # Its triggers also match everyday phrases ("what's next on my calendar", "my
    # leave got approved"). While it is off those reach normal chat, and only a
    # message that names the architect is told it is off.
    _architect_on = architect_enabled()
    if not _architect_on and is_architect_command(req.message):
        async def architect_off_generator():
            yield {"data": json.dumps({"token": (
                "The architect is turned off on this computer. The owner can turn it "
                "on with IRA_ARCHITECT_ENABLED=true in .env and a restart.")})}
            yield {"data": json.dumps({
                "done": True, "agent": "architect_off",
                "latency_ms": 0, "session_id": req.session_id,
            })}
        return EventSourceResponse(architect_off_generator())

    # ── Owner gate: architect self-modification is owner-only ──────────────────
    # The architect/implement/apply triggers can generate and git-apply code against
    # IRA's own source. Gate all three behind the verified-owner flag here so a
    # non-owner can never reach stream_architect_proposal / stream_auto_implement /
    # apply_implementation, independent of per-user state isolation. This holds even
    # in dev_mode — self-modification always requires the owner.
    if _architect_on and not owner and (
        is_architect_trigger(req.message)
        or is_implement_trigger(req.message)
        or is_apply_trigger(req.message)
    ):
        async def architect_blocked_generator():
            owner_name = cfg.owner_name
            blocked_msg = (
                f"I'm sorry — building, implementing, or applying changes to my own "
                f"code is restricted to {owner_name}. I can't run the architect "
                f"workflow for anyone else. Is there something else I can help with?"
            )
            for word in blocked_msg.split(" "):
                yield {"data": json.dumps({"token": word + " "})}
                await asyncio.sleep(0.02)
            yield {"data": json.dumps({
                "done": True, "agent": "security_gate",
                "latency_ms": 0, "session_id": req.session_id,
            })}
        return EventSourceResponse(architect_blocked_generator())

    # ── Architect Agent: intercept before all other routing ───────────────────
    if _architect_on and is_architect_trigger(req.message):
        from reasoning.memory_context import select_memory_context
        memories_raw = await retrieve(_p, req.message, user_id=_user)
        memory_ctx = select_memory_context(memories_raw)

        async def architect_proposal_stream():
            async for event in stream_architect_proposal(req.message, memory_context=memory_ctx):
                # Re-map architect events to the standard SSE token format for the frontend
                if event.get("architect_start"):
                    yield {"data": json.dumps({"token": event["message"] + "\n\n"})}
                elif event.get("architect_agent") and event.get("chunk"):
                    yield {"data": json.dumps({"token": event["chunk"]})}
                elif event.get("architect_done"):
                    yield {"data": json.dumps({
                        "done": True,
                        "agent": "architect",
                        "latency_ms": event.get("latency_ms", 0),
                        "session_id": req.session_id,
                        "is_architect": True,
                    })}
        return EventSourceResponse(architect_proposal_stream())

    if _architect_on and is_implement_trigger(req.message):
        feature = extract_feature_name(req.message)
        from api.routes.architect import _get_state as _arch_get, _set_state as _arch_set
        cached = await _arch_get(_user)
        proposal_ctx = cached.get("proposal") or ""

        async def architect_impl_stream():
            async for event in stream_auto_implement(feature, proposal_context=proposal_ctx):
                if event.get("implement_start"):
                    yield {"data": json.dumps({"token": event["message"] + "\n\n"})}
                elif event.get("implement_chunk"):
                    yield {"data": json.dumps({"token": event["implement_chunk"]})}
                elif event.get("implement_done"):
                    st = await _arch_get(_user)
                    st["implementation"] = event.get("implementation", "")
                    st["feature_name"] = feature
                    st["pending_apply"] = True
                    await _arch_set(_user, st)
                    yield {"data": json.dumps({
                        "done": True,
                        "agent": "architect",
                        "latency_ms": 0,
                        "session_id": req.session_id,
                        "is_architect": True,
                        "pending_apply": True,
                    })}
        return EventSourceResponse(architect_impl_stream())

    if _architect_on and is_apply_trigger(req.message):
        # Never by voice (P0-03). Applying commits to IRA's own source and
        # restarts services. The voice service is believed when it says the
        # speaker is the owner, so on a voice turn the spoken phrase would be
        # the whole approval, and a misheard or replayed phrase would apply
        # code nobody read. The owner applies by typing it, after reading the
        # implementation, or through POST /architect/apply.
        if req.is_voice or _is_voice_service(_user):
            async def apply_by_voice_refused():
                yield {"data": json.dumps({"token": (
                    "I can't apply changes to my own code from a voice command. "
                    "Read the implementation, then type \"architect apply\" to "
                    "apply it.")})}
                yield {"data": json.dumps({
                    "done": True, "agent": "architect", "latency_ms": 0,
                    "session_id": req.session_id, "is_architect": True,
                    "refused": "voice",
                })}
            return EventSourceResponse(apply_by_voice_refused())

        from api.routes.architect import _get_state as _arch_get, _set_state as _arch_set
        from utils.auto_implement import apply_implementation

        async def apply_stream():
            # Lockdown first, before saying "Applying": the apply rewrites IRA's
            # own source, commits and restarts services (POST /architect/apply
            # refuses it the same way).
            from trust.lockdown_enforcement import lockdown_refusal

            refusal = await lockdown_refusal(risk="high", action_type="architect_apply",
                                             is_owner=owner)
            if refusal is not None:
                yield {"data": json.dumps({"token": refusal["message"] + "\n"})}
                yield {"data": json.dumps({
                    "done": True, "agent": "architect", "latency_ms": 0,
                    "session_id": req.session_id, "is_architect": True,
                    "lockdown": refusal["lockdown_state"],
                })}
                return
            yield {"data": json.dumps({"token": "⚙️ Applying implementation — running `git apply`…\n\n"})}
            st = await _arch_get(_user)
            impl = st.get("implementation")
            if not impl:
                yield {"data": json.dumps({"token": "❌ No pending implementation. Run `architect implement [feature]` first.\n"})}
            else:
                # `architect apply` is the explicit, human-gated REAL apply (git apply
                # -> commit -> restart, no remote push — see utils/auto_implement.py).
                # Pass dry_run=False so the "Applying…" message and the pending-state
                # clear below are truthful (the default dry_run=True only validates).
                # Mirrors POST /architect/apply.
                result = await apply_implementation(impl, dry_run=False)
                if result.success:
                    st["pending_apply"] = False
                    st["implementation"] = None
                    await _arch_set(_user, st)
                msg = result.message + ("\n\n" + result.error if result.error else "")
                yield {"data": json.dumps({"token": msg})}
            yield {"data": json.dumps({
                "done": True, "agent": "architect", "latency_ms": 0,
                "session_id": req.session_id, "is_architect": True,
            })}
        return EventSourceResponse(apply_stream())

    _THINK_ADDON = (
        "\n\nTHINK MODE ACTIVE: Before answering, reason through the problem thoroughly "
        "inside <think>...</think> tags. Be explicit and detailed in your reasoning. "
        "After </think>, give your final clean answer without repeating the reasoning."
    )

    # Fix P24: feature handler registry replaces the 8-block if-ladder.
    # Handlers are in _dispatch.py; first match wins; None means fall through.
    # Dispatched before retrieval (10 November 2026): a dispatched turn used to pay
    # for an embedding and the vector queries it never used.
    _dispatched = await _feature_dispatch(req, _user)
    if _dispatched is not None:
        return _dispatched

    # A follow-up may refer back to anything, so it always retrieves; a first
    # turn retrieves only when its words refer to the user's own facts or to
    # memory (utils/memory_gate.py, opt-in). The history is read only when the
    # words alone cannot decide, so a turn that retrieves anyway costs nothing
    # extra. The profile is injected every turn regardless.
    from utils.memory_gate import gate_enabled, message_refers_to_memory

    async def _retrieve_if_wanted() -> list[dict]:
        if gate_enabled() and not message_refers_to_memory(req.message):
            if not await get_recent_messages(_p, conv_id, limit=10):
                return []
        return await retrieve(_p, req.message, user_id=_user)

    # ── Search context (DeepSearch or standard) ───────────────────────────────
    used_live_x = False
    deep_search_rounds = 0

    async def _get_ctx():
        if req.deep_search:
            return await deep_search_context(req.message)
        elif not req.is_voice:
            return await get_search_context(req.message)
        return "", {}

    # ── Mode selection: Engineer > Grok > Normal ───────────────────────────────
    if req.engineer_mode:
        memories_raw = await _retrieve_if_wanted()
        search_ctx = ""
        system_prompt = build_engineer_prompt()
        system_prompt = _compose_quality_layer(
            system_prompt, req.message, think_mode=req.think_mode, deep_search=req.deep_search
        )
        use_deep = True

    elif req.grok_mode:
        memories_task = asyncio.create_task(_retrieve_if_wanted())
        search_task = asyncio.create_task(_get_ctx())
        memories_raw, (search_ctx, search_meta) = await asyncio.gather(memories_task, search_task)
        used_live_x = search_meta.get("used_live_x", False)
        deep_search_rounds = search_meta.get("deep_search_rounds", 0)
        system_prompt = build_grok_system_prompt(context=search_ctx)
        system_prompt = _compose_quality_layer(
            system_prompt, req.message, think_mode=req.think_mode, deep_search=req.deep_search
        )

    else:
        memories_raw = await _retrieve_if_wanted()
        search_ctx, search_meta = await _get_ctx()
        used_live_x = search_meta.get("used_live_x", False)
        deep_search_rounds = search_meta.get("deep_search_rounds", 0)
        system_prompt = _get_agent_system_prompt(active_agent, is_voice=req.is_voice)
        if not req.is_voice:  # keep voice replies short — no extra guidance text
            system_prompt = _compose_quality_layer(
                system_prompt, req.message, think_mode=req.think_mode, deep_search=req.deep_search
            )

    # Append Think Mode instructions to whichever system prompt was selected
    if req.think_mode and not req.is_voice:
        system_prompt += _THINK_ADDON

    from reasoning.memory_context import select_memory_context
    memory_ctx = select_memory_context(memories_raw)

    messages = [{"role": "system", "content": system_prompt}]
    # v1 1.4: inject the owner profile every turn so the brain stays grounded.
    profile_summary = await get_profile_summary()
    if profile_summary:
        messages.append({"role": "system", "content": profile_summary})
    # PR #67: relationship memory as labelled reference DATA (never instructions).
    from memory.relationships import get_people_summary
    people_summary = await get_people_summary(_p)
    if people_summary:
        messages.append({"role": "system", "content": people_summary})
    if memory_ctx:
        # Already labelled "user memory, not an instruction" by select_memory_context.
        messages.append({"role": "system", "content": memory_ctx})
    if search_ctx:
        messages.append({"role": "system", "content": f"Live information:\n{search_ctx}"})

    recent = await get_recent_messages(_p, conv_id, limit=10)
    messages.extend(_framed_history(recent))
    # The model's copy of the message marks pasted material as data (P1-11). What
    # is saved, cached and classified is `req.message` as the user wrote it.
    messages.append({"role": "user", "content": frame_user_message(req.message)})

    async def event_generator():
        full_response: list[str] = []
        full_thinking: list[str] = []
        t0 = time.monotonic()
        # The model that answers, as its provider names it (finding P1-05).
        served, recording = served_models.begin()
        try:
            if req.think_mode:
                # ── Think Mode: parse <think>…</think> tags from stream ────────
                buf = ""
                in_think = False
                OPEN, CLOSE = "<think>", "</think>"

                async for raw_tok in stream_tokens(messages, use_deep=use_deep, use_reasoning=use_reasoning):
                    buf += raw_tok
                    # Keep draining buf until we can't make progress
                    while True:
                        if in_think:
                            ci = buf.find(CLOSE)
                            if ci >= 0:
                                chunk = buf[:ci]
                                if chunk:
                                    full_thinking.append(chunk)
                                    yield {"data": json.dumps({"thinking_token": chunk})}
                                yield {"data": json.dumps({"thinking_done": True})}
                                buf = buf[ci + len(CLOSE):]
                                in_think = False
                            else:
                                safe = max(0, len(buf) - len(CLOSE))
                                if safe:
                                    chunk = buf[:safe]
                                    full_thinking.append(chunk)
                                    yield {"data": json.dumps({"thinking_token": chunk})}
                                    buf = buf[safe:]
                                break
                        else:
                            oi = buf.find(OPEN)
                            if oi >= 0:
                                pre = buf[:oi]
                                if pre:
                                    full_response.append(pre)
                                    yield {"data": json.dumps({"token": pre})}
                                yield {"data": json.dumps({"thinking_start": True})}
                                buf = buf[oi + len(OPEN):]
                                in_think = True
                            else:
                                safe = max(0, len(buf) - len(OPEN))
                                if safe:
                                    chunk = buf[:safe]
                                    full_response.append(chunk)
                                    yield {"data": json.dumps({"token": chunk})}
                                    buf = buf[safe:]
                                break

                # Flush remaining buffer
                if buf:
                    if in_think:
                        full_thinking.append(buf)
                        yield {"data": json.dumps({"thinking_token": buf})}
                        yield {"data": json.dumps({"thinking_done": True})}
                    else:
                        full_response.append(buf)
                        yield {"data": json.dumps({"token": buf})}
            else:
                # ── Normal streaming ───────────────────────────────────────────
                async for token in stream_tokens(messages, use_deep=use_deep, use_reasoning=use_reasoning):
                    full_response.append(token)
                    yield {"data": json.dumps({"token": token})}

            # No tool runs on this path and no approval token is issued (the
            # architect and tool-agent turns returned above), so an answer that
            # claims an action gets one sentence appended (P1-13), streamed as
            # a token and saved with the answer. The model's text is not cut.
            answer = "".join(full_response)
            noted = append_no_action_note(answer, acted=False)
            if noted != answer:
                note = noted[len(answer):]
                full_response.append(note)
                yield {"data": json.dumps({"token": note})}

            latency = int((time.monotonic() - t0) * 1000)
            answered = served.last
            yield {
                "data": json.dumps({
                    "done": True,
                    "agent": "engineer" if req.engineer_mode else active_agent,
                    "latency_ms": latency,
                    "session_id": req.session_id,
                    "used_live_x": used_live_x,
                    "is_engineer": req.engineer_mode,
                    "is_think": req.think_mode,
                    "deep_search_rounds": deep_search_rounds,
                    # Agent Activity view: real routing metadata, not inferred client-side.
                    # The model the provider said answered, and the tier IRA asked for.
                    "model": answered.model if answered else NOT_REPORTED,
                    "model_tier": "reasoning" if use_reasoning else ("deep" if use_deep else "fast"),
                    "memory_count": len(memories_raw or []),
                    # Confidence badge data — derived from this turn's retrieval
                    # scores + the rule-based verifier, never a model self-report.
                    "confidence": _confidence_payload(
                        req.message, "".join(full_response), memories_raw,
                    ),
                })
            }

            # User row first, then the assistant row (history order).
            _save_turn_in_background(
                _p, conv_id, req.message, "".join(full_response),
                user_id=_user,
                model_used="qwen3-reasoning" if use_reasoning else ("qwen3-deep" if use_deep else "qwen3-fast"),
                latency_ms=latency,
            )

        except Exception as e:
            yield {"data": json.dumps({"error": f"Stream interrupted: {str(e)[:100]}"})}
        finally:
            served_models.end(recording)

    return EventSourceResponse(event_generator())


@router.post("/expert")
async def chat_expert(
    req: ChatRequest,
    _p: Principal = Depends(require_workspace_principal),
    _user: str = Depends(_workspace_username),
):
    """
    Grok-style Expert Mode: 5 specialist agents run in true parallel, debate,
    and stream their individual thoughts + supervisor synthesis via SSE.

    Rate limit: max _EXPERT_RATE_LIMIT sessions per user per hour (tracked in Redis).  # Fix P18
    Events: {"agent":"researcher","label":"Researcher","emoji":"🔬","chunk":"...","done":false}
            {"agent":"supervisor","done":true,"latency_ms":N}
    """
    # Fix P18: rate limit is _EXPERT_RATE_LIMIT (currently 20/hour), not the old 3/hour
    decision = await _check_expert_rate_limit(_user)
    if not decision.allowed:
        # P14-01b, finding V. The refusal quotes the limit that was ACTUALLY
        # applied. Quoting `_EXPERT_RATE_LIMIT` while a degraded limiter refused
        # at 3 would tell the user a number nothing enforced — the same class of
        # untruth as the silent fail-open this replaced, on the other side.
        degraded = decision.state is RateLimitState.DEGRADED
        raise HTTPException(
            status_code=429,
            detail=(
                f"Expert Mode rate limit reached ({decision.limit} sessions/hour)."
                + (" The shared rate-limit backend is unavailable, so a lower "
                   "temporary limit is in force." if degraded else "")
                + " Please wait before starting another Expert Mode session."
            ),
            headers={"X-Expert-Remaining": "0", "Retry-After": "3600",
                     "X-Expert-Limit-State": decision.state.value},
        )

    from agents.expert_mode import stream_expert_mode
    from memory.store import retrieve

    owner = _is_owner(_user) or (_is_voice_service(_user) and req.is_voice_owner)

    if _USE_CORTEX:                                  # NEW engine path (default OFF): deliberation via subagents
        from router import enforce_owner_gate
        from subagents import deliberate

        from reasoning.memory_context import select_memory_context
        refusal = enforce_owner_gate(req.message, owner)
        owner_name = get_settings().owner_name
        memories_raw = await retrieve(_p, req.message, user_id=_user)
        memory_ctx = select_memory_context(memories_raw)

        async def _cortex_expert_gen():
            t0 = time.monotonic()
            if refusal:
                text = refusal
            else:
                text = await asyncio.to_thread(
                    deliberate, req.message,
                    owner_name=owner_name, memory_context=memory_ctx,
                )
            for word in text.split(" "):
                yield {"data": json.dumps({"agent": "supervisor", "chunk": word + " ", "done": False})}
                await asyncio.sleep(0.005)
            yield {"data": json.dumps({
                "agent": "supervisor", "done": True,
                "latency_ms": int((time.monotonic() - t0) * 1000),
            })}

        return EventSourceResponse(
            _cortex_expert_gen(),
            headers={"X-Expert-Remaining": str(decision.remaining),
                     # P14-01b, finding V: the quota above is only meaningful
                     # alongside which control produced it. Under DEGRADED it is
                     # a per-process count against a much lower limit.
                     "X-Expert-Limit-State": decision.state.value},
        )

    # Gather memory + live search in parallel for Expert Mode
    from utils.search_tools import get_search_context as _get_search_ctx
    memories_raw, (search_ctx, search_meta) = await asyncio.gather(
        retrieve(_p, req.message, user_id=_user),
        _get_search_ctx(req.message),
    )
    from reasoning.memory_context import select_memory_context
    expert_used_live_x = search_meta.get("used_live_x", False)
    memory_ctx = select_memory_context(memories_raw)
    if search_ctx:
        memory_ctx = f"{memory_ctx}\n\n{search_ctx}".strip()

    async def expert_generator():
        async for event in stream_expert_mode(
            query=req.message,
            memory_context=memory_ctx,
            is_owner=owner,
            image_b64=req.image_b64,
            mime_type=req.mime_type,
        ):
            # Inject used_live_x into the final supervisor done event
            if event.get("done") and event.get("agent") == "supervisor":
                event["used_live_x"] = expert_used_live_x
            yield {"data": json.dumps(event)}

    return EventSourceResponse(
        expert_generator(),
        headers={"X-Expert-Remaining": str(decision.remaining),
                 "X-Expert-Limit-State": decision.state.value},
    )


# ── Vision endpoint ───────────────────────────────────────────────────────────

class VisionRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8_000)
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    image_b64: str = Field(..., description="Base64-encoded image")
    mime_type: str = Field("image/jpeg")


@router.post("/vision")
async def chat_vision(
    req: VisionRequest,
    _p: Principal = Depends(require_workspace_principal),
    _user: str = Depends(_workspace_username),
):
    """
    Vision-capable streaming chat.  Accepts a base64-encoded image alongside
    a text prompt.  Uses VLLM_VISION_URL if configured; falls back to the
    fast text model with a graceful degradation note.
    """
    conv_id = await ensure_conversation(_p, req.session_id)

    async def vision_generator():
        # Prompt 3.3: route the image + prompt to the local VL model helper (3.1),
        # which streams tokens and fails soft (yields a clear message) if no vision
        # model is available.
        from utils.llm import stream_vision_tokens
        t0 = time.monotonic()
        full_response: list[str] = []
        try:
            async for token in stream_vision_tokens(
                prompt=req.message,
                image_b64=req.image_b64,
                mime_type=req.mime_type,
                system=_get_agent_system_prompt("conversational"),
            ):
                full_response.append(token)
                yield {"data": json.dumps({"token": token})}

            latency = int((time.monotonic() - t0) * 1000)
            yield {"data": json.dumps({"done": True, "agent": "vision", "latency_ms": latency})}

            # Fix P3: scope vision saves to the authenticated user (not "system").
            # User row first, then the assistant row (history order).
            _save_turn_in_background(
                _p, conv_id, req.message, "".join(full_response),
                user_id=_user, model_used="vision", latency_ms=latency,
            )
        except Exception as e:
            yield {"data": json.dumps({"error": f"Vision error: {str(e)[:100]}"})}

    return EventSourceResponse(vision_generator())


# ── Document analysis endpoint (PDF / DOCX / TXT) ────────────────────────────

#: Scanned pages read by OCR per upload, at most. Any workspace member can
#: upload, and a scanned book would otherwise hold a worker thread for an hour;
#: the pages left unread are named in the text the model receives.
_OCR_MAX_PAGES = 10
#: Per page, and for the whole upload. A scanned page takes seconds on an idle
#: CPU and much longer on a loaded one; a page that runs out is reported unread.
_OCR_PAGE_TIMEOUT_S = 60.0
_OCR_UPLOAD_BUDGET_S = 120.0


def _ocr_blank_pages(content: bytes, pages: list[str], blank: list[int]) -> list[str]:
    """Read pages with no text layer (scans) by OCR, in place. Returns notes.

    A scanned PDF has pages that are only images, so the text extractor returns
    nothing for them, and the model used to receive an empty "document" and
    answer as if the file said nothing. Each OCR'd page is labelled as such,
    and any page left unread is said, so the model never treats silence as
    content.
    """
    import tempfile
    import time as _time
    from pathlib import Path

    from memory.pdf_ocr import OcrFailed, OcrUnavailable, find_tools, ocr_page

    try:
        tools = find_tools()
    except OcrUnavailable:
        return [f"[{len(blank)} page(s) have no text layer (a scan?) and could not be read: "
                "OCR is not installed. Debian/Ubuntu: apt install poppler-utils "
                "tesseract-ocr; macOS: brew install poppler tesseract]"]

    notes: list[str] = []
    failed = 0
    deadline = _time.monotonic() + _OCR_UPLOAD_BUDGET_S
    todo = blank[:_OCR_MAX_PAGES]
    done = 0
    # A private directory (0700), removed with the file in it.
    with tempfile.TemporaryDirectory(prefix="ira-doc-ocr-") as tmp:
        path = Path(tmp) / "upload.pdf"
        path.write_bytes(content)
        for i in todo:
            left = deadline - _time.monotonic()
            if left <= 1.0:
                break
            try:
                text = ocr_page(path, i + 1, tools=tools,
                                timeout=min(_OCR_PAGE_TIMEOUT_S, left))
            except OcrFailed:
                failed += 1
                done += 1
                continue
            done += 1
            if text.strip():
                pages[i] = f"[Page {i + 1}, read by OCR]\n{text}"
            else:
                failed += 1
    unread = len(blank) - done
    if failed:
        notes.append(f"[{failed} scanned page(s) could not be read by OCR]")
    if unread:
        notes.append(f"[{unread} scanned page(s) not read: OCR stops after "
                     f"{_OCR_MAX_PAGES} pages or {int(_OCR_UPLOAD_BUDGET_S)} s per upload]")
    return notes


def _extract_document_text(content: bytes, filename: str, content_type: str) -> str:
    """Extract plain text from PDF, DOCX, or TXT file bytes.

    PDF pages with no text layer are read by OCR when the local tools are
    installed (bounded; see `_ocr_blank_pages`). This can take seconds per
    page, so callers on the event loop run it in a thread.
    """
    fname = (filename or "").lower()
    ctype = (content_type or "").lower()

    # PDF
    if fname.endswith(".pdf") or "pdf" in ctype:
        try:
            import io
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            pages = [page.extract_text() or "" for page in reader.pages]
        except ImportError:
            return "[pypdf not installed — cannot extract PDF text]"
        except Exception as e:
            return f"[PDF extraction error: {e}]"
        blank = [i for i, p in enumerate(pages) if not p.strip()]
        notes = _ocr_blank_pages(content, pages, blank) if blank else []
        return "\n\n".join([p for p in pages if p.strip()] + notes)

    # DOCX. "officedocument" alone also matches Excel and PowerPoint types.
    if fname.endswith(".docx") or "wordprocessingml" in ctype:
        try:
            import io
            from docx import Document
            from docx.table import Table
            doc = Document(io.BytesIO(content))
            # Paragraphs and tables in document order: reading paragraphs alone
            # dropped every table, and with it most budgets and schedules.
            parts: list[str] = []
            for block in doc.iter_inner_content():
                if isinstance(block, Table):
                    for row in block.rows:
                        cells: list[str] = []
                        seen = None
                        for cell in row.cells:   # a merged cell repeats; keep it once
                            if cell._tc is not seen:   # the same cell, not merely the same text
                                cells.append(cell.text.strip())
                            seen = cell._tc
                        if any(cells):
                            parts.append(" | ".join(cells))
                elif block.text.strip():
                    parts.append(block.text)
            return "\n".join(parts)
        except ImportError:
            return "[python-docx not installed — cannot extract DOCX text]"
        except Exception as e:
            return f"[DOCX extraction error: {e}]"

    # Plain text / markdown / CSV. A byte-order mark names the encoding (Windows
    # Notepad saves "Unicode" text as UTF-16); a binary file is never decoded
    # into text the model would read as the document.
    if _unreadable_upload(content, filename, content_type):
        return "[not a text document — nothing was read from it]"
    for bom, codec in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if content.startswith(bom):
            return content.decode(codec, errors="replace")
    try:
        return content.decode("utf-8", errors="replace")
    except Exception:
        return "[Could not decode document as text]"


#: What the document upload cannot read, by type, said to the user instead of
#: handing the model the file's bytes decoded as text (it answered from them).
#: Audio and video are not read as documents: the upload route transcribes them.
_AUDIO = "an audio recording"
_VIDEO = "a video"
_AUDIO_EXT = (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus")
_VIDEO_EXT = (".mp4", ".webm", ".mov", ".mkv", ".avi")
_TEXT_EXT = (".txt", ".md", ".markdown", ".csv")
#: Control bytes text does not contain (tab, newlines, form feed and ESC aside).
_BINARY_CONTROLS = bytes(b for b in range(32) if b not in b"\t\n\v\f\r\x1b")
_OFFICE_OTHER = {".doc": "a Word 97–2003 file (.doc)", ".xls": "an Excel file",
                 ".xlsx": "an Excel file", ".ppt": "a PowerPoint file",
                 ".pptx": "a PowerPoint file"}


def _unreadable_upload(content: bytes, filename: str, content_type: str) -> str | None:
    """What this upload is, when the document reader cannot read it; else None.

    PDF and Word (.docx) have readers. Text, Markdown and CSV are text. Audio,
    video, other Office formats and any other binary file are named, never
    decoded as text.
    """
    fname = (filename or "").lower()
    ctype = (content_type or "").lower()
    if fname.endswith((".pdf", ".docx")) or "pdf" in ctype or "wordprocessingml" in ctype:
        return None
    if ctype.startswith("audio/") or fname.endswith(_AUDIO_EXT):
        return _AUDIO
    if ctype.startswith("video/") or fname.endswith(_VIDEO_EXT):
        return _VIDEO
    for ext, what in _OFFICE_OTHER.items():
        if fname.endswith(ext):
            return what
    # Chrome and Edge on Windows label a .csv as "application/vnd.ms-excel" when
    # Office is installed: a text extension is not refused for its label.
    if not fname.endswith(_TEXT_EXT) and (
            ctype in ("application/msword", "application/vnd.ms-excel", "application/vnd.ms-powerpoint")
            or "spreadsheetml" in ctype or "presentationml" in ctype):
        return "an Office file this reader does not open"
    sample = content[:8192]
    if sample.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        return None                       # text with a byte-order mark
    if b"\x00" in sample:
        return "a binary file"
    # Control bytes, not undecodable ones: Windows-1252 text (Excel's "CSV",
    # Notepad's "ANSI") is not UTF-8, but it is text.
    if sum(sample.count(bytes([b])) for b in _BINARY_CONTROLS) > len(sample) // 100:
        return "a binary file"
    return None


@router.post("/document", include_in_schema=False)
async def chat_document(
    message: str = None,
    session_id: str = None,
    _p: Principal = Depends(require_workspace_principal),
    _user: str = Depends(_workspace_username),
):
    """Hidden stub — document upload is handled by /document/upload."""
    raise HTTPException(status_code=501, detail="Use /api/v1/chat/document/upload for document analysis.")


# Register the real multipart handler manually to avoid FastAPI's form conflicts
from fastapi import Form, File, UploadFile


@router.post("/document/upload")
async def chat_document_upload(
    message: str = Form(...),
    session_id: str = Form(default=None),
    file: UploadFile = File(...),
    _p: Principal = Depends(require_workspace_principal),
    _user: str = Depends(_workspace_username),
):
    """
    Analyse an uploaded document (PDF, DOCX, TXT) alongside a user message.

    Extracts text from the file, injects it as context, and streams the LLM
    response token-by-token via SSE.  Capped at 12 000 chars to fit context.

    Returns SSE stream: {"token": "..."} … {"done": true, "agent": "document"}
    """
    session_id = session_id or str(uuid.uuid4())

    # P1-14 (6 November 2026): the #214 gate runs here too, over the user's own
    # words only. The file's body is material to work on: it is wrapped as
    # external data below and never scanned, so an instruction planted inside a
    # document cannot refuse the question asked about it.
    refusal = short_refusal(message)
    if refusal is not None:
        async def _refusal_upload_gen():
            yield {"data": json.dumps({"token": refusal})}
            yield {"data": json.dumps({
                "done": True, "agent": "security", "model": SHORT_REFUSAL_MODEL,
                "latency_ms": 0, "session_id": session_id,
            })}

        return EventSourceResponse(_refusal_upload_gen())

    conv_id = await ensure_conversation(_p, session_id)

    from utils.file_utils import read_with_size_cap
    raw = await read_with_size_cap(file, max_bytes=50 * 1024 * 1024)

    def answer(source: str, said: str, saved_user: str):
        """Stream the model's answer from ``source``, after ``said``; then keep the exchange."""
        messages = [
            {"role": "system", "content": _get_agent_system_prompt("conversational")},
            {"role": "system", "content": source},
            {"role": "user", "content": message},
        ]

        async def answer_generator():
            full_response: list[str] = [said]
            t0 = time.monotonic()
            try:
                async for token in stream_tokens(messages, use_deep=True):
                    full_response.append(token)
                    yield {"data": json.dumps({"token": token})}

                latency = int((time.monotonic() - t0) * 1000)
                yield {"data": json.dumps({
                    "done": True,
                    "agent": "document",
                    "latency_ms": latency,
                    "session_id": session_id,
                    "filename": file.filename,
                })}

                # Fix P3: scope document saves to the authenticated user (not "system").
                # User row first, then the assistant row (history order).
                _save_turn_in_background(
                    _p, conv_id, saved_user, "".join(full_response),
                    user_id=_user, model_used="qwen3-deep", latency_ms=latency,
                )
            except Exception as e:
                yield {"data": json.dumps({"error": f"Document analysis error: {str(e)[:100]}"})}

        return answer_generator()

    unreadable = _unreadable_upload(raw, file.filename or "", file.content_type or "")
    if unreadable in (_AUDIO, _VIDEO):
        return EventSourceResponse(_listen_then_answer(
            raw, file.filename or "recording", unreadable == _VIDEO, message, answer,
            _p, conv_id, session_id, _user))
    if unreadable:
        # Said, not guessed: the model is not called with bytes it cannot read.
        reply = (f"I can't read **{file.filename}**: it is {unreadable}. Here I can read "
                 "PDF, Word (.docx), plain text, Markdown and CSV files. "
                 + ("Save it as .docx or PDF and upload it again."
                    if unreadable.startswith(("a Word", "an Excel", "a PowerPoint", "an Office"))
                    else "I haven't guessed at what it contains."))

        async def unreadable_generator():
            yield {"data": json.dumps({"token": reply})}
            yield {"data": json.dumps({"done": True, "agent": "document", "latency_ms": 0,
                                       "session_id": session_id, "filename": file.filename})}
            from memory.store import save_message
            try:
                await save_message(_p, conv_id, "user", f"[Document: {file.filename}] {message}", user_id=_user)
                await save_message(_p, conv_id, "assistant", reply, user_id=_user)
            except Exception as exc:  # the reply was already sent; record why history is short
                logging.getLogger("ira.chat").warning(f"save_message failed: {exc}")

        return EventSourceResponse(unreadable_generator())

    # Off the event loop: PDF parsing, and OCR of scanned pages, take seconds.
    doc_text = await asyncio.to_thread(
        _extract_document_text, raw, file.filename or "", file.content_type or "")

    # Truncate to fit context window
    LIMIT = 12_000
    if len(doc_text) > LIMIT:
        doc_text = doc_text[:LIMIT] + f"\n\n[… document truncated at {LIMIT} chars …]"

    # The file's text is data, never instructions: whoever wrote it is not the user.
    doc_system = (
        f"The user has uploaded a document: **{file.filename}**\n\n"
        f"Document contents:\n\n{wrap_external_content(doc_text, source='uploaded document')}\n\n"
        "Answer the user's question using the document as your primary source. "
        "If the document does not contain the answer, say so clearly."
    )
    return EventSourceResponse(answer(doc_system, "", f"[Document: {file.filename}] {message}"))


#: The longest recording a chat upload transcribes. Measured 23 October on an
#: idle 4-CPU machine with Whisper small: 9 minutes of speech took 162 s, 3.3
#: times real time, after a one-off model load of about 52 s.
_UPLOAD_AUDIO_MAX_SECONDS = 600
#: One upload transcription at a time. Each holds the CPUs for minutes, and
#: the voice path shares the model.
_UPLOAD_STT_SLOTS = asyncio.Semaphore(1)


def _transcribe_upload(raw: bytes) -> tuple[str, str, float]:
    """(transcript, language, seconds) from the local Whisper model.

    Never an outside service: the uploaded recording stays on this machine,
    whatever IRA_ALLOW_EXTERNAL_API says.
    """
    from voice.stt import transcribe_audio_bytes
    text, lang, _conf, pcm16 = transcribe_audio_bytes(raw, max_seconds=_UPLOAD_AUDIO_MAX_SECONDS)
    return (text or "").strip(), lang, len(pcm16) / 2 / 16000


async def _transcribe_in_turn(raw: bytes) -> tuple[str, str, float]:
    """``_transcribe_upload`` in a worker thread, one upload at a time.

    The slot is released when the thread finishes, not when the request
    does: a closed page cancels the stream, but the thread runs on, and
    releasing early would let the next upload start beside it.
    """
    await _UPLOAD_STT_SLOTS.acquire()
    try:
        work = asyncio.get_running_loop().run_in_executor(None, _transcribe_upload, raw)
    except BaseException:
        _UPLOAD_STT_SLOTS.release()
        raise

    def finished(done: asyncio.Future) -> None:
        _UPLOAD_STT_SLOTS.release()
        if not done.cancelled():
            done.exception()   # retrieved: an abandoned failure is not logged as unhandled

    work.add_done_callback(finished)
    return await asyncio.shield(work)


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    if not minutes:
        return f"{secs} s"
    return f"{minutes} min" if not secs else f"{minutes} min {secs:02d} s"


async def _listen_then_answer(raw: bytes, name: str, is_video: bool, message: str, answer,
                              _p: Principal, conv_id, session_id: str, _user: str):
    """An audio or video upload: transcribed here, then answered from the transcript.

    When it cannot be transcribed (no speech-to-text on this install, too
    long, no sound, or no speech heard) the reply says so and the model is
    not called.
    """
    from voice.stt import AudioTooLong, STTUnavailable

    said = f"Listening to **{name}** on this machine…"
    yield {"data": json.dumps({"token": said})}
    saved_user = f"[{'Video' if is_video else 'Audio'}: {name}] {message}"
    log = logging.getLogger("ira.chat")
    cap = _duration(_UPLOAD_AUDIO_MAX_SECONDS)
    refusal = None
    try:
        text, lang, seconds = await _transcribe_in_turn(raw)
    except STTUnavailable as exc:
        log.warning(f"upload transcription unavailable: {type(exc).__name__}")
        refusal = ("I can't: speech-to-text is not available on this install. "
                   "I haven't guessed at what it contains.")
    except AudioTooLong as exc:
        if exc.seconds is not None:
            refusal = f"It is about {_duration(exc.seconds)} long, and here I transcribe recordings up to {cap}. "
        else:   # decoding stopped at the limit, so its full length is unknown
            refusal = f"It runs past {cap}, the longest recording I transcribe here. "
        refusal += "Trim it, or split it and upload the parts."
    except Exception as exc:   # anything else is about the file: PyAV could not read sound from it
        log.info(f"upload transcription failed on {name!r}: {type(exc).__name__}")
        refusal = "I couldn't read any sound from it. I haven't guessed at what it contains."
    else:
        if not text:
            refusal = (f"I listened to all {_duration(seconds)} of it and heard no speech. "
                       "I haven't guessed at what it contains.")

    if refusal:
        reply = f"\n\n{refusal}"
        yield {"data": json.dumps({"token": reply})}
        yield {"data": json.dumps({"done": True, "agent": "document", "latency_ms": 0,
                                   "session_id": session_id, "filename": name})}
        from memory.store import save_message
        try:
            await save_message(_p, conv_id, "user", saved_user, user_id=_user)
            await save_message(_p, conv_id, "assistant", said + reply, user_id=_user)
        except Exception as exc:  # the reply was already sent; record why history is short
            log.warning(f"save_message failed: {type(exc).__name__}")
        return

    heard = (f" done: {_duration(seconds)}, transcribed here. The recording was not sent anywhere."
             + (" I heard its sound track; I can't see the picture." if is_video else "") + "\n\n")
    yield {"data": json.dumps({"token": heard})}
    LIMIT = 12_000
    if len(text) > LIMIT:
        text = text[:LIMIT] + f"\n\n[… transcript truncated at {LIMIT} chars …]"
    source = (
        f"The user has uploaded {'a video' if is_video else 'an audio recording'}: **{name}**\n\n"
        f"A local speech-to-text transcript of it follows (language: {lang}). "
        "It can mishear names and numbers.\n\n"
        f"Transcript:\n\n{wrap_external_content(text, source='uploaded recording, transcribed')}\n\n"
        "Answer the user's question using the transcript as your primary source. "
        "If the transcript does not contain the answer, say so clearly."
    )
    async for event in answer(source, said + heard, saved_user):
        yield event


def _get_agent_system_prompt(agent: str, is_voice: bool = False) -> str:
    """
    Return the correct system prompt for the classified agent type.
    Voice requests get a concise-reply suffix appended.
    """
    from agents.conversational import IRA_SYSTEM
    from agents.researcher import _SYSTEM as RESEARCHER_SYSTEM
    from agents.security import _SYSTEM as SECURITY_SYSTEM
    from agents.executor import _SYSTEM as EXECUTOR_SYSTEM
    from agents.creator import _SYSTEM as CREATOR_SYSTEM
    from agents.website import _SYSTEM as WEBSITE_SYSTEM
    from agents.tutor import _SYSTEM as TUTOR_SYSTEM
    from agents.career import _SYSTEM as CAREER_SYSTEM
    from agents.digital import _SYSTEM as DIGITAL_SYSTEM

    prompt = {
        "conversational": IRA_SYSTEM,
        "researcher":     RESEARCHER_SYSTEM,
        "security":       SECURITY_SYSTEM,
        "executor":       EXECUTOR_SYSTEM,
        "creator":        CREATOR_SYSTEM,
        "website":        WEBSITE_SYSTEM,
        "tutor":          TUTOR_SYSTEM,
        "career":         CAREER_SYSTEM,
        "digital":        DIGITAL_SYSTEM,
    }.get(agent, IRA_SYSTEM)
    # Every agent draws the same line between the request and what it quotes
    # (P1-11); the agents whose own prompt already carries the rule get it once.
    prompt = with_quoted_material_rule(prompt)
    # And none claims an action no tool took (P1-13); the security agent's own
    # prompt already carries the rule, so it is not repeated there.
    prompt = with_no_action_claim_rule(prompt)
    # And answers a simple question briefly (P0 performance, run 4 of 6 November):
    # the agents whose own prompt already carries the rule get it once.
    prompt = with_brief_answer_rule(prompt)

    if is_voice:
        # Voice responses must be short — TTS reads every word aloud
        voice_suffix = (
            "\n\nVOICE MODE: You are speaking aloud. "
            "Keep every response to 1-2 short sentences maximum. "
            "Be warm, direct, and conversational. "
            "Never use bullet points, headers, or markdown — speak naturally."
        )
        prompt = prompt + voice_suffix

    return prompt


# ── Conversation history ───────────────────────────────────────────────────────

@router.get("/history", response_model=HistoryResponse)
async def history(
    session_id: str = Query(...),
    limit: int = Query(default=50, le=200),
    _p: Principal = Depends(require_workspace_principal),
    _user: str = Depends(_workspace_username),
):
    conv_id = await ensure_conversation(_p, session_id)
    messages = await get_recent_messages(_p, conv_id, limit=limit)
    return HistoryResponse(
        session_id=session_id,
        conversation_id=conv_id,
        messages=[HistoryMessage(**m) for m in messages],
    )
