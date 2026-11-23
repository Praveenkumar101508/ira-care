"""A care organisation can start on its own: sign-up, the organisation, inviting
family by link with consent captured at invitation, and the plan.

Everything here reuses IRA's identity and billing: `register_account` and
email verification, `provision_tenant`, `tenant_invitations`, the billing
plans and entitlements. Email goes through `core/notify/mail`, which is
log-only (console or null) unless an SMTP host is configured. The payment
provider is off unless configured and refuses test mode in production.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import uuid
from typing import Optional
from urllib.parse import quote

from core.identity.db_context import workspace_connection
from core.identity.principal import Principal

from . import repository as repo

logger = logging.getLogger("ira.care.onboarding")

#: Circle roles an invitation may carry, and the tenant role each becomes.
INVITABLE_ROLES = {"family": "member", "carer": "member", "clinician": "member", "care_organisation": "member"}
PLAN_KEYS = ("care_people_max", "care_circle_members_max")


class OnboardingRefused(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def public_base_url() -> str:
    return (os.getenv("IRA_CARE_PUBLIC_URL") or "http://localhost:3000").rstrip("/")


# ── Sign-up and the organisation ─────────────────────────────────────────────

async def signup(email: str, password: str) -> dict:
    """Create the founder's account. The verification link goes by mail
    (log-only by default). The answer never says whether the address existed."""
    from auth.routes import _send_verification_link
    from core.identity.registration import RegistrationDisabled, RegistrationError, register_account
    try:
        outcome = await register_account(email, password)
    except RegistrationDisabled as exc:
        raise OnboardingRefused("Sign-up is not enabled on this deployment (IRA_CLOUD_REGISTRATION_ENABLED).", 404) from exc
    except RegistrationError as exc:
        raise OnboardingRefused(str(exc), 400) from exc
    if outcome.created and outcome.token and outcome.email:
        await _send_verification_link(outcome.email, outcome.token)
    return {"detail": "If that address can be registered, a confirmation link has been sent to it.",
            "next": "confirm the address, sign in, then create the organisation"}


async def create_organisation(founder_email: str, name: str) -> dict:
    """The verified founder's organisation: tenant, default workspace, memberships."""
    from core.identity.tenant_provisioning import ProvisioningRefused, ProvisioningUnavailable, provision_tenant
    try:
        made = await provision_tenant(founder_email, name, workspace_name="Care team")
    except ProvisioningRefused as exc:
        raise OnboardingRefused(str(exc), 409) from exc
    except ProvisioningUnavailable as exc:
        raise OnboardingRefused("The organisation could not be created right now.", 503) from exc
    return {"tenant_id": str(made.tenant_id), "workspace_id": str(made.workspace_id), "organisation_id": str(made.organisation_id),
            "name": name, "next": "add the person you look after, invite the family, pair the watch"}


async def user_id_for_subject(subject: str) -> Optional[uuid.UUID]:
    """The verified, active account a token subject (an email) names, or None.
    The same predicate the cloud login uses, for the same reason."""
    from utils.db import acquire
    async with acquire() as conn:
        return await conn.fetchval(
            "SELECT id FROM users WHERE normalised_email = normalise_email($1) AND email_verified_at IS NOT NULL "
            "AND deleted_at IS NULL AND status = 'active'", (subject or "").strip())


# ── Inviting family by link, with consent captured at invitation ─────────────

def join_link(token: str) -> str:
    return f"{public_base_url()}/join?token={quote(token)}"


