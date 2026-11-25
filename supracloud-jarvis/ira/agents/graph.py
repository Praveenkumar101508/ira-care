"""
IRA LangGraph — hierarchical multi-agent graph with biometric security gate.

Flow:
  START
    → retrieve_memory          (fetch relevant past context)
    → classify                 (route to the right specialist)
    → biometric_gate           (block restricted domains for non-owners)
    → [conversational | researcher | security | website | creator | executor
       | career | tutor | digital | access_denied]
    → store_interaction        (persist message + async embedding)
  END

DEV_MODE: biometric gate is bypassed entirely (all requests treated as owner).
"""

from __future__ import annotations

import time

import logging
from typing import TYPE_CHECKING

from langgraph.graph import StateGraph, START, END
from langchain_core.messages import HumanMessage

_graph_logger = logging.getLogger("ira.graph")

if TYPE_CHECKING:
    # Types only. The saver is imported at runtime inside init_checkpointer,
    # where its absence is a handled fallback rather than an import error.
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agents.state import IRAState
from agents.supervisor import classify
from agents.conversational import conversational
from agents.researcher import researcher
from agents.security import security_guardian
from agents.website import website_manager
from agents.creator import meta_agent_creator
from agents.executor import executor
from agents.career import career_agent
from agents.tutor import tutor_agent
from agents.digital import digital_agent
from agents.browser_agent import browser_agent
from agents.armory import armory_agent
from memory.namespaces import SYSTEM_NAMESPACE
from memory.store import retrieve, ensure_conversation, save_message
from config import get_settings


# ── Where the graph's workspace context lives ────────────────────────────────
#
# In `config["configurable"]`, NOT in `IRAState`.
#
# `IRAState` is checkpointed by AsyncPostgresSaver and restored when a thread
# resumes. A workspace stored there would be re-read on resume — hours later,
# possibly after the user lost access to it — and the graph would write into a
# workspace nobody currently authorised. `configurable` is per-invocation: it
# is supplied fresh by the caller on every turn, from a Principal that route
# dependency has just verified, and it is never persisted.
#
# Nothing here falls back. A node that cannot find a principal raises, because
# the alternative is a graph turn writing somewhere it guessed.

CONFIG_PRINCIPAL_KEY = "principal"


def principal_from_config(config) -> object:
    """The verified principal for THIS invocation, or a refusal.

    `config` is LangGraph's RunnableConfig. A node declares a second parameter
    and LangGraph passes it; a node that does not declare one cannot reach
    customer data, which is a useful default.
    """
    from core.identity.db_context import MissingWorkspaceContext

    principal = ((config or {}).get("configurable") or {}).get(CONFIG_PRINCIPAL_KEY)
    if principal is None:
        raise MissingWorkspaceContext(
            "The agent graph was invoked without a verified principal in "
            "config['configurable']['principal']. Refusing to read or write "
            "memory: there is no workspace to bind, and guessing one would "
            "put this turn's conversation somewhere nobody chose."
        )
    return principal


# ── Memory retrieval node ──────────────────────────────────────────────────────

async def retrieve_memory(state: IRAState, config=None) -> IRAState:
    """Fetch top-K relevant memories + the owner profile, formatted for the prompt.

    The owner profile (who-I-am / goals / projects / prefs) is injected every turn so
    the brain stays grounded; it is prepended to the retrieved-memory context.
    """
    from owner_profile import get_profile_summary
    profile_summary = await get_profile_summary()

    # A first turn that does not refer to the user's own facts or to memory skips
    # the vector retrieval (utils/memory_gate.py, 10 November 2026); a turn with
    # history always retrieves. The profile above is injected regardless.
    from utils.memory_gate import retrieval_wanted
    history = state.get("messages") or []
    if retrieval_wanted(state["user_query"], has_history=len(history) > 1):
        memories = await retrieve(
            principal_from_config(config),
            state["user_query"],
            user_id=state.get("user_id", SYSTEM_NAMESPACE),
        )
    else:
        memories = []
    lines = []
    for m in memories or []:
        score = f"{m['similarity']:.2f}"
        lines.append(f"[{m['source_type']} | similarity={score}] {m['content']}")

    # The agents put this block in a SYSTEM message, so it carries the same
    # reference-only label the streaming path's `select_memory_context` uses.
    # Recall includes Memory Vault rows for the owner, and those are not all
    # typed by the owner (realtime-brain consolidations land there too); text
    # inside a memory must read as data, never as an instruction.
    from reasoning.memory_context import MEMORY_REFERENCE_LABEL

    recalled = MEMORY_REFERENCE_LABEL + "\n" + "\n".join(lines) if lines else ""
    sections = [s for s in (profile_summary, recalled) if s]
    # Keep the retrieval SCORES (not the text) on the state so the confidence
    # layer can grade this turn's support without retrieving a second time.
    support = [
        {
            "source_type": m.get("source_type"),
            "similarity": m.get("similarity"),
            "rerank_score": m.get("rerank_score"),
        }
        for m in memories or []
    ]
    return {**state, "memory_context": "\n\n".join(sections), "retrieval_support": support}


