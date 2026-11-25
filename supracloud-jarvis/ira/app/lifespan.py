"""
Application lifespan — startup and shutdown sequence.

Startup:
  1. Connect to PostgreSQL pool (+ run durable migrations)
  2. Connect to Redis
  3. Warm the BGE embedding model (background, CPU)
  4. Pre-warm the Supertonic TTS engine (background, fail-soft)
  5. Compile the LangGraph agent graph (persistent checkpointer)
  6. Start the optional brain / voice-output / wake-word loops (all OFF by default)

Shutdown: stop the loops, close the checkpointer, DB pool and Redis.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from config import get_settings

# NOTE: DB / Redis / embeddings / graph imports are deliberately lazy (inside the
# functions below). They pull native deps (asyncpg, sentence-transformers) that the
# lightweight test environment stubs out — importing them here would make the app
# factory untestable without the full stack, and startup behavior is unchanged.

logger = logging.getLogger("ira")


def _refuse_published_credentials(cfg) -> None:
    """Refuse to start with a published signing key, admin password or webhook
    secret; warn loudly about a published canary or cortex key.

    The six were committed with real values in July 2026 and are in every clone
    (docs/cloud/SECRET_ROTATION_REGISTER.md). Starting with one of the first
    three means anyone who has seen the repository can become the owner or
    inject webhooks. Names only: no value, or part of one, is ever logged.
    """
    from core.runtime.published_credentials import REFUSED, published_in_use

    found = published_in_use(cfg)
    for name in found:
        logger.critical("%s holds a value published in this repository's history; "
                        "rotate it (docs/security/CREDENTIAL_ROTATION.md)", name)
    refused = [name for name in found if name in REFUSED]
    if refused:
        raise RuntimeError(
            f"IRA will not start: {', '.join(refused)} still hold(s) a value published "
            "in this repository's history, which anyone with a copy of the repository "
            "can use. Replace it in .env (docs/security/CREDENTIAL_ROTATION.md), then "
            "start IRA again.")


async def _validate_config(cfg) -> None:
    """Warn about common misconfiguration issues at startup.

    One thing is refused rather than warned about: a credential that still holds
    a value published in this repository's history (`core.runtime.published_credentials`).
    """
    _refuse_published_credentials(cfg)
    warnings = []

    if cfg.owner_name in ("CHANGE_ME_your_name", "", "Change Me", "Your Name Here"):
        warnings.append("OWNER_NAME is not set to your real name")

    if cfg.ira_admin_password in ("CHANGE_ME_strong_password_here", "admin", "password"):
        warnings.append("IRA_ADMIN_PASSWORD is using the default placeholder — change it!")

    if cfg.ira_secret_key in ("CHANGE_ME_ira_secret_key_here", ""):
        warnings.append("IRA_SECRET_KEY is not set — JWT security is broken!")

    if not getattr(cfg, "telegram_bot_token", None) and not getattr(cfg, "smtp_host", None):
        warnings.append("No notification channel configured (Telegram or SMTP) — alerts will not be delivered")

    for w in warnings:
        logger.warning(f"⚠️  CONFIG WARNING: {w}")

    if any("broken" in w.lower() or "not set" in w.lower() for w in warnings):
        logger.warning("Fix the above config warnings before going to production.")

    # Sovereignty guard: if the Cortex engine is ON, its gateway MUST be local or
    # prompts could leave the box via a remote gateway. Warn loudly; never block.
    from config import cortex_local_only_warning, research_backends_warning
    _cortex_leak = cortex_local_only_warning()
    if _cortex_leak:
        logger.warning("=" * 70)
        logger.warning("⚠️  CORTEX SOVEREIGNTY WARNING  ⚠️")
        logger.warning(f"   {_cortex_leak}")
        logger.warning("   Point IRA_CORTEX_URL at 127.0.0.1/localhost unless you")
        logger.warning("   truly intend to route through a remote (cloud) gateway.")
        logger.warning("=" * 70)

    # Same for the web-research backends (SearXNG / Crawl4AI) — keep them local.
    _research_leak = research_backends_warning()
    if _research_leak:
        logger.warning("=" * 70)
        logger.warning("⚠️  WEB-RESEARCH SOVEREIGNTY WARNING  ⚠️")
        logger.warning(f"   {_research_leak}")
        logger.warning("   Point SEARXNG_URL / CRAWL4AI_URL at local/self-hosted endpoints.")
        logger.warning("=" * 70)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from utils.db import init_pool, close_pool
    from utils.redis_client import init_redis, close_redis
    from agents.graph import init_checkpointer, close_checkpointer

    cfg = get_settings()

    # ── Cloud runtime foundation (Phase 1) ────────────────────────────────────
    #
    # Ordering here is deliberate and each step depends on the one before it:
    #
    #   1. Register secret VALUES for redaction — before any logging happens, so
    #      no startup line can print one.
    #   2. Configure structured logging — before validation, so a validation
    #      failure is emitted as a parseable, redacted record rather than a bare
    #      print to stderr.
    #   3. Validate configuration and RAISE in staging/production — before any
    #      connection is opened, so a misconfigured instance never touches a
    #      database with credentials it should not have started with.
    #
    # In development and test, step 3 is a no-op and steps 1-2 change only the
    # log format. Nothing here alters behaviour for an existing private install.
    from core.runtime import environment as _env
    from core.runtime.logging_config import configure_logging
    from core.runtime.redaction import register_secrets_from_settings

    registered = register_secrets_from_settings(cfg)
    configure_logging(service="ira-api")
    logger.info("log redaction active", extra={"registered_secrets": registered})

    resolved_env = _env.get_environment()
    # Raises ConfigurationError in staging/production when anything is unsafe.
    # The message names the settings and never their values.
    _env.enforce(cfg, resolved_env)
    logger.info("environment resolved", extra={"environment": resolved_env.value})

    # Initialise OpenTelemetry (no-op if OTLP_ENDPOINT not set)
    from utils.telemetry import setup_telemetry
    setup_telemetry(service_name="ira-api")

    if cfg.dev_mode:
        # Fix P9: refuse to start DEV_MODE against a public domain to prevent
        # accidentally exposing a fully open admin endpoint on the internet.
        if not cfg.is_local_domain:
            raise RuntimeError(
                f"DEV_MODE=true is set but IRA_DOMAIN={cfg.ira_domain!r} is not a "
                "localhost/private address. Refusing to start — this would expose an "
                "unauthenticated admin endpoint on a public domain. "
                "Set DEV_MODE=false or use a local domain."
            )
        logger.warning("=" * 70)
        logger.warning("⚠️  DEV_MODE ENABLED — AUTH AND BIOMETRICS ARE DISABLED  ⚠️")
        logger.warning("   - Authentication bypassed (any token accepted)")
        logger.warning("   - Biometric gate disabled (all requests treated as owner)")
        logger.warning("   - All LLM calls routed to local Ollama")
        logger.warning("   NEVER USE IN PRODUCTION!")
        logger.warning("=" * 70)
    logger.info(f"IRA {cfg.ira_version} starting up...")

    # Initialise connections
    await init_pool()
    logger.info("PostgreSQL pool ready")

    # ── The application no longer applies migrations ─────────────────────────
    #
    # It used to, on every boot, through this pool. That one line is why the
    # runtime role owned every table: DDL requires ownership, so the role
    # serving requests could also disable forced row-level security, drop a
    # policy, or reassign ownership. FORCE constrained the application's
    # queries, not its role (D-18, R-50).
    #
    # Migrations are now a deployment step — `python -m scripts.migrate apply`
    # over a separate migrator DSN. Startup only INSPECTS, and reports
    # not-ready when the schema is behind. A pending migration becomes a
    # visible deployment failure instead of an invisible privilege.
    from utils.db import get_pool
    from utils.migrations import migration_status

    status = await migration_status(get_pool())
    if status["up_to_date"]:
        logger.info("migrations: schema up to date (%d applied)", status["applied"])
    else:
        logger.error(
            "migrations: %d PENDING — this instance will report NOT READY. "
            "Run `python -m scripts.migrate apply --dsn <migrator>` as a "
            "deployment step; the application must not apply them itself.",
            len(status["pending"]),
            extra={"pending": status["pending"][:10]},
        )

    # What this process is allowed to do in the database, asserted rather than
    # assumed. Raises in staging and production; logs elsewhere.
    from core.runtime.db_identity import enforce as enforce_db_identity

    identity = await enforce_db_identity(get_pool())
    logger.info(identity.summary())

    await _provision_private_owner()

    await init_redis()
    logger.info("Redis connection ready")

    await _seed_token_versions()

    # The Trust Ledger's fallback journal (P1-09): verify its chain and replay
    # what it holds now that the database is up. In the background, and it
    # never raises: a broken chain is logged at CRITICAL, recorded as a
    # security event and shown in /api/v1/trust/ledger/health, not a reason to
    # keep IRA down.
    from trust.journal import startup_reconcile

    def _journal_checked(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("Trust Ledger journal startup check failed: %s", task.exception())

    _tj = asyncio.create_task(startup_reconcile(), name="ledger-journal-startup")
    _tj.add_done_callback(_journal_checked)

    # Warm embedding model in background — don't block startup
    _t = asyncio.create_task(_warm_embeddings())
    _t.add_done_callback(lambda t: t.exception() and logger.warning(f"Embedding warm-up failed: {type(t.exception()).__name__}"))

    # Pre-warm the on-device Supertonic TTS so the first POST /voice/say is not cold.
    _ts = asyncio.create_task(_warm_supertonic())
    _ts.add_done_callback(lambda t: t.exception() and logger.warning(f"Supertonic warm-up failed: {type(t.exception()).__name__}"))

    # Initialise LangGraph checkpointer (AsyncPostgresSaver → persistent state)
    await init_checkpointer(cfg.database_dsn)
    logger.info("LangGraph agent graph compiled")

    await _validate_config(cfg)

    # Continuous realtime brain — persistent background loop (OFF by default).
    from api.routes.brain import start_brain, stop_brain
    await start_brain(app)

    # Local voice output — speak brain replies aloud (OFF by default). After the brain.
    from voice.voice_output import start_voice_output, stop_voice_output
    await start_voice_output(app, getattr(app.state, "brain", None))

    # Always-on, owner-gated wake-word listener (OFF by default).
    from voice.wakeword import start_wakeword, stop_wakeword
    await start_wakeword(app)

    # ── Background retention (P4-11) ─────────────────────────────────────────
    #
    # Off unless IRA_SCHEDULER_ENABLED is set. A background deleter that started
    # by default would begin removing data the first time somebody ran this
    # locally against a copy of production, which is a thing people do.
    #
    # Started LAST, after every dependency it needs is up: a pass that begins
    # before the pool exists fails, records `failed`, and makes the first entry
    # in the job history a lie about the deployment rather than about the data.
    _retention_stop = None
    _retention_task = None
    from core.jobs import scheduler as _scheduler

    if _scheduler.enabled_in_api():
        _retention_stop = asyncio.Event()
        _retention_task = asyncio.create_task(
            _scheduler.run_forever(_retention_stop), name="retention-scheduler")
        logger.info("retention scheduler enabled")
    elif _scheduler.enabled():
        # Enabled, but somewhere else. Saying so matters: an operator who sees
        # "disabled" here and no worker deployed has been told retention is off
        # when it is on, and the reverse mistake is worse.
        logger.info("retention scheduler runs in a dedicated worker (%s is false)",
                    _scheduler.IN_API_ENV)
    else:
        logger.info("retention scheduler disabled (%s is not set)",
                    _scheduler.ENABLED_ENV)

    logger.info("IRA is online. Good morning.")
    yield

    # The scheduler stops FIRST, before the pool it needs is closed. A pass
    # still deleting when the pool goes would raise mid-tenant and leave a
    # `running` row for the stale sweep — safe, because the work is idempotent,
    # but a clean shutdown should not need cleaning up after.
    if _retention_task is not None:
        _retention_stop.set()
        try:
            await asyncio.wait_for(_retention_task, timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning("retention scheduler did not stop in time; cancelling")
            _retention_task.cancel()
        except Exception:  # noqa: BLE001
            logger.exception("retention scheduler failed during shutdown")

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    #
    # Two changes from the previous sequence, both about not losing work:
    #
    #   1. Draining is announced FIRST. `/readyz` starts returning 503 before a
    #      single resource is closed, so the load balancer stops sending new
    #      requests while the in-flight ones still have their database pool.
    #      Closing the pool first would fail the requests we are trying to
    #      protect.
    #
    #   2. Every step is bounded and independently guarded. Previously one
    #      hanging `close()` would hang the whole shutdown until the orchestrator
    #      SIGKILLed the process — losing every remaining step, including the
    #      database pool. Now a step that hangs is abandoned after its timeout
    #      and the rest still run.
    #
    # Errors are logged, never swallowed silently: a shutdown that fails to
    # close a pool is a connection leak worth knowing about.
    from core.runtime.readiness import begin_draining

    begin_draining()
    logger.info("draining — /readyz now reports not-ready")

    await _shutdown_sequence([
        ("wakeword", lambda: stop_wakeword(app)),
        ("voice_output", lambda: stop_voice_output(app)),
        ("brain", lambda: stop_brain(app)),
        ("checkpointer", close_checkpointer),
        ("postgres_pool", close_pool),
        ("redis", close_redis),
    ])
    logger.info("IRA shutting down. Goodbye.")


async def _provision_private_owner() -> None:
    """Give the private edition's configured owner their identity rows.

    `023_tenancy.sql` seeds the membership rows that workspace verification
    reads only for the default username, and says in as many words that other
    deployments get theirs "from the application on first login"; this is
    that. Idempotent, private-edition only, and non-fatal: a process that
    cannot provision should still serve the endpoints that need no workspace,
    and the ones that do will refuse individually rather than the whole
    instance failing to start.

    One failure is fatal on purpose: `ReservedOwnerLogin`, a new install whose
    owner login is a name IRA reserves for its own memory. Starting would file
    the owner's conversations where deletion cannot reach them, and choosing
    another login before the first start costs nothing.
    """
    from core.identity.provisioning import (
        ReservedOwnerLogin,
        ensure_private_owner_identity,
    )

    try:
        await ensure_private_owner_identity()
    except ReservedOwnerLogin:
        raise
    except Exception as e:
        logger.error(
            "private owner identity provisioning failed — workspace-scoped "
            "endpoints will refuse until this is resolved: %s", type(e).__name__,
        )


async def _seed_token_versions() -> None:
    """Carry pre-096 Redis counters into PostgreSQL; create legacy account rows;
    purge counters of accounts erased more than seven days ago.

    Idempotent and run on every start (migration 096): the table keeps the
    higher of its value and any surviving `ira:token_ver:*` key, so a "log out
    everywhere" made before the upgrade is not undone. Non-fatal: the table is
    the authority either way, and an account the seeder could not reach is
    refused (401), never passed.
    """
    from core.identity.token_versions import seed_from_redis

    try:
        report = await seed_from_redis()
    except Exception as e:  # noqa: BLE001 - logged; requests still fail closed
        logger.error("token-version seeding failed; legacy accounts stay refused: %s", e)
        return
    logger.info(
        "token versions seeded: %d legacy account row(s) created, Redis %s, "
        "%d counter(s) seen, %d raised, %d without an account",
        report.legacy_rows_created, report.redis, report.counters_seen,
        report.counters_raised, len(report.unmatched),
    )
    # Single-device sign-outs recorded only in Redis before R08 (private
    # edition): copied into PostgreSQL, so they survive losing Redis.
    from core.identity.f1_revocation import seed_revocations_from_redis

    try:
        copied, state = await seed_revocations_from_redis()
        logger.info("sign-out records copied from Redis: %d (Redis %s)", copied, state)
    except Exception as e:  # noqa: BLE001 - logged; those tokens stay revoked in Redis
        logger.error("sign-out records could not be copied from Redis: %s", e)
    # Retention (migration 096, decision D). Every install runs this; the daily
    # worker job runs only where a worker does. Non-fatal: a counter kept
    # longer is a retention lapse, never a security one.
    from core.identity.token_versions import purge_erased

    try:
        purged = await purge_erased()
        logger.info("token-version retention at start: %d counter(s) purged", purged)
    except Exception as e:  # noqa: BLE001
        logger.error("token-version retention at start failed: %s", e)


async def _shutdown_sequence(steps) -> None:
    """Run each shutdown step under its own timeout, in order.

    In order rather than concurrently: the checkpointer writes to the pool, so
    closing the pool first would make the checkpointer's final flush fail. The
    sequence encodes that dependency.
    """
    from core.runtime.timeouts import SHUTDOWN_TASK_TIMEOUT, shutdown_timeout

    deadline = asyncio.get_running_loop().time() + shutdown_timeout()

    for name, step in steps:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            logger.error("shutdown budget exhausted; skipping remaining steps",
                         extra={"skipped_from": name})
            return
        try:
            await asyncio.wait_for(step(), timeout=min(SHUTDOWN_TASK_TIMEOUT, remaining))
            logger.info("shutdown step complete", extra={"step": name})
        except asyncio.TimeoutError:
            logger.error("shutdown step timed out", extra={"step": name})
        except asyncio.CancelledError:
            # The process is going away regardless; record it and keep closing
            # what we still can rather than propagating and losing the rest.
            logger.warning("shutdown step cancelled", extra={"step": name})
        except Exception:  # noqa: BLE001
            logger.error("shutdown step failed", extra={"step": name}, exc_info=True)


async def _warm_embeddings():
    """Load the embedding and reranker models once, OFF THE EVENT LOOP.

    ── FINDING CM ──────────────────────────────────────────────────────────

    `preload_model()` is `sentence_transformers` resolving a model and loading
    its weights: hundreds of megabytes, a network fetch when the cache is cold,
    and **entirely synchronous**. It used to be called directly here.

    `asyncio.create_task` keeps that off the STARTUP PATH, which is what the
    call site's comment promises and is where the reasoning stopped. It does
    nothing about the event loop. A synchronous call inside a task still owns
    the only thread that serves requests, so for the whole load this process
    accepted connections and answered none of them.

    Measured on this tree, no CPU contention, cold cache, `/readyz` at
    concurrency 16:

        before   median 219 ms   p95 30 072 ms   96 of 200 requests UNANSWERED
        after    median  61 ms   p95    212 ms    0 of 200 unanswered

    The 30 072 ms is the client timeout, not a latency — the loop was gone.

    That is not only a benchmark artefact. An instance in this state passes
    `/livez` and `/readyz` — both answered before the load began — is put into
    the load balancer, and then stalls every request it is given. The
    benchmark found it because it was the only thing pointing concurrency at a
    freshly started process.

    `_warm_supertonic` below already had this right. The difference between
    the two functions was the whole defect.
    """
    await asyncio.sleep(2)  # Let the server finish starting first
    try:
        from memory.embeddings import preload_model
        await asyncio.to_thread(preload_model)
        logger.info("BGE embedding model warmed and ready")
    except Exception as e:
        logger.warning(f"Embedding model warm-up failed: {type(e).__name__}")

    # A2: warm the reranker cross-encoder too (only if enabled) so the first
    # memory query isn't slowed by the one-time model load. Also off the loop:
    # the reranker is the larger of the two and was the one that stalled.
    cfg = get_settings()
    if cfg.reranker_enabled:
        try:
            from memory.reranker import preload_model as preload_reranker
            await asyncio.to_thread(preload_reranker)
            logger.info("BGE reranker model warmed and ready")
        except Exception as e:
            logger.warning(f"Reranker model warm-up failed: {type(e).__name__}")


async def _warm_supertonic():
    """Load the on-device Supertonic TTS engine once at startup (fail-soft).

    Runs the blocking model load off the event loop. If Supertonic isn't installed
    (e.g. a text-only deployment), this is a no-op and POST /voice/say returns 503
    until the engine is available — it never blocks or fails startup.
    """
    await asyncio.sleep(2)  # let the server finish starting first
    try:
        from voice.tts_supertonic import prewarm
        ready = await asyncio.to_thread(prewarm)
        if ready:
            logger.info("Supertonic TTS engine warmed and ready")
        else:
            logger.info("Supertonic TTS not available — /voice/say will 503 until installed")
    except Exception as e:
        logger.info(f"Supertonic warm-up skipped: {type(e).__name__}")
