"""Health and status endpoints.

Three distinct probes, and the distinction is load-bearing (see
`core/runtime/readiness.py` for the full reasoning):

    GET /livez         is the process alive?      never checks dependencies
    GET /readyz        should it get traffic?     fails closed on any required dep
    GET /health        how is everything?         the existing human/dashboard view
    GET /health/detail per-subsystem breakdown    redacted in production

`/livez` and `/readyz` are public and return no detail worth protecting — a
status word and, on failure, the names of the failed checks. `/health/detail`
exposes subsystem configuration, so in a production-like environment it is
reduced to the same shape as `/health` unless the caller is authenticated.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
import psutil
from fastapi import APIRouter, Response
from pydantic import BaseModel

from config import get_settings
from core.runtime import readiness as _readiness
from core.runtime.build_info import get_build_info
from core.runtime.environment import get_environment
from utils.db import get_pool
from utils.redis_client import get_redis

router = APIRouter(tags=["health"])

logger = logging.getLogger("ira.health")


# ── Liveness ─────────────────────────────────────────────────────────────────

@router.get("/livez", include_in_schema=True)
async def livez() -> dict:
    """Is the process alive and its event loop responsive?

    **Deliberately checks nothing else.** If this returned 503 because
    PostgreSQL was briefly down, an orchestrator would restart every replica —
    losing warm caches, loaded models and in-flight requests — to fix a problem
    in a different service. Reaching this handler at all proves the event loop
    is scheduling work, which is the only thing a restart could repair.
    """
    info = get_build_info()
    return {
        "status": "alive",
        "service": info.service,
        "version": info.version,
        "git_commit": info.git_commit_short,
        "environment": info.environment,
    }


# ── Readiness ────────────────────────────────────────────────────────────────

@router.get("/readyz", include_in_schema=True)
async def readyz(response: Response, verbose: bool = False) -> dict:
    """Should this instance receive traffic?

    Fails closed: any required dependency down, any probe erroring, or the
    instance draining, and the answer is 503. Recovers on its own once the
    dependency returns — no restart involved.

    `verbose=true` includes per-check latency and, since P14-02, the failure
    detector's view. It is suppressed in production-like environments, where an
    unauthenticated caller should not be able to map internal dependency
    topology and timing.

    ── Why the detector's view is worth a separate field ────────────────────

    The body's `status` answers *should I get traffic right now*, and it is a
    statement about **this probe**. `detection` answers *has this dependency
    actually failed, and for how long* — which cannot be read off a single
    evaluation, and is the thing an operator at three in the morning is actually
    asking. A `not_ready` with `postgres` failing for eleven seconds and a
    `not_ready` with `postgres` failing for four hours call for different
    actions, and until P14-02 the surface could not tell them apart.

    Absent when nothing has been observed, rather than present and empty: a key
    whose value is "no failures" reads as "checked, all fine", which is the
    claim a process with no observations is not in a position to make.
    """
    report = await _readiness.evaluate_readiness()
    if not report.ready:
        response.status_code = 503
    allow_verbose = verbose and not get_environment().is_production_like
    body = report.as_dict(verbose=allow_verbose)

    # ── P14-05 FINDING AF ────────────────────────────────────────────────────
    #
    # The truth pass found P14-02's central claim — *"an operator can tell a
    # fresh failure from a long one"* — **false in production**, which is the
    # only place it matters. The detection section rode on `allow_verbose`, and
    # that is suppressed in production-like environments, so the detector ran,
    # observed, declared FAILED, and its view was invisible on the only surface
    # exposing it. Phase 12's exact shape: true at one layer, false at the layer
    # the document describes.
    #
    # The suppression is CORRECT and stays. What changes is that production gets
    # a reduced view rather than none:
    #
    #   * verbose (development): every tracked dependency, with timings.
    #   * production-like: `health` only, and only for dependencies ALREADY
    #     NAMED in the public `failed` list. Those names are disclosed by
    #     `/readyz` regardless, so this adds no topology — and it is exactly the
    #     distinction P14-02 exists to draw: SUSPECTED (a blip) versus FAILED (a
    #     real outage).
    #
    # Timings stay out of the production body. `failing_for_ms` and
    # `last_observed_ms_ago` are the fields that would let an unauthenticated
    # caller measure internal behaviour, and no operator question here needs
    # them badly enough to trade that.
    if not allow_verbose:
        from core.runtime.failure_detector import detector as _detector

        named = set(body.get("failed", []))
        reduced = [{"name": s.name, "health": s.health.value}
                   for s in _detector().report() if s.name in named]
        if reduced:
            body["detection"] = reduced
        return body

    if allow_verbose:
        from core.runtime.failure_detector import detector as _detector

        rows = [
            {
                "name": s.name,
                "health": s.health.value,
                **({"failing_for_ms": s.failing_for_ms}
                   if s.failing_for_ms is not None else {}),
                # Named `threshold_detection_ms`, not `detection_ms`. The
                # shorter name reads as time-to-detect, which it is not — see
                # `failure_detector`'s docstring — and a field name is the part
                # of a measurement most likely to be quoted without its caveat.
                **({"threshold_detection_ms": s.detection_ms}
                   if s.detection_ms is not None else {}),
                **({"last_observed_ms_ago": s.last_observed_ms_ago}
                   if s.last_observed_ms_ago is not None else {}),
            }
            for s in _detector().report()
        ]
        if rows:
            body["detection"] = rows
    return body


class ServiceStatus(BaseModel):
    #: "ok" | "degraded" | "down" | "unknown".
    #:
    #: P13-03 added `unknown`, and the distinction it draws is the point of the
    #: batch: `down` says the DEPENDENCY is not serving, `unknown` says THIS
    #: PROCESS could not find out. Before, a probe that could not even run
    #: reported `down`, which is a confident claim about something nobody
    #: measured — P13-01's third shape, on a health surface.
    status: str
    latency_ms: int


class HealthResponse(BaseModel):
    status: str
    version: str
    services: dict[str, ServiceStatus]
    gpu_vram_used_mb: int | None = None


#: P13-03. The pool and client accessors raise this when nothing has
#: initialised them. That is a WIRING fault in this process, not the dependency
#: being down, and reporting "down" sends an operator to look at a database
#: that is fine. `_UNINITIALISED` is matched on the message because both
#: accessors raise a bare `RuntimeError`; the match is narrow and asserted by a
#: test, so a future rewording fails loudly instead of silently reverting this.
_UNINITIALISED = "not initialised"


def _probe_failure(exc: Exception, started: float) -> ServiceStatus:
    """`unknown` when the probe could not run, `down` when it ran and failed."""
    if isinstance(exc, RuntimeError) and _UNINITIALISED in str(exc).lower():
        logger.error(
            "health probe could NOT RUN (%s) — reporting 'unknown' rather than "
            "'down', because this says nothing about the dependency itself: %s",
            type(exc).__name__, exc)
        return ServiceStatus(status="unknown", latency_ms=0)
    return ServiceStatus(status="down",
                         latency_ms=int((time.monotonic() - started) * 1000))


async def _check_postgres() -> ServiceStatus:
    t = time.monotonic()
    try:
        await get_pool().fetchval("SELECT 1")
        return ServiceStatus(status="ok", latency_ms=int((time.monotonic() - t) * 1000))
    except Exception as exc:  # noqa: BLE001
        return _probe_failure(exc, t)


async def _check_redis() -> ServiceStatus:
    t = time.monotonic()
    try:
        await get_redis().ping()
        return ServiceStatus(status="ok", latency_ms=int((time.monotonic() - t) * 1000))
    except Exception as exc:  # noqa: BLE001
        return _probe_failure(exc, t)


async def _check_vllm(url: str, api_key: str) -> ServiceStatus:
    t = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{url.removesuffix('/v1')}/health",
                                 headers={"Authorization": f"Bearer {api_key}"})
        status = "ok" if r.status_code == 200 else "degraded"
        return ServiceStatus(status=status, latency_ms=int((time.monotonic() - t) * 1000))
    except Exception:
        return ServiceStatus(status="down", latency_ms=0)


@router.get("/health", response_model=HealthResponse)
async def health():
    cfg = get_settings()

    # The model probe follows the engine chat actually uses (utils.llm decides, from
    # LLM_BACKEND). A native install runs Ollama and has no vLLM hosts; probing the
    # Docker defaults (vllm-fast:8001, vllm-deep:8002) reported a healthy install as
    # "degraded" and sent the vLLM key to whatever answered to those names.
    from utils.llm import _use_ollama

    if not _use_ollama():
        pg, rd, fast, deep = await asyncio.gather(
            _check_postgres(),
            _check_redis(),
            _check_vllm(cfg.vllm_fast_url, cfg.vllm_api_key),
            _check_vllm(cfg.vllm_deep_url, cfg.vllm_api_key),
        )
        services = {"postgres": pg, "redis": rd, "vllm_fast": fast, "vllm_deep": deep}
    else:
        pg, rd, model = await asyncio.gather(
            _check_postgres(),
            _check_redis(),
            _check_ollama(getattr(cfg, "ollama_base_url", "http://localhost:11434/v1")),
        )
        services = {"postgres": pg, "redis": rd, "ollama": model}

    all_ok = all(s.status == "ok" for s in services.values())
    overall = "ok" if all_ok else ("degraded" if any(s.status == "ok" for s in [pg, rd]) else "down")

    return HealthResponse(
        status=overall,
        version=cfg.ira_version,
        services=services,
    )


# ── Per-pillar detail (5.3) ───────────────────────────────────────────────────
# Reports each subsystem independently and NEVER raises — every probe degrades to
# a status instead of throwing, so one subsystem being down doesn't break /health
# (and, by the same fail-soft design across the request paths, doesn't break chat).

async def _check_ollama(base_url: str) -> ServiceStatus:
    t = time.monotonic()
    root = base_url.removesuffix("/v1")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{root}/api/tags")
        return ServiceStatus(status="ok" if r.status_code == 200 else "degraded",
                             latency_ms=int((time.monotonic() - t) * 1000))
    except Exception:
        return ServiceStatus(status="down", latency_ms=0)


async def _check_cortex() -> ServiceStatus:
    # Cortex 0.15.2 has no HTTP gateway — IRA shells out to the `cortex` CLI
    # (see cortex_bridge.py). So the probe verifies the engine is ON and the
    # executable is resolvable; we don't run `cortex -z` here (it cold-loads the
    # 14B model and would make /health slow).
    import os
    enabled = os.getenv("IRA_USE_CORTEX", "false").strip().lower() in ("1", "true", "yes", "on")
    if not enabled:
        return ServiceStatus(status="degraded", latency_ms=0)  # off — legacy engine active
    try:
        from cortex_bridge import CortexConfig
        from pathlib import Path
        import shutil
        bin_path = CortexConfig().cortex_bin
        ok = bool(shutil.which(bin_path) or Path(bin_path).exists())
        return ServiceStatus(status="ok" if ok else "down", latency_ms=0)
    except Exception:
        return ServiceStatus(status="down", latency_ms=0)


def _norm(result) -> dict:
    """Normalize a ServiceStatus / leaked exception into a plain status dict.

    P13-03. A probe that leaked an exception past its own guard reports
    `unknown`, not `down`. `down` is a claim about the DEPENDENCY — an operator
    reading it goes and looks at PostgreSQL. `unknown` is a claim about the
    MEASUREMENT, and it is the honest one when the probe itself never ran.
    """
    if isinstance(result, ServiceStatus):
        return {"status": result.status, "latency_ms": result.latency_ms}
    return {"status": "unknown", "latency_ms": 0,
            "detail": f"probe raised {type(result).__name__}"
                      if isinstance(result, BaseException) else "probe produced no result"}


@router.get("/health/detail")
async def health_detail():
    """Independent status for each pillar/subsystem. Always 200; never raises.

    **Reduced in production-like environments.** The full body names embedding
    models, vision models, research backends and action dependencies — a map of
    internal topology that an unauthenticated caller has no reason to hold. In
    development it is exactly as before; in staging and production it collapses
    to per-subsystem status words with the configuration values removed.
    """
    cfg = get_settings()

    pg, rd, brain, cortex = await asyncio.gather(
        _check_postgres(), _check_redis(),
        _check_ollama(getattr(cfg, "ollama_base_url", "http://localhost:11434/v1")),
        _check_cortex(),
        return_exceptions=True,
    )

    # Web-research channels + action deps — both already fail soft.
    try:
        import channels
        research = await channels.doctor()
    except Exception as exc:  # noqa: BLE001
        research = {"error": str(exc)[:80]}
    try:
        from actions import action_status
        actions = action_status(cfg)
    except Exception as exc:  # noqa: BLE001
        actions = {"error": str(exc)[:80]}

    try:
        from utils.llm import vision_available
        vision_ok = vision_available()
    except Exception:
        vision_ok = False

    # P13-02. The observability layer reports its own state, so an operator can
    # ask at runtime instead of inferring it from a startup log line that is
    # not there. `_DETAIL_KEYS` already strips `endpoint` in production-like
    # environments, so the URL does not leak from an unauthenticated surface.
    #
    # `export_confirmed` is carried through as False with its reason attached
    # rather than dropped: a health surface that said "tracing: active" and
    # nothing else would be claiming a signal nobody measured, which is the one
    # thing §5 of the phase plan forbids.
    try:
        from utils.telemetry import telemetry_state

        telemetry = telemetry_state()
    except Exception as exc:  # noqa: BLE001
        telemetry = {"tracing": "unknown", "reason": f"{type(exc).__name__}: {exc}"}

    # P13-05. The objectives, with each one's collection state DERIVED by
    # running its probe against this build. A surface that listed objectives
    # without saying which of them nothing collects would be the green wall
    # §2 of the phase plan forbids — so `alertable: false` travels with them.
    try:
        from core.runtime.slo import register as _slo_register

        objectives = _slo_register()
    except Exception as exc:  # noqa: BLE001
        objectives = [{"error": f"{type(exc).__name__}: {exc}"}]

    pillars = {
        "telemetry": telemetry,
        "objectives": objectives,
        "brain": {"ollama": _norm(brain), "cortex": _norm(cortex)},
        "memory": {"postgres": _norm(pg), "redis": _norm(rd),
                   "embedding_model": getattr(cfg, "embedding_model", "")},
        "vision": {"configured": bool(vision_ok),
                   "model": getattr(cfg, "ollama_vision_model", "")},
        "voice": {"configured": bool(getattr(cfg, "livekit_api_key", "")),
                  "note": "voice runs as a separate service; config presence only"},
        "web_research": research,
        "actions": actions,
    }

    mem_ok = _norm(pg)["status"] == "ok" and _norm(rd)["status"] == "ok"
    brain_ok = _norm(brain)["status"] == "ok" or _norm(cortex)["status"] == "ok"
    overall = "ok" if (mem_ok and brain_ok) else ("degraded" if (mem_ok or brain_ok) else "down")

    if get_environment().is_production_like:
        pillars = _reduce_pillars(pillars)

    return {"status": overall, "version": cfg.ira_version, "pillars": pillars}


#: Keys whose *values* are configuration detail rather than status. Kept out of
#: the production body — model names, endpoint URLs and backend identities map
#: internal topology for anyone who asks.
_DETAIL_KEYS = frozenset({
    "embedding_model", "model", "note", "error", "url", "endpoint", "host",
    "path", "binary", "backend", "backends", "provider",
})


def _reduce_pillars(pillars: dict) -> dict:
    """Strip configuration values, keep status. Used in production-like envs."""
    def _reduce(value):
        if isinstance(value, dict):
            return {
                k: _reduce(v) for k, v in value.items()
                if k not in _DETAIL_KEYS
            }
        if isinstance(value, list):
            return [_reduce(v) for v in value]
        return value

    return {name: _reduce(body) for name, body in pillars.items()}