# ── Biometric / Context-Aware Security Gate ────────────────────────────────────

async def biometric_gate(state: IRAState) -> IRAState:
    """
    The Biometric Dual-Role Clearance Gate.

    DEV_MODE: skipped entirely — all requests pass as owner.
    Production:
      PASS  → is_owner=True, or query is not in a restricted domain
      BLOCK → is_owner=False AND query is in a restricted domain
    """
    cfg = get_settings()

    # Dev mode: bypass gate completely — but only where the edition honours the
    # flag (private). The raw flag is not authority: P2-02, `dev_mode_active()`.
    from api.middleware.auth import dev_mode_active

    if dev_mode_active():
        return {**state, "is_owner": True, "clearance_level": "admin"}

    # V1·Phase 3: decision comes from the unified owner-gate (single source of truth
    # shared with the router), so the graph and router paths can never diverge. The
    # branded refusal below stays a graph-path UX concern.
    from security.owner_gate import evaluate as _evaluate_gate

    if _evaluate_gate(state["user_query"], state.get("is_owner", False)).allowed:
        return state

    # Owner-only query + not owner → block
    owner_name = cfg.owner_name
    refusal = (
        f"I'm sorry — I'm an automated assistant for SupraCloud. "
        f"That administrative information is strictly restricted to "
        f"{owner_name}, the company founder. "
        f"I cannot share security logs, personal data, system credentials, "
        f"or internal architecture details with anyone else. "
        f"If you have a general enquiry about SupraCloud's services or need "
        f"technical assistance, I'm delighted to help with that instead."
    )

    return {
        **state,
        "active_agent": "access_denied",
        "final_response": refusal,
        "model_used": "security_gate",
        "latency_ms": 0,
        "error": None,
    }


def route_after_gate(state: IRAState) -> str:
    """Route from biometric_gate to specialist or access_denied."""
    return state["active_agent"]


# ── Access-denied terminal node ────────────────────────────────────────────────

async def access_denied(state: IRAState) -> IRAState:
    """Terminal node for blocked requests. Refusal message already set."""
    return state


# ── Interaction persistence node ───────────────────────────────────────────────

async def store_interaction(state: IRAState, config=None) -> IRAState:
    """Persist the user message and IRA response."""
    conv_id = state.get("conversation_id", "")
    if not conv_id:
        return state

    principal = principal_from_config(config)
    await save_message(
        principal,
        conv_id,
        role="user",
        content=state["user_query"],
        user_id=state.get("user_id", SYSTEM_NAMESPACE),
    )
    await save_message(
        principal,
        conv_id,
        role="assistant",
        content=state["final_response"],
        model_used=state.get("model_used"),
        latency_ms=state.get("latency_ms"),
        user_id=state.get("user_id", SYSTEM_NAMESPACE),
    )
    return state


