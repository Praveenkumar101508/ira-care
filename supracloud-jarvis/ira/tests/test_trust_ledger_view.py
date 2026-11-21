"""Trust Ledger view endpoint (PR 76) — GET /api/v1/trust/ledger, plus the
partial-window verify mode in trust/ledger.py."""
import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import asyncio
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException

import api.routes.trust as trust
from trust.ledger import GENESIS, compute_hash, verify_chain


class _Cfg:
    ira_admin_username = "owner"


def _chain(n=5):
    records, prev = [], GENESIS
    for i in range(1, n + 1):
        body = {"seq": i, "user_id": "owner", "action_id": f"a{i}",
                "task_id": None, "kind": "action", "record": {"n": i},
                "prev_hash": prev}
        h = compute_hash(prev, body)
        records.append({**body, "hash": h, "at": "2026-07-12"})
        prev = h
    return records


# ── partial-window verify ────────────────────────────────────────────────────

def test_partial_window_verifies_without_genesis():
    full = _chain(10)
    window = full[5:]  # recent slice — first is NOT genesis-linked
    assert verify_chain(window, allow_partial=True) == 5
    # strict mode rejects the same window
    with pytest.raises(ValueError, match="broken prev_hash"):
        verify_chain(window)


def test_partial_window_still_detects_tampering():
    window = _chain(6)[2:]
    window[1]["record"] = {"n": 999}
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_chain(window, allow_partial=True)


# ── endpoint ─────────────────────────────────────────────────────────────────
#
# P10-02 changed how this endpoint is reached, and these tests changed with it
# rather than being made to pass.
#
# It used to take `_user` and gate on `is_owner`. That flag is False in the
# cloud edition whatever the username is (3C-04), so "owner-only" meant
# UNREACHABLE there — a different thing that looked the same from the code. On
# the owner's decision the gate is now the verified, server-derived principal
# and the `trust_ledger_read` scope, and the route reads through the module's
# bound functions instead of its own `acquire()` query.
#
# So the fixtures hand it a principal and patch `recent_records` /
# `export_head`. Patching `utils.db.acquire`, as this file used to, would now
# patch a seam the route no longer touches and leave every assertion below
# passing over code that never ran.

import uuid

from core.identity.principal import Principal
from security.roles import SCOPE_TRUST_LEDGER_READ

TENANT = uuid.uuid4()


def _principal(*, scopes=frozenset({SCOPE_TRUST_LEDGER_READ})):
    return Principal(
        user_id=uuid.uuid4(), tenant_id=TENANT, session_id=uuid.uuid4(),
        edition="cloud", roles=frozenset({"admin"}), scopes=frozenset(scopes),
        username="admin@example.test", is_owner=False,
    )


def _patch_reads(monkeypatch, records, head=None):
    async def fake_recent(*, limit=50, principal=None):
        assert principal is not None, "the route read the ledger unbound"
        return list(reversed(records))

    async def fake_head(*, principal=None):
        assert principal is not None, "the route read the head unbound"
        return head or {"seq": len(records), "hash": records[-1]["hash"],
                        "tenant_id": str(TENANT)}

    import trust.ledger as ledger_mod
    monkeypatch.setattr(ledger_mod, "recent_records", fake_recent)
    monkeypatch.setattr(ledger_mod, "export_head", fake_head)


def test_a_role_without_the_ledger_scope_is_refused():
    """`member` and `viewer` hold no `trust_ledger_read`. An audit ledger is
    tenant-wide, and every member reading every colleague's actions is a
    different product from the one the owner agreed to."""
    with pytest.raises(HTTPException) as exc:
        asyncio.run(trust.trust_ledger(principal=_principal(scopes=frozenset())))
    assert exc.value.status_code == 403


