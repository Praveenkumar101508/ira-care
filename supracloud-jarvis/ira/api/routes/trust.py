"""
Trust Console v1 — GET /api/v1/trust/status.

One owner-facing endpoint that answers the question "is IRA actually local and
safe right now?" in a single glance:

  - privacy mode + external-API master switch (+ consent flag)
  - where the model runs (backend, URL, is it local?)
  - where the database / memory lives (is it local?)
  - voice profile enrollment state
  - pending approval-gated actions
  - last successful owner login
  - active security warnings (placeholder secrets, DEV_MODE, sovereignty leaks)

Every probe is fail-soft: a subsystem being down yields "unknown" for that
field, never a 500 — the console must work exactly when things are broken.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Depends

from api.middleware.auth import is_owner, require_auth, require_owner
from api.middleware.workspace import require_tenant_principal
from core.identity.principal import Principal
from security.roles import SCOPE_TRUST_LEDGER_READ
from config import (
    get_settings,
    cortex_local_only_warning,
    research_backends_warning,
    _is_local_or_private_host,
)

logger = logging.getLogger("ira.trust")

router = APIRouter(prefix="/trust", tags=["trust"])

LAST_LOGIN_KEY = "ira:last_login"


def _url_is_local(url: str) -> bool:
    return _is_local_or_private_host(urlparse(url).hostname or "")


async def _voice_enrolled() -> bool | None:
    """True/False from the voice_profiles table; None when the DB is unreachable."""
    try:
        from utils.db import acquire
        async with acquire() as conn:
            row = await conn.fetchrow("SELECT 1 FROM voice_profiles LIMIT 1")
        return row is not None
    except Exception:
        return None


async def _last_login() -> str | None:
    """ISO timestamp of the last successful login (recorded by /auth/token)."""
    try:
        from utils.redis_client import get_redis
        raw = await get_redis().get(LAST_LOGIN_KEY)
        return raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
    except Exception:
        return None


def _pending_actions() -> int | None:
    try:
        from utils.approval import guardrail
        return guardrail.pending_count()
    except Exception:
        return None


async def _inference_capacity() -> dict | None:
    """What inference tier this machine can carry right now (reasoning/capacity.py).

    Informational: it changes no routing. The record reads memory and CPU facts
    and, only when the owner has set IRA_CAPACITY_PROBE=1, runs one short cached
    generation probe through Ollama; that is blocking I/O, so it runs in a
    worker thread, never on the event loop. Any failure yields None.
    """
    try:
        from reasoning.capacity import capacity_record
        return await asyncio.to_thread(capacity_record)
    except Exception:
        return None


def _security_warnings(cfg) -> list[str]:
    warnings: list[str] = []
    if cfg.dev_mode:
        warnings.append("DEV_MODE is ON — auth and biometrics are bypassed")
    if cfg.ira_secret_key in ("CHANGE_ME_ira_secret_key_here", ""):
        warnings.append("IRA_SECRET_KEY is a placeholder — JWT security is broken")
    if cfg.ira_admin_password in ("CHANGE_ME_strong_password_here", "admin", "password"):
        warnings.append("IRA_ADMIN_PASSWORD is a default placeholder")
    from core.runtime.published_credentials import published_in_use

    for name in published_in_use(cfg):
        warnings.append(f"{name} holds a value published in this repository's history: rotate it")
    if cfg.ira_allow_external_api:
        warnings.append("IRA_ALLOW_EXTERNAL_API is ON — cloud API calls are permitted")
    _cortex = cortex_local_only_warning()
    if _cortex:
        warnings.append(_cortex)
    _research = research_backends_warning()
    if _research:
        warnings.append(_research)
    if not _url_is_local(cfg.ollama_base_url):
        warnings.append(
            f"OLLAMA_BASE_URL={cfg.ollama_base_url!r} is not local — prompts would leave the box"
        )
    return warnings


#: Warnings about the owner's own credentials. "The admin password is one of
#: three published values" and "the signing key is the published placeholder"
#: are each enough to become the owner, so only the owner is told. The same
#: holds for the four other credentials whose values were published in July
#: 2026: "the webhook secret is the published one" tells a reader how to forge
#: a webhook, and "the canary is the published one" how to avoid the tripwire.
_CREDENTIAL_WARNINGS = ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "WEBHOOK_SECRET",
                        "IRA_CORTEX_KEY", "IRA_CANARY_TOKEN", "IRA_CANARY_USERNAME")


@router.get("/status")
async def trust_status(_user: str = Depends(require_auth)):
    """Snapshot of IRA's privacy / locality posture.

    The owner sees every warning. Household roles holding `settings_read` see
    the posture but not the warnings about the owner's credentials, and their
    status is computed from what they are shown, so it cannot hint at one.
    """
    cfg = get_settings()

    model_local = _url_is_local(cfg.ollama_base_url)
    db_local = _is_local_or_private_host(cfg.postgres_host)
    warnings = _security_warnings(cfg)
    if not is_owner(_user):
        warnings = [w for w in warnings if not w.startswith(_CREDENTIAL_WARNINGS)]

    external_off = not cfg.ira_allow_external_api and not cfg.web_search_enabled
    all_local = model_local and db_local
    status = "local_only" if (external_off and all_local and not warnings) else "attention"

    return {
        "status": status,
        "privacy": {
            "mode": cfg.ira_privacy_mode,
            "external_api_allowed": cfg.ira_allow_external_api,
            "api_consent_required": cfg.ira_require_api_consent,
            "web_search_enabled": cfg.web_search_enabled,
        },
        "model": {
            "backend": cfg.llm_backend,
            "base_url": cfg.ollama_base_url,
            "local": model_local,
            "profile": cfg.ira_model_profile,
        },
        "database": {
            "host": cfg.postgres_host,
            "local": db_local,
        },
        "memory": {
            "store": "postgres+pgvector",
            "local": db_local,
            "embedding_model": cfg.embedding_model,
            "embedding_device": cfg.embedding_device,
        },
        "voice_profile": {
            "enrolled": await _voice_enrolled(),
        },
        "pending_actions": _pending_actions(),
        "last_login": await _last_login(),
        "security_warnings": warnings,
        "inference_capacity": await _inference_capacity(),
    }


# ── Trust Ledger view (PR 76) ────────────────────────────────────────────────

#: Why a ledger view has no records. Four distinct answers that used to be one.
#:
#: The old response had `available: true/false` and a free-text `reason`, so a
#: client could not tell an empty ledger from an unbound read — and until
#: P10-01 the unbound read is what it always got. A caller rendering "no
#: records" from either is showing a clean audit trail that was never consulted.
LEDGER_OK = "OK"
LEDGER_NO_TENANT_CONTEXT = "NO_TENANT_CONTEXT"
LEDGER_UNAVAILABLE = "LEDGER_UNAVAILABLE"


@router.get("/ledger")
async def trust_ledger(
    action_id: str | None = None,
    limit: int = 50,
    principal: Principal = Depends(require_tenant_principal),
):
    """This TENANT's Trust Ledger: its hash-chained records and whether the
    chain verifies.

    ── Who may read it (P10-02, owner decision B) ──────────────────────────

    A tenant administrator may read their **own** tenant's ledger. A cloud
    customer with an audit trail nobody on their side can inspect has one in
    name only.

    The gate is no longer `is_owner`. That flag is `False` in the cloud edition
    whatever the username is (3C-04), so this endpoint was not "owner-only" in
    cloud — it was **unreachable**, which is a different thing that looked the
    same from the code. Authority now comes from the verified, server-derived
    principal and the role→scope map: `primary_owner`, `owner`, `admin` and the
    read-only `security_admin` hold `trust_ledger_read`; `member`, `viewer` and
    `billing_admin` do not.

    The tenant is **never** taken from the request. `require_tenant_principal`
    resolves it and verifies membership (P9-03), and `trust_ledger`'s FORCE RLS
    is the boundary underneath — the scope check is a legible early refusal on
    top of it, not instead of it.

    ── Four answers, not two ───────────────────────────────────────────────

    Still fail-soft: the console must work exactly when things are broken. But
    "broken" is now distinguishable from "empty":

      * `status: OK`, `records: []`        — this tenant really has no records
      * **403**                            — not authorised (raised, not a body)
      * `status: NO_TENANT_CONTEXT`        — a wiring defect, never an empty list
      * `status: LEDGER_UNAVAILABLE`       — the database could not be reached

    A single `available: false` covered the last three, and `available: true`
    with `[]` covered the first two.
    """
    if not principal.has_scope(SCOPE_TRUST_LEDGER_READ):
        from fastapi import HTTPException

        # The same 403 whether the caller is a member of this tenant with the
        # wrong role or holds no ledger scope anywhere. Nothing in the body
        # says which, so it cannot be used to map the role of an account.
        logger.warning("trust ledger read refused user=%s tenant=%s",
                       principal.user_id, principal.tenant_id)
        raise HTTPException(status_code=403,
                            detail="Reading the Trust Ledger requires an "
                                   "administrator or audit role in this tenant.")

    from trust.ledger import (
        LedgerNotBound,
        export_head,
        recent_records,
        records_for,
        verify_chain,
    )

    try:
        if action_id:
            records = await records_for(action_id, principal=principal)
        else:
            # Was a bare `acquire()` query written out here rather than a call
            # to the module. P10-01 bound the module's reads and this survived,
            # because a second copy of a query is a second thing to fix. It is
            # the module's function now, with the module's binding.
            records = list(reversed(await recent_records(
                limit=max(1, min(int(limit), 200)), principal=principal)))
        head = await export_head(principal=principal)
    except LedgerNotBound as exc:
        # Not an outage. Reaching here means the dependency produced a principal
        # and the ledger still could not tell whose records these are, which is
        # a defect in us — reported as itself rather than as an empty ledger.
        logger.error("TRUST LEDGER VIEW HAS NO TENANT CONTEXT: %s", exc)
        return {"available": False, "status": LEDGER_NO_TENANT_CONTEXT,
                "reason": str(exc), "records": [], "count": 0,
                "tenant_id": str(principal.tenant_id)}
    except Exception as exc:  # noqa: BLE001 — the console works when things break
        logger.warning("trust ledger view unavailable: %s", exc)
        return {"available": False, "status": LEDGER_UNAVAILABLE,
                "reason": str(exc), "records": [], "count": 0,
                "tenant_id": str(principal.tenant_id)}

    verified = None
    try:
        verify_chain(records, allow_partial=True)
        verified = True
    except Exception as exc:  # tampering or gap — report, do not crash
        verified = f"FAILED: {exc}"
    return {"available": True, "status": LEDGER_OK, "records": records,
            "head": head, "verified": verified, "count": len(records),
            "tenant_id": str(principal.tenant_id)}


# ── The Trust Ledger's fallback journal (P1-09) ──────────────────────────────


@router.get("/ledger/health")
async def trust_ledger_health(_owner: str = Depends(require_owner)):
    """Whether every confirmed action is in the Trust Ledger, and if not, why.

    Owner-only, through the one owner dependency every owner route uses. The
    fallback journal is one file for the whole process, not a tenant's: its
    backlog and its path are the deployment's, so a tenant administrator who
    may read their own ledger (`GET /trust/ledger`) is not shown them.

    Reports, from `trust.journal.health_snapshot`:

      * `backlog` / `backlog_actions` — records, and the actions they belong
        to, held in the journal and not yet replayed into the ledger;
      * `chain_verified` — whether the journal's HMAC chain verifies (`null`
        when there is no journal), and `chain_error` when it does not;
      * `torn_tail` — the journal ends with a write cut short;
      * `last_ledger_failure_at`, `last_journal_failure_at`,
        `unrecorded_executions` — what this process has seen fail;
      * `journal_available` / `journal_error` — whether the journal could be
        used now (permissions, platform, key);
      * `warning` and `message` — what the Trust Ledger panel shows.

    Read-only: it verifies the journal and changes nothing, so it is safe to
    poll. A backlog it finds is replayed at the next ledger write.
    """
    from trust.journal import health_snapshot

    return await asyncio.to_thread(health_snapshot)