# ── Graph construction ────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    g = StateGraph(IRAState)

    # Core nodes
    g.add_node("retrieve_memory",   retrieve_memory)
    g.add_node("classify",          classify)
    g.add_node("biometric_gate",    biometric_gate)
    g.add_node("access_denied",     access_denied)
    g.add_node("conversational",    conversational)
    g.add_node("researcher",        researcher)
    g.add_node("security",          security_guardian)
    g.add_node("website",           website_manager)
    g.add_node("creator",           meta_agent_creator)
    g.add_node("executor",          executor)
    g.add_node("career",            career_agent)
    g.add_node("tutor",             tutor_agent)
    g.add_node("digital",           digital_agent)
    g.add_node("browser",           browser_agent)
    g.add_node("armory",            armory_agent)
    g.add_node("store_interaction", store_interaction)

    # Entry → memory retrieval → classification → biometric gate
    g.add_edge(START, "retrieve_memory")
    g.add_edge("retrieve_memory", "classify")
    g.add_edge("classify", "biometric_gate")

    # Conditional routing from biometric_gate to specialist (or access_denied)
    g.add_conditional_edges(
        "biometric_gate",
        route_after_gate,
        {
            "conversational": "conversational",
            "researcher":     "researcher",
            "security":       "security",
            "website":        "website",
            "creator":        "creator",
            "executor":       "executor",
            "career":         "career",
            "tutor":          "tutor",
            "digital":        "digital",
            "browser":        "browser",
            "armory":         "armory",
            "access_denied":  "access_denied",
        },
    )

    # All specialists + access_denied → store → END
    for node in ["conversational", "researcher", "security", "website",
                 "creator", "executor", "career", "tutor", "digital", "browser",
                 "armory", "access_denied"]:
        g.add_edge(node, "store_interaction")
    g.add_edge("store_interaction", END)

    return g


#: Prefix for every checkpoint thread this application writes. Fixed and
#: greppable, so an un-namespaced thread id is visually obvious in the
#: `checkpoints` table and a `LIKE` scan for one tenant's threads is a prefix
#: match rather than a full scan.
CHECKPOINT_THREAD_PREFIX = "ira:t"


def checkpoint_thread_id(principal, session_id: str) -> str:
    """The LangGraph thread key, namespaced by tenant and workspace.

    ── Why the session id alone was not enough ──────────────────────────────

    `thread_id` used to be the bare `session_id`, which arrives **on the request
    body**. That made the checkpoint namespace client-addressable: a caller who
    knew another session's id could name that thread directly, and the
    checkpoint tables have no tenant column, no workspace column and no RLS to
    stop them — their schema is LangGraph's own (migration 097 creates it as
    LangGraph defines it), so everything Batch 5C built stops at their edge.

    Putting the verified tenant and workspace in front of the caller's value
    fixes that without a schema change: the client still controls the suffix and
    controls nothing else, so the worst it can do is collide with itself.

    ── The second job this does ─────────────────────────────────────────────

    It gives erasure a handle. `IRAState` carries the message history and
    `memory_context` — vault memories injected into the prompt — and
    `core/privacy/erasure.py` had no way to find those rows, so an erasure
    reported success while leaving them behind. A prefix that names the tenant
    and the workspace is what makes them findable.

    Refuses rather than falling back. A principal with no workspace has no
    namespace to write into, and a thread keyed on a guess is worse than an
    error.

    ── And the user (ADR-0009) ───────────────────────────────────────────────

    Tenant and workspace still left one gap: two people in the same workspace
    who sent the same session id named the same thread, and with it each
    other's message history and injected vault memories. The verified user now
    sits between the workspace and the client's value, as it does in the
    `conversations` policies, so the client controls only the suffix of its own
    namespace. Threads written before this carry no `:u:` segment; nothing
    builds their ids any more, and erasure removes them (`erasure._erase_checkpoints`).
    """
    from core.identity.db_context import MissingWorkspaceContext

    tenant = getattr(principal, "tenant_id", None)
    workspace = getattr(principal, "workspace_id", None)
    user = getattr(principal, "user_id", None)
    if tenant is None or workspace is None or user is None:
        raise MissingWorkspaceContext(
            "checkpoint_thread_id() needs a principal carrying a tenant, a "
            "verified workspace and a user. Checkpointed state holds the message "
            "history and the memory context injected into the prompt, so a "
            "thread written outside that namespace is customer content with "
            "nothing to attribute it to and no way to erase it."
        )
    return f"{CHECKPOINT_THREAD_PREFIX}:{tenant}:w:{workspace}:u:{user}:s:{session_id}"


