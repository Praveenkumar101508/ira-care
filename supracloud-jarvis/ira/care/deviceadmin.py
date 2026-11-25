"""Deployment administrators and the instructions they send to one watch (E2.o, D-085).

Who: an authenticated account (the ordinary sign-in, resolved to a Principal by
`require_tenant_principal`) whose user id holds a LIVE row in `deployment_administrators`
(migration 122). That table is written only by the operator, with the migrator's
credentials (scripts/deployment_admin.py), the same authority that publishes OS releases
(scripts/os_release.py). Nothing else confers it: not a care workspace role (care manager,
coordinator, carer, viewer), not a tenant role (owner, admin), not `Principal.is_owner`. A
revoked grant stops the next request, stops delivery of anything the account issued that the
watch has not yet received, and puts what it issued on the watch's revocation list.

What: `care.devicepolicy.ACTIONS`, nothing else, for one paired watch, signed, single-use,
at most 15 minutes. Every step lands in `care_device_admin_events`: issued and revoked (by
which administrator, with the reason), delivered, and applied, refused or restored (by the
watch).

Where: the administrator names the watch by its tenant, workspace and device id (from the
care organisation's support request; the device id is in the workspace's device list,
GET /care/people/{id}/devices). The work then runs on a connection bound to THAT workspace,
so row-level security still decides what is visible: a device id paired with another tenant's
or workspace's ids is simply not found. The connection touches the watch's own row and the two instruction
tables, and no care content.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Optional

from core.identity.background import background_principal
from core.identity.db_context import MissingWorkspaceContext, tenant_connection, workspace_connection
from core.identity.principal import Principal

from . import devicepolicy

logger = logging.getLogger("ira.care.deviceadmin")

REFUSED = "This needs a deployment administrator."


class NotDeploymentAdmin(PermissionError):
    pass


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


async def is_deployment_admin(conn, user_id: Optional[uuid.UUID]) -> bool:
    """Whether the account holds a live grant. Under the table's policy a live grant is visible
    only on a connection bound to that same account (`app.user_id`), as `tenant_connection` is
    for the caller; asked about anybody else, the answer is no."""
    if user_id is None:
        return False
    return bool(await conn.fetchval(
        "SELECT 1 FROM deployment_administrators WHERE user_id = $1 AND revoked_at IS NULL", user_id))


async def require_deployment_admin(principal: Principal) -> uuid.UUID:
    """The caller's account id if it holds a live grant; otherwise NotDeploymentAdmin. Decided
    only by the grant table: roles, scopes, workspace role and is_owner are never read."""
    async with tenant_connection(principal) as conn:
        ok = await is_deployment_admin(conn, principal.user_id)
    if not ok:
        logger.warning("deployment administrator refused user=%s tenant=%s", principal.user_id, principal.tenant_id)
        raise NotDeploymentAdmin(REFUSED)
    return principal.user_id


def target_principal(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> Principal:
    """A principal bound to the named workspace, with no roles and no scopes (as a watch's own).
    The administrator's identity is recorded in the rows, not carried here."""
    return replace(background_principal(tenant_id, job="deployment-admin"), workspace_id=workspace_id)


@asynccontextmanager
async def _in_workspace(tenant_id: uuid.UUID, workspace_id: uuid.UUID):
    """The named workspace's connection. A tenant and workspace that do not belong together are
    "not found", the same answer as a device id from somewhere else."""
    try:
        async with workspace_connection(target_principal(tenant_id, workspace_id)) as conn:
            yield conn
    except MissingWorkspaceContext as exc:
        raise NotFound("No such workspace in that tenant.") from exc


async def _event(conn, instruction_id, event: str, actor_kind: str, actor_id, detail: str = "") -> None:
    await conn.execute(
        "INSERT INTO care_device_admin_events (instruction_id, event, actor_kind, actor_id, detail)"
        " VALUES ($1, $2, $3, $4, $5)", instruction_id, event, actor_kind, actor_id, detail[:200])


def _public(row) -> dict:
    keep = ("id", "device_id", "action", "nonce", "issued_at", "expires_at", "issued_by", "reason",
            "key_id", "delivered_at", "revoked_at", "revoked_by", "outcome", "outcome_reason", "outcome_at")
    return {k: row[k] for k in keep}


def _ms(t: dt.datetime) -> int:
    return int(t.timestamp() * 1000)


async def issue(*, admin_id: uuid.UUID, tenant_id: uuid.UUID, workspace_id: uuid.UUID, device_id: uuid.UUID,
                action: str, minutes: int, reason: str, key, now: Optional[dt.datetime] = None) -> dict:
    """Sign and store one instruction for one paired watch. ValueError for anything outside the
    allow-list or the 15-minute bound (checked here, again by the watch, and by the table)."""
    if action not in devicepolicy.ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    window_ms = int(minutes) * 60_000
    if not 0 < window_ms <= devicepolicy.MAX_WINDOW_MS:
        raise ValueError("an instruction lasts 1 to 15 minutes")
    now = (now or dt.datetime.now(dt.timezone.utc)).replace(microsecond=0)
    async with _in_workspace(tenant_id, workspace_id) as conn:
        found = await conn.fetchval("SELECT id FROM care_devices WHERE id = $1 AND status = 'paired'", device_id)
        if found is None:
            raise NotFound("No paired watch with that id in that workspace.")
        ins = devicepolicy.new_instruction(str(device_id), action, now_ms=_ms(now), window_ms=window_ms)
        sig = devicepolicy.signed(ins, key)
        row = await conn.fetchrow(
            "INSERT INTO care_device_admin_instructions (device_id, action, nonce, issued_at, expires_at, issued_by,"
            " reason, payload, signature, key_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *",
            device_id, action, ins.nonce, now, now + dt.timedelta(milliseconds=window_ms), admin_id,
            reason.strip(), sig["payload"], sig["signature"], sig["key_id"])
        await _event(conn, row["id"], "issued", "deployment_admin", admin_id, f"{action} for {minutes} min")
    logger.info("device instruction issued id=%s action=%s device=%s by=%s", row["id"], action, device_id, admin_id)
    return _public(row)


async def revoke(*, admin_id: uuid.UUID, tenant_id: uuid.UUID, workspace_id: uuid.UUID,
                 instruction_id: uuid.UUID, reason: str) -> dict:
    async with _in_workspace(tenant_id, workspace_id) as conn:
        row = await conn.fetchrow(
            "UPDATE care_device_admin_instructions SET revoked_at = NOW(), revoked_by = $2"
            " WHERE id = $1 AND revoked_at IS NULL RETURNING *", instruction_id, admin_id)
        if row is None:
            exists = await conn.fetchval("SELECT 1 FROM care_device_admin_instructions WHERE id = $1", instruction_id)
            if exists:
                raise Conflict("That instruction is already revoked.")
            raise NotFound("No such instruction in that workspace.")
        await _event(conn, row["id"], "revoked", "deployment_admin", admin_id, reason.strip())
    logger.info("device instruction revoked id=%s by=%s", instruction_id, admin_id)
    return _public(row)


async def history(*, tenant_id: uuid.UUID, workspace_id: uuid.UUID, device_id: uuid.UUID) -> list[dict]:
    """Every instruction for one watch, newest first, each with its audit trail."""
    async with _in_workspace(tenant_id, workspace_id) as conn:
        rows = await conn.fetch(
            "SELECT * FROM care_device_admin_instructions WHERE device_id = $1 ORDER BY issued_at DESC LIMIT 100",
            device_id)
        events = await conn.fetch(
            "SELECT instruction_id, event, actor_kind, actor_id, detail, at FROM care_device_admin_events"
            " WHERE instruction_id = ANY($1::uuid[]) ORDER BY at, id", [r["id"] for r in rows])
    out = []
    for r in rows:
        item = _public(r)
        item["events"] = [dict(e) for e in events if e["instruction_id"] == r["id"]]
        out.append(item)
    return out


# ── The watch's side (device token; care/routes.py) ──────────────────────────

# An issuer's grant revoked at or after the instruction was issued. The watch's connection can
# see revoked grants and no live ones (migration 122), and this needs only the former: a later
# re-grant is a new row, so what was issued before the revocation stays withdrawn.
_ISSUER_REVOKED = ("EXISTS (SELECT 1 FROM deployment_administrators a"
                   " WHERE a.user_id = i.issued_by AND a.revoked_at >= i.issued_at)")


async def for_device(conn, device_id: uuid.UUID, *, now: Optional[dt.datetime] = None) -> dict:
    """What a watch collects: the live instructions it has not reported on, and the nonces of
    unexpired instructions it must no longer honour (revoked, or issued by an administrator
    whose grant was revoked). A revocation only ever makes the watch MORE restricted."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = await conn.fetch(
        f"SELECT i.*, NOT (i.revoked_at IS NOT NULL OR {_ISSUER_REVOKED}) AS live"  # noqa: S608 - constant fragment
        " FROM care_device_admin_instructions i WHERE i.device_id = $1 AND i.expires_at > $2"
        " ORDER BY i.issued_at", device_id, now)
    deliver, revoked = [], []
    for r in rows:
        if not r["live"]:
            revoked.append(r["nonce"])
            continue
        if r["outcome"] is not None:
            continue
        deliver.append({"id": str(r["id"]), "payload": r["payload"], "signature": r["signature"],
                        "key_id": r["key_id"], "algorithm": devicepolicy.ALGORITHM})
        if r["delivered_at"] is None:
            await conn.execute("UPDATE care_device_admin_instructions SET delivered_at = $2 WHERE id = $1",
                               r["id"], now)
            await _event(conn, r["id"], "delivered", "device", device_id)
    return {"instructions": deliver, "revoked_nonces": revoked}


_NEXT = {None: {"applied", "refused"}, "applied": {"restored"}}


async def record_outcome(conn, device_id: uuid.UUID, instruction_id: uuid.UUID, outcome: str, reason: str) -> dict:
    """The watch reports what it did. Only for its own instruction; applied then restored, or refused."""
    row = await conn.fetchrow(
        "SELECT * FROM care_device_admin_instructions WHERE id = $1 AND device_id = $2", instruction_id, device_id)
    if row is None:
        raise NotFound("No such instruction for this watch.")
    if outcome not in _NEXT.get(row["outcome"], set()):
        raise Conflict(f"Cannot record {outcome!r} after {row['outcome']!r}.")
    row = await conn.fetchrow(
        "UPDATE care_device_admin_instructions SET outcome = $2, outcome_reason = $3, outcome_at = NOW()"
        " WHERE id = $1 RETURNING *", instruction_id, outcome, reason[:200])
    await _event(conn, instruction_id, outcome, "device", device_id, reason)
    return _public(row)