def test_the_refusal_does_not_say_which_role_the_caller_holds():
    """One 403 for "wrong role here" and "no ledger scope anywhere", so the
    endpoint cannot be used to map an account's role."""
    with pytest.raises(HTTPException) as exc:
        asyncio.run(trust.trust_ledger(principal=_principal(scopes=frozenset())))
    body = str(exc.value.detail).lower()
    assert "member" not in body and "viewer" not in body


def test_ledger_not_scope_mapped():
    from security.scope_enforcer import PATH_SCOPES, allowed_for_role
    assert not any(p.startswith("/api/v1/trust/ledger") for _, p, _ in PATH_SCOPES)
    for role in ("viewer", "trusted_user", "family_admin", "owner_equivalent"):
        assert allowed_for_role(role, "GET", "/api/v1/trust/ledger") is False


def test_ledger_view_returns_records_and_verifies(monkeypatch):
    records = _chain(5)
    _patch_reads(monkeypatch, records)

    out = asyncio.run(trust.trust_ledger(principal=_principal()))
    assert out["available"] is True
    assert out["status"] == trust.LEDGER_OK
    assert out["count"] == 5
    assert out["verified"] is True
    assert out["records"][0]["seq"] == 1 and out["records"][-1]["seq"] == 5
    assert out["tenant_id"] == str(TENANT)


def test_ledger_view_fail_soft_when_db_down(monkeypatch):
    """Still fail-soft: the console must work exactly when things are broken."""
    async def broken(*_a, **_kw):
        raise RuntimeError("db down")

    import trust.ledger as ledger_mod
    monkeypatch.setattr(ledger_mod, "recent_records", broken)
    monkeypatch.setattr(ledger_mod, "export_head", broken)

    out = asyncio.run(trust.trust_ledger(principal=_principal()))
    assert out["available"] is False
    assert out["status"] == trust.LEDGER_UNAVAILABLE
    assert "db down" in out["reason"]


def test_an_unbound_read_is_not_reported_as_an_empty_ledger(monkeypatch):
    """The defect this batch closes, at the surface that shows it.

    `LedgerNotBound` is our wiring failing, not the database. Rendering it as
    `records: []` produces a clean, empty audit trail — which is exactly what
    the whole endpoint returned in cloud before P10-01.
    """
    from trust.ledger import LedgerNotBound

    async def unbound(*_a, **_kw):
        raise LedgerNotBound("no tenant context")

    import trust.ledger as ledger_mod
    monkeypatch.setattr(ledger_mod, "recent_records", unbound)
    monkeypatch.setattr(ledger_mod, "export_head", unbound)

    out = asyncio.run(trust.trust_ledger(principal=_principal()))
    assert out["status"] == trust.LEDGER_NO_TENANT_CONTEXT
    assert out["status"] != trust.LEDGER_UNAVAILABLE, (
        "a wiring defect was reported as a database outage")
    assert out["records"] == []


def test_a_real_empty_ledger_is_not_an_error(monkeypatch):
    """The fourth answer. A tenant that has genuinely done nothing gets a
    successful, empty response — distinguishable from all three failures."""
    async def empty(*, limit=50, principal=None):
        return []

    async def genesis_head(*, principal=None):
        return {"seq": 0, "hash": GENESIS, "tenant_id": str(TENANT)}

    import trust.ledger as ledger_mod
    monkeypatch.setattr(ledger_mod, "recent_records", empty)
    monkeypatch.setattr(ledger_mod, "export_head", genesis_head)

    out = asyncio.run(trust.trust_ledger(principal=_principal()))
    assert out["available"] is True
    assert out["status"] == trust.LEDGER_OK
    assert out["records"] == [] and out["count"] == 0


def test_ledger_view_reports_tampering(monkeypatch):
    records = _chain(4)
    records[1]["hash"] = "f" * 64  # corrupt
    _patch_reads(monkeypatch, records)

    out = asyncio.run(trust.trust_ledger(principal=_principal()))
    assert out["available"] is True
    assert isinstance(out["verified"], str) and "FAILED" in out["verified"]