def checkpoint_thread_prefix(tenant_id, workspace_id=None, user_id=None) -> str:
    """The prefix matching every thread of a tenant, a workspace, or one user in
    one workspace.

    Used by erasure. Kept beside the builder above so the two cannot drift —
    a prefix that stopped matching what the builder produces would make erasure
    silently delete nothing, which is the failure mode this whole phase is about.

    The workspace prefix ends at the separator, so it matches both the user
    segment and a pre-ADR-0009 thread (`…:w:{workspace}:s:{session}`).
    """
    base = f"{CHECKPOINT_THREAD_PREFIX}:{tenant_id}:w:"
    if workspace_id is None:
        if user_id is not None:
            raise ValueError("a user prefix needs the workspace it is in")
        return base
    if user_id is None:
        return f"{base}{workspace_id}:"
    return f"{base}{workspace_id}:u:{user_id}:s:"


def legacy_checkpoint_thread_prefix(tenant_id, workspace_id) -> str:
    """The prefix of threads written before ADR-0009, which had no user segment.

    Nothing builds these ids any more, so their state is unreachable. Erasure
    removes them, as it always has, rather than leaving content behind that
    nothing can attribute or erase.
    """
    return f"{CHECKPOINT_THREAD_PREFIX}:{tenant_id}:w:{workspace_id}:s:"


# Compiled graph — singleton, initialised at app startup via init_checkpointer()
_checkpointer: BaseCheckpointSaver | None = None
_compiled_graph = None
_pg_checkpointer_ctx = None