async def invite(principal: Principal, person_id: uuid.UUID, body: dict) -> Optional[dict]:
    """Invite somebody into the person's care circle. Creates the tenant
    invitation, the pending circle member, and the consents the person (or
    their legal representative) gave for that member's role; mails the link."""
    from core.identity.invitations import InvitationError, InvitationRefused, create_invitation
    from core.notify.mail import Message, send
    role = body.get("role") or "family"
    if role not in INVITABLE_ROLES:
        raise OnboardingRefused(f"role must be one of {sorted(INVITABLE_ROLES)}", 400)
    async with workspace_connection(principal) as conn:
        person = await conn.fetchrow("SELECT * FROM care_people WHERE id = $1", person_id)
        if person is None:
            return None
        await _enforce_circle_limit(conn, principal, person_id)
    try:
        inv = await create_invitation(principal, body["email"], INVITABLE_ROLES[role])
    except InvitationRefused as exc:
        raise OnboardingRefused(str(exc), 403) from exc
    except InvitationError as exc:
        raise OnboardingRefused(str(exc), 400) from exc
    async with workspace_connection(principal) as conn:
        member = await conn.fetchrow(
            """
            INSERT INTO care_circle_members (person_id, display_name, relationship, role, tier, priority, phone,
                                             push_enabled, sms_enabled, call_enabled, invitation_id, invited_email)
            VALUES ($1, $2, $3, $4, $5, 100, $6, TRUE, TRUE, TRUE, $7, $8) RETURNING *
            """, person_id, body.get("display_name") or body["email"].split("@")[0], body.get("relationship", ""),
            role, int(body.get("tier", 1)), body.get("phone", ""), inv.id, body["email"].strip())
        recorded = []
        for c in body.get("consents") or []:
            await conn.execute(
                "INSERT INTO care_consents (person_id, kind, granted, granted_by, granted_by_name, note, recorded_by) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                person_id, c["kind"], bool(c.get("granted", True)), c.get("granted_by", "person"),
                c.get("granted_by_name", ""), f"recorded at the invitation of {body['email'].strip()}", principal.user_id)
            recorded.append(c["kind"])
    name = person["preferred_name"] or person["display_name"]
    link = join_link(inv.token or "")
    delivery = await send(Message(
        to=body["email"].strip(), subject=f"You are invited to {name}'s care circle on IRA CARE",
        body=(f"You have been invited to join {name}'s care circle as {role.replace('_', ' ')}.\n\n{link}\n\n"
              f"The link works once and expires on {inv.expires_at:%d %B %Y}. Create an account with this email "
              f"address (or sign in) and the link will add you to the circle.\n")))
    return {"invitation_id": str(inv.id), "member_id": str(member["id"]), "email": inv.email, "role": role,
            "expires_at": inv.expires_at, "link": link, "consents_recorded": recorded,
            "mail": {"delivered": delivery.delivered, "transport": delivery.transport}}


async def accept(token: str, user_id: uuid.UUID) -> dict:
    """The invitee, signed in as themselves, redeems the link: tenant and
    workspace memberships, and the pending circle member is linked to them."""
    from core.identity.invitations import InvitationRefused, accept_invitation
    from core.identity.db_context import invitation_connection  # noqa: F401 - the acceptance binds its own tenant
    try:
        accepted = await accept_invitation(token, user_id)
    except InvitationRefused as exc:
        raise OnboardingRefused("This invitation link is not valid.", 400) from exc
    from utils.db import acquire
    async with acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id', $1, false)", str(accepted.tenant_id))
        await conn.execute("SELECT set_config('app.workspace_id', $1, false)", str(accepted.workspace_id or ""))
        await conn.execute("SELECT set_config('app.user_id', $1, false)", str(user_id))
        try:
            row = await conn.fetchrow(
                "UPDATE care_circle_members m SET user_id = $2 FROM tenant_invitations i "
                "WHERE m.invitation_id = i.id AND i.accepted_by = $2 AND i.tenant_id = $1 AND m.user_id IS NULL "
                "RETURNING m.person_id, m.role", accepted.tenant_id, user_id)
        finally:
            for k in ("app.tenant_id", "app.workspace_id", "app.user_id"):
                await conn.execute(f"SELECT set_config('{k}', '', false)")
    return {"tenant_id": str(accepted.tenant_id), "workspace_id": str(accepted.workspace_id) if accepted.workspace_id else None,
            "role": accepted.role, "person_id": str(row["person_id"]) if row else None,
            "circle_role": row["role"] if row else None}


async def list_invitations(principal: Principal, person_id: uuid.UUID) -> Optional[list[dict]]:
    async with workspace_connection(principal) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            return None
        rows = await conn.fetch(
            "SELECT m.id AS member_id, m.display_name, m.role, m.invited_email, m.user_id IS NOT NULL AS accepted, "
            "i.expires_at, i.accepted_at, i.revoked_at FROM care_circle_members m "
            "LEFT JOIN tenant_invitations i ON i.id = m.invitation_id WHERE m.person_id = $1 AND m.invitation_id IS NOT NULL "
            "ORDER BY i.created_at DESC", person_id)
        return [dict(r) for r in rows]


