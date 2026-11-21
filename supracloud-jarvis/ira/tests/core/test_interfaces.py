"""Phase 1 — the core interfaces, and the fail-closed behaviour they promise.

These are contract tests. They do not test an implementation (there isn't one
yet); they test that the SEAM cannot be used unsafely — that the placeholder
implementations deny, that a decision object cannot be misread as permission,
and that a storage key cannot escape its tenant.
"""
from __future__ import annotations

import uuid

import pytest

from core.editions import EDITION_PRIVATE
from core.identity import PRIVATE_TENANT_ID, Principal
from core.interfaces import (
    ActionRequest,
    ConnectorCall,
    DenyAllBroker,
    DENY_UNKNOWN,
    Effect,
    Locality,
    PermissionDecision,
    RiskLevel,
    UnavailableConnectorGateway,
    tenant_key,
)
from core.interfaces.model_provider import LOCAL_OLLAMA_DISCLOSURE, ProviderDisclosure


def _principal(**kw) -> Principal:
    base = dict(
        user_id=uuid.uuid4(),
        tenant_id=PRIVATE_TENANT_ID,
        session_id=uuid.uuid4(),
        edition=EDITION_PRIVATE,
    )
    base.update(kw)
    return Principal(**base)  # type: ignore[arg-type]


# ── Permission decisions ─────────────────────────────────────────────────────

def test_action_request_demands_a_purpose():
    """An action whose reason cannot be stated cannot be shown honestly on a
    receipt, so it cannot be requested."""
    with pytest.raises(ValueError, match="purpose"):
        ActionRequest(principal=_principal(), domain="email", action="send", purpose="")


def test_action_request_demands_domain_and_action():
    with pytest.raises(ValueError):
        ActionRequest(principal=_principal(), domain="", action="send", purpose="why")


def test_confirmation_is_not_permission():
    """The bug this property exists to prevent: treating 'ask the user' as
    'go ahead'."""
    d = PermissionDecision(effect=Effect.REQUIRE_CONFIRMATION, reason="needs approval")
    assert d.needs_confirmation is True
    assert d.is_allowed is False


@pytest.mark.parametrize(
    "effect", [Effect.ALLOW, Effect.ALLOW_FOR_SESSION, Effect.ALLOW_FOR_ROUTINE]
)
def test_allow_effects_permit_execution(effect):
    assert PermissionDecision(effect=effect, reason="ok").is_allowed is True


def test_deny_is_not_allowed():
    assert PermissionDecision(effect=Effect.DENY, reason="no").is_allowed is False


def test_fail_closed_default_denies():
    assert DENY_UNKNOWN.effect is Effect.DENY
    assert DENY_UNKNOWN.is_allowed is False
    assert DENY_UNKNOWN.risk is RiskLevel.HIGH


@pytest.mark.asyncio
async def test_placeholder_broker_denies_everything():
    """The safe placeholder denies. Wiring a broker in before Phase 4 implements
    the rules must not accidentally permit anything."""
    broker = DenyAllBroker()
    decision = await broker.decide(
        ActionRequest(
            principal=_principal(is_owner=True),
            domain="email",
            action="send",
            purpose="reply to Sarah",
        )
    )
    assert decision.is_allowed is False
    assert decision.effect is Effect.DENY
    # Even for the owner.
    assert "Phase 4" in decision.reason


# ── Connector gateway ────────────────────────────────────────────────────────

def test_connector_call_demands_a_purpose():
    with pytest.raises(ValueError, match="purpose"):
        ConnectorCall(principal=_principal(), connector="gmail", operation="send", purpose="")


@pytest.mark.asyncio
async def test_placeholder_gateway_refuses_and_registers_nothing():
    gw = UnavailableConnectorGateway()
    result = await gw.call(
        ConnectorCall(
            principal=_principal(is_owner=True),
            connector="gmail",
            operation="send",
            purpose="reply to Sarah",
        )
    )
    assert result.ok is False
    assert result.data is None
    assert gw.registered() == ()


# ── Storage tenancy ──────────────────────────────────────────────────────────

def test_tenant_key_namespaces_by_tenant():
    p = _principal()
    assert tenant_key(p, "session/abc").startswith(f"tenant:{p.tenant_id}:")


def test_tenant_key_rejects_traversal():
    """`../` across a tenant boundary is a cross-tenant read wearing a filename."""
    p = _principal()
    with pytest.raises(ValueError, match="traverse"):
        tenant_key(p, "../other-tenant/secrets")


def test_tenant_key_differs_between_tenants():
    a = _principal(tenant_id=uuid.uuid4())
    b = _principal(tenant_id=uuid.uuid4())
    assert tenant_key(a, "x") != tenant_key(b, "x")


def test_storage_protocols_take_a_principal_not_a_tenant_id():
    """The design property: a caller cannot pass a tenant in, so a browser
    cannot influence one."""
    import inspect

    from core.interfaces.storage import MemoryStore

    for name in ("store", "retrieve", "get", "delete"):
        params = inspect.signature(getattr(MemoryStore, name)).parameters
        assert "principal" in params, f"{name} must take a principal"
        assert "tenant_id" not in params, f"{name} must NOT accept a caller-supplied tenant_id"


# ── Provider disclosure ──────────────────────────────────────────────────────

def test_local_provider_does_not_leave_the_machine():
    assert LOCAL_OLLAMA_DISCLOSURE.locality is Locality.LOCAL
    assert LOCAL_OLLAMA_DISCLOSURE.leaves_the_machine is False
    assert LOCAL_OLLAMA_DISCLOSURE.is_third_party is False


def test_external_provider_is_flagged_as_leaving():
    d = ProviderDisclosure(
        provider_id="x", display_name="X", locality=Locality.EXTERNAL, retains_prompts=None
    )
    assert d.leaves_the_machine is True
    assert d.is_third_party is True


def test_unknown_retention_stays_unknown():
    """`None` must survive as 'not known' — rendering it as 'no' would be a
    privacy claim the system cannot support."""
    d = ProviderDisclosure(
        provider_id="x", display_name="X", locality=Locality.EXTERNAL, retains_prompts=None
    )
    assert d.retains_prompts is None
    assert d.retains_prompts is not False