async def recorded_checkpoint_version(conn) -> int | None:
    """The newest LangGraph checkpoint migration this database records.

    None when `checkpoint_migrations` does not exist or is empty. `conn` is the
    saver's own psycopg connection, whose rows are dicts.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT to_regclass('checkpoint_migrations') IS NOT NULL AS present")
        row = await cur.fetchone()
        if not row or not row["present"]:
            return None
        await cur.execute("SELECT max(v) AS v FROM checkpoint_migrations")
        row = await cur.fetchone()
        return None if row is None or row["v"] is None else int(row["v"])


async def init_checkpointer(pg_conn_string: str) -> None:
    """
    Initialise the LangGraph checkpointer and compile the agent graph.

    Uses AsyncPostgresSaver (psycopg3) so conversation state survives
    restarts. Falls back to in-memory MemorySaver, with a warning that names
    the cause, when the package is missing or the tables cannot be used.

    ── Why `setup()` is skipped when the tables are current ─────────────────

    LangGraph's `setup()` always starts with `CREATE TABLE IF NOT EXISTS
    checkpoint_migrations`, and PostgreSQL checks CREATE on the schema before it
    looks at IF NOT EXISTS. The runtime role has no CREATE on `public` (migration
    042, on purpose), so `setup()` failed even when every table existed, and
    every native install fell back to memory. Migration 097 creates the tables
    and records LangGraph's migrations, so `setup()` now runs only when the
    recorded version is behind this LangGraph's. Where the role may create
    tables (a superuser in development) that still works; where it may not, the
    warning says to run the migrations.

    Call this once from main.py lifespan startup, after the DB pool is ready.
    """
    global _checkpointer, _compiled_graph, _pg_checkpointer_ctx
    _checkpointer = None
    saver_cls: type[AsyncPostgresSaver] | None
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver as _loaded
        saver_cls = _loaded
    except ImportError as exc:
        _graph_logger.warning(
            f"AsyncPostgresSaver unavailable ({type(exc).__name__}); falling back to in-memory MemorySaver. "
            "Install langgraph-checkpoint-postgres and psycopg[binary] for persistence."
        )
        saver_cls = None

    if saver_cls is not None:
        latest = len(saver_cls.MIGRATIONS) - 1
        recorded, looked = None, False
        try:
            _pg_checkpointer_ctx = saver_cls.from_conn_string(pg_conn_string)
            saver = await _pg_checkpointer_ctx.__aenter__()
            recorded = await recorded_checkpoint_version(saver.conn)
            looked = True
            if recorded is None or recorded < latest:
                await saver.setup()
            elif recorded > latest:
                _graph_logger.warning(
                    f"The checkpoint tables record LangGraph migration {recorded}, newer "
                    f"than this LangGraph's {latest}; using them as they are.")
            _checkpointer = saver
            _graph_logger.info("LangGraph checkpointer: AsyncPostgresSaver (persistent)")
        except Exception as exc:
            await close_checkpointer()
            found = ("the checkpoint tables could not be read" if not looked
                     else "no checkpoint tables" if recorded is None
                     else f"checkpoint tables at LangGraph migration {recorded}")
            _graph_logger.warning(
                f"AsyncPostgresSaver unavailable ({type(exc).__name__}); falling back to in-memory "
                f"MemorySaver. {found[0].upper() + found[1:]}; this LangGraph needs migration {latest}. "
                "Run the database migrations (python -m scripts.migrate apply): "
                "they create the tables, which the application's role may not.")

    if _checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver
        _checkpointer = MemorySaver()

    _compiled_graph = build_graph().compile(checkpointer=_checkpointer)


async def close_checkpointer() -> None:
    """Clean up the AsyncPostgresSaver connection pool on shutdown."""
    global _pg_checkpointer_ctx
    if _pg_checkpointer_ctx is not None:
        try:
            await _pg_checkpointer_ctx.__aexit__(None, None, None)
        except Exception:
            pass
        _pg_checkpointer_ctx = None


def get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        # Synchronous fallback: used only if init_checkpointer() was not called
        from langgraph.checkpoint.memory import MemorySaver
        _graph_logger.warning("get_graph() called before init_checkpointer(); using in-memory checkpointer")
        _compiled_graph = build_graph().compile(checkpointer=MemorySaver())
    return _compiled_graph


def make_initial_state(
    *,
    session_id: str,
    conversation_id: str,
    user_query: str,
    user_id: str,
    is_owner: bool = False,
    mode: str = "assistant",
    is_voice: bool = False,
    message_history: list | None = None,
) -> IRAState:
    # Fix P23: single factory so run_graph() and chat_stream() cannot drift out of sync
    return {
        "messages": [HumanMessage(content=user_query)] if not message_history else message_history,
        "session_id": session_id,
        "conversation_id": conversation_id,
        "user_query": user_query,
        "active_agent": "conversational",
        "use_deep_model": False,
        "mode": mode,
        "memory_context": "",
        "final_response": "",
        "stream_tokens": [],
        "latency_ms": 0,
        "model_used": "qwen3-fast",
        "is_owner": is_owner,
        "clearance_level": "admin" if is_owner else "public",
        "is_voice": is_voice,
        "user_id": user_id,
        "error": None,
    }


async def run_graph(
    principal,
    session_id: str,
    conversation_id: str,
    user_query: str,
    message_history: list | None = None,
    is_owner: bool = False,
    mode: str = "assistant",
    is_voice: bool = False,
    user_id: str = SYSTEM_NAMESPACE,
) -> IRAState:
    """
    Execute the full agent graph for a single user turn.
    Returns the completed state with final_response populated.

    Args:
        is_owner:  True when the request comes from the authenticated admin user
                   (text) or has passed biometric voice verification (voice).
        mode:      "assistant" | "tutor" — persona override from the frontend.
        is_voice:  True when the request originates from the voice pipeline.
    """
    graph = get_graph()

    initial_state = make_initial_state(  # Fix P23: use shared factory
        session_id=session_id,
        conversation_id=conversation_id,
        user_query=user_query,
        user_id=user_id,
        is_owner=is_owner,
        mode=mode,
        is_voice=is_voice,
        message_history=message_history,
    )

    # The principal rides in `configurable`, beside the thread id, so every
    # node in this invocation can bind a workspace and no node can inherit one
    # from a checkpoint written on an earlier turn.
    config = {
        "configurable": {
            "thread_id": checkpoint_thread_id(principal, session_id),
            CONFIG_PRINCIPAL_KEY: principal,
        }
    }

    from core.models.served import recording
    from utils.telemetry import trace_span
    with trace_span("run_graph", {
        "session_id": session_id,
        "user_query_len": len(user_query),
        "is_owner": is_owner,
        "mode": mode,
    }), recording() as served:
        final_state = await graph.ainvoke(initial_state, config=config)

    # Which model answered, as its provider said (finding P1-05). The agents'
    # `model_used` is a tier name they write without asking.
    answered = served.last
    final_state["served_model"] = answered.model if answered else None
    final_state["served_tier"] = answered.tier if answered else None
    return final_state