# ── The plan ─────────────────────────────────────────────────────────────────

async def limits_for(principal: Principal) -> dict:
    from core.billing.entitlements import effective_limits
    try:
        limits = await effective_limits(principal)
    except Exception as exc:  # noqa: BLE001 - billing unavailable never blocks care
        logger.warning("care onboarding: entitlements unavailable (%s)", exc)
        return {}
    return {k: v for k, v in limits.items() if k in PLAN_KEYS}


async def enforce_person_limit(principal: Principal) -> None:
    limit = (await limits_for(principal)).get("care_people_max")
    if limit is None:
        return
    async with workspace_connection(principal) as conn:
        count = await conn.fetchval("SELECT count(*) FROM care_people WHERE status <> 'archived'")
    if count >= limit:
        raise OnboardingRefused(f"The plan allows {limit} person(s); archive one or change the plan.", 402)


async def _enforce_circle_limit(conn, principal: Principal, person_id: uuid.UUID) -> None:
    limit = (await limits_for(principal)).get("care_circle_members_max")
    if limit is None:
        return
    count = await conn.fetchval("SELECT count(*) FROM care_circle_members WHERE person_id = $1 AND active", person_id)
    if count >= limit:
        raise OnboardingRefused(f"The plan allows a circle of {limit}; remove a member or change the plan.", 402)


async def plan_summary(principal: Principal) -> dict:
    """What the organisation is on and how much of it is used. Read-only; the
    billing routes (/api/v1/billing) do the rest."""
    from utils.db import acquire
    async with acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id', $1, false)", str(principal.tenant_id))
        try:
            sub = await conn.fetchrow(
                "SELECT s.plan_id, s.status, s.current_period_end, p.display_name FROM tenant_subscriptions s "
                "JOIN billing_plans p ON p.plan_id = s.plan_id WHERE s.tenant_id = $1 ORDER BY s.created_at DESC LIMIT 1",
                principal.tenant_id)
            plans = await conn.fetch(
                "SELECT p.plan_id, p.display_name, p.description, "
                "(SELECT jsonb_object_agg(e.key, e.limit_value) FROM plan_entitlements e WHERE e.plan_id = p.plan_id) AS limits "
                "FROM billing_plans p WHERE p.active AND p.plan_id LIKE 'care_%' ORDER BY p.plan_id")
        finally:
            await conn.execute("SELECT set_config('app.tenant_id', '', false)")
    async with workspace_connection(principal) as conn:
        people = await conn.fetchval("SELECT count(*) FROM care_people WHERE status <> 'archived'")
        members = await conn.fetchval("SELECT count(*) FROM care_circle_members WHERE active")
    import json
    return {
        "subscription": dict(sub) if sub else None,
        "entitled": sub is not None and sub["status"] in ("trialing", "active", "past_due")
        and (sub["current_period_end"] is None or sub["current_period_end"] > dt.datetime.now(dt.timezone.utc)),
        "limits": await limits_for(principal),
        "usage": {"care_people": people, "care_circle_members": members},
        "plans": [dict(p, limits=json.loads(p["limits"]) if isinstance(p["limits"], str) else (p["limits"] or {})) for p in plans],
        "payment_provider": (os.getenv("IRA_PAYMENT_PROVIDER") or "off"),
        "note": "No price is set on the care plans; checkout answers PRICE_NOT_CONFIGURED until the owner prices them.",
    }


async def seed_trial(principal: Principal, plan_id: str, days: int = 30) -> dict:
    """A trial subscription for a new organisation, so limits apply from day one.
    No money, no provider: a row that says 'trialing' until the period ends."""
    from utils.db import acquire
    end = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)
    async with acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id', $1, false)", str(principal.tenant_id))
        try:
            row = await conn.fetchrow(
                "INSERT INTO tenant_subscriptions (tenant_id, plan_id, status, current_period_end) VALUES ($1, $2, 'trialing', $3) RETURNING *",
                principal.tenant_id, plan_id, end)
        finally:
            await conn.execute("SELECT set_config('app.tenant_id', '', false)")
    return dict(row)


__all__ = ["signup", "create_organisation", "invite", "accept", "list_invitations", "plan_summary", "seed_trial",
           "enforce_person_limit", "OnboardingRefused", "repo"]
