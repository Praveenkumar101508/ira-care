"""`/api/v1/care`: the care API.

Two kinds of caller:

* **People**, authenticated by the existing login and bound to a verified
  workspace through `require_workspace_principal`. Their workspace role decides
  read / act / manage (`care.authz`).
* **Watches**, authenticated by a device token in the `X-Device-Token` header
  (`care.devices`). A watch may report readings and state, answer check-ins and
  ask for help, and nothing else.

Every read of readings, check-in answers, medication or transcripts writes a
row to `care_access_log` in the same transaction as the read.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder

from api.middleware.auth import require_auth
from app.middleware import limiter
from api.middleware.workspace import require_workspace_principal
from core.identity.db_context import workspace_connection
from core.identity.principal import Principal

from . import authz, audit, checkin_cycle, consent, history, onboarding, ops, personal, plans, push, reports, repository as repo, schemas, sos, visits
from .authz import ensure_can_act, ensure_can_manage, ensure_can_read

# The two care routes anyone can call without signing in (the Twilio callback is the third,
# and is refused unless its signature is valid). Per client address (P2-03 key).
PAIR_LIMIT = "10/minute"
SIGNUP_LIMIT = "3/minute"

router = APIRouter(prefix="/care", tags=["care"])
logger = logging.getLogger("ira.care.routes")

DEVICE_TOKEN_HEADER = "X-Device-Token"

# ── Which people a caller may see (D-076) ────────────────────────────────────

async def person_guard(person_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)) -> None:
    """Every /people/{person_id}/... route: a manager sees everyone in the workspace; anyone
    else only the people whose circle they belong to. Others are "not found"."""
    async with workspace_connection(p) as conn:
        if not await authz.may_see_person(conn, p, person_id):
            raise _not_found("Person not found.")


def resource_guard(table: str, param: str, label: str):
    """The same rule for a route named by one of a person's things (an alert, a visit...):
    whose it is is looked up first. A thing that does not exist is left to the route."""
    async def guard(request: Request, p: Principal = Depends(require_workspace_principal)) -> None:
        try:
            rid = uuid.UUID(str(request.path_params[param]))
        except (KeyError, ValueError):
            return
        async with workspace_connection(p) as conn:
            person_id = await conn.fetchval(f"SELECT person_id FROM {table} WHERE id = $1", rid)  # noqa: S608 - fixed names
            if person_id is not None and not await authz.may_see_person(conn, p, person_id):
                raise _not_found(f"{label} not found.")
    return guard


_RESOURCE_GUARDS = {
    "member_id": resource_guard("care_circle_members", "member_id", "Circle member"),
    "device_id": resource_guard("care_devices", "device_id", "Device"),
    "reminder_id": resource_guard("care_reminders", "reminder_id", "Reminder"),
    "visit_id": resource_guard("care_visits", "visit_id", "Visit"),
    "alert_id": resource_guard("care_alerts", "alert_id", "Alert"),
    "routine_id": resource_guard("care_routines", "routine_id", "Routine"),
    "medication_id": resource_guard("care_medications", "medication_id", "Medication"),
    "consent_id": resource_guard("care_consents", "consent_id", "Consent"),
    "report_id": resource_guard("care_reports", "report_id", "Report"),
}


def _not_found(what: str = "Not found") -> HTTPException:
    return HTTPException(status_code=404, detail=what)


async def require_device(
    x_device_token: Optional[str] = Header(default=None, alias=DEVICE_TOKEN_HEADER),
) -> tuple[Principal, dict]:
    if not x_device_token:
        raise HTTPException(status_code=401, detail="Device token required.")
    found = await repo.authenticate_device(x_device_token)
    if found is None:
        raise HTTPException(status_code=401, detail="Device token not recognised.")
    return found


@router.get("/capabilities")
async def list_capabilities(p: Principal = Depends(require_workspace_principal)):  # noqa: ARG001
    """What IRA CARE measures and what it does not, with the reason (care/capabilities.py)."""
    from . import capabilities
    return capabilities.public()


# ── People ───────────────────────────────────────────────────────────────────

def _refused(exc: onboarding.OnboardingRefused) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


# ── Onboarding: sign-up, the organisation, invitations, the plan ─────────────

@router.post("/signup", status_code=202)
@limiter.limit(SIGNUP_LIMIT)   # unauthenticated and it creates rows: the same budget as /auth/register
async def care_signup(request: Request, body: schemas.Signup):
    """A care organisation's founder creates an account. Unauthenticated; the
    answer never says whether the address existed; a confirmation link goes by
    mail (log-only unless SMTP is configured). 404 when sign-up is not enabled."""
    try:
        return await onboarding.signup(body.email, body.password)
    except onboarding.OnboardingRefused as exc:
        raise _refused(exc)


@router.post("/organisations", status_code=201)
async def care_create_organisation(body: schemas.OrganisationCreate, subject: str = Depends(require_auth)):
    """The verified founder creates the organisation: tenant, care team workspace,
    memberships, and a 30-day trial of the chosen care plan so its limits apply."""
    try:
        made = await onboarding.create_organisation(subject, body.name)
    except onboarding.OnboardingRefused as exc:
        raise _refused(exc)
    from core.editions import EDITION_CLOUD
    principal = Principal(user_id=uuid.UUID(int=0), tenant_id=uuid.UUID(made["tenant_id"]), session_id=uuid.uuid4(),
                          edition=EDITION_CLOUD, workspace_id=uuid.UUID(made["workspace_id"]), workspace_role="workspace_admin")
    try:
        trial = await onboarding.seed_trial(principal, body.plan_id)
        made["trial"] = {"plan_id": trial["plan_id"], "status": trial["status"], "ends": trial["current_period_end"]}
    except Exception as exc:  # noqa: BLE001 - a missing plan row never blocks the organisation
        made["trial"] = None
        made["trial_note"] = f"no trial recorded ({type(exc).__name__})"
    return made


@router.post("/people/{person_id}/invitations", status_code=201, dependencies=[Depends(person_guard)])
async def invite_to_circle(person_id: uuid.UUID, body: schemas.InvitationCreate,
                           p: Principal = Depends(require_workspace_principal)):
    """Invite family or a carer by link, recording the consents given for their
    role at the same time. The link is mailed (log-only by default) and returned
    so it can be shared another way."""
    ensure_can_manage(p)
    try:
        out = await onboarding.invite(p, person_id, body.model_dump())
    except onboarding.OnboardingRefused as exc:
        raise _refused(exc)
    if out is None:
        raise _not_found("Person not found.")
    async with workspace_connection(p) as conn:
        await audit.record_access(conn, p, person_id=person_id, resource_kind="consent", resource_id=uuid.UUID(out["invitation_id"]),
                                  action="export", detail={"event": "invitation", "consents": out["consents_recorded"]})
    return out


@router.get("/people/{person_id}/invitations", dependencies=[Depends(person_guard)])
async def list_circle_invitations(person_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    ensure_can_manage(p)
    rows = await onboarding.list_invitations(p, person_id)
    if rows is None:
        raise _not_found("Person not found.")
    return {"invitations": rows, "count": len(rows)}


@router.post("/invitations/accept")
async def accept_circle_invitation(body: schemas.InvitationAccept, subject: str = Depends(require_auth)):
    """The invitee, signed in as themselves (same email as the invitation),
    redeems the link and joins the circle. No tenant or workspace is named."""
    user_id = await onboarding.user_id_for_subject(subject)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Sign in with the invited email address first.")
    try:
        return await onboarding.accept(body.token, user_id)
    except onboarding.OnboardingRefused as exc:
        raise _refused(exc)


@router.get("/plan")
async def care_plan(p: Principal = Depends(require_workspace_principal)):
    """The organisation's plan, limits and usage. Prices and payment are the
    billing routes' business (/api/v1/billing)."""
    ensure_can_manage(p)
    return await onboarding.plan_summary(p)


@router.post("/people", status_code=201)
async def create_person(body: schemas.PersonCreate, p: Principal = Depends(require_workspace_principal)):
    ensure_can_manage(p)
    _notes_need_a_key(body.access_notes)
    try:
        await onboarding.enforce_person_limit(p)
    except onboarding.OnboardingRefused as exc:
        raise _refused(exc)
    return await repo.create_person(p, body.model_dump())


def _notes_need_a_key(access_notes) -> None:
    """Access notes are sealed at rest; without the key they cannot be stored, and
    we say so rather than store them in the clear."""
    from . import accessnotes
    if access_notes and not accessnotes.configured():
        raise HTTPException(status_code=503, detail="Access notes cannot be stored: the encryption key "
                            "(IRA_CONNECTOR_ENCRYPTION_KEY) is not configured on the server.")


@router.get("/people")
async def list_people(include_archived: bool = False, p: Principal = Depends(require_workspace_principal)):
    ensure_can_read(p)
    people = await repo.list_people(p, include_archived=include_archived)
    async with workspace_connection(p) as conn:
        visible = await authz.visible_people(conn, p)
    if visible is not None:  # only the people whose circle the caller belongs to (D-076)
        people = [x for x in people if uuid.UUID(str(x["id"])) in visible]
    return {"people": people, "count": len(people)}


@router.get("/people/{person_id}", dependencies=[Depends(person_guard)])
async def get_person(person_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    ensure_can_read(p)
    person = await repo.get_person(p, person_id)
    if person is None:
        raise _not_found("Person not found.")
    return person


@router.patch("/people/{person_id}", dependencies=[Depends(person_guard)])
async def update_person(person_id: uuid.UUID, body: schemas.PersonUpdate,
                        p: Principal = Depends(require_workspace_principal)):
    ensure_can_manage(p)
    _notes_need_a_key(body.access_notes)
    person = await repo.update_person(p, person_id, body.model_dump(exclude_unset=True))
    if person is None:
        raise _not_found("Person not found.")
    return person


@router.delete("/people/{person_id}", status_code=204, dependencies=[Depends(person_guard)])
async def erase_person(person_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    """Delete the person and everything recorded about them (the access log stays)."""
    ensure_can_manage(p)
    async with workspace_connection(p) as conn:
        await audit.record_access(conn, p, person_id=person_id, resource_kind="person",
                                  resource_id=person_id, action="export",
                                  detail={"event": "erasure requested"})
    if not await repo.erase_person(p, person_id):
        raise _not_found("Person not found.")
    return None


@router.get("/people/{person_id}/status", dependencies=[Depends(person_guard)])
async def person_status(person_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    """Live status for the family app: device state, latest readings, open alerts."""
    ensure_can_read(p)
    async with workspace_connection(p) as conn:
        status = await repo.person_status(conn, person_id)
        if status is None:
            raise _not_found("Person not found.")
        await audit.record_access(conn, p, person_id=person_id, resource_kind="vitals",
                                  detail={"view": "status"})
    return status


# ── Care circle ──────────────────────────────────────────────────────────────

@router.get("/people/{person_id}/circle", dependencies=[Depends(person_guard)])
async def list_members(person_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    ensure_can_read(p)
    members = await repo.list_members(p, person_id)
    return {"members": members, "count": len(members)}


@router.post("/people/{person_id}/circle", status_code=201, dependencies=[Depends(person_guard)])
async def add_member(person_id: uuid.UUID, body: schemas.CircleMemberCreate,
                     p: Principal = Depends(require_workspace_principal)):
    ensure_can_manage(p)
    member = await repo.add_member(p, person_id, body.model_dump())
    if member is None:
        raise _not_found("Person not found.")
    return member


@router.patch("/circle/{member_id}", dependencies=[Depends(_RESOURCE_GUARDS["member_id"])])
async def update_member(member_id: uuid.UUID, body: schemas.CircleMemberUpdate,
                        p: Principal = Depends(require_workspace_principal)):
    ensure_can_manage(p)
    member = await repo.update_member(p, member_id, body.model_dump(exclude_unset=True))
    if member is None:
        raise _not_found("Member not found.")
    return member


@router.delete("/circle/{member_id}", status_code=204, dependencies=[Depends(_RESOURCE_GUARDS["member_id"])])
async def remove_member(member_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    ensure_can_manage(p)
    if not await repo.remove_member(p, member_id):
        raise _not_found("Member not found.")
    return None


# ── Safety settings ──────────────────────────────────────────────────────────

@router.get("/people/{person_id}/settings", dependencies=[Depends(person_guard)])
async def get_settings(person_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    ensure_can_read(p)
    settings = await repo.get_settings(p, person_id)
    if settings is None:
        raise _not_found("Person not found.")
    return settings


@router.put("/people/{person_id}/settings", dependencies=[Depends(person_guard)])
async def put_settings(person_id: uuid.UUID, body: schemas.SafetySettings,
                       p: Principal = Depends(require_workspace_principal)):
    ensure_can_manage(p)
    settings = await repo.put_settings(p, person_id, body.model_dump())
    if settings is None:
        raise _not_found("Person not found.")
    return settings


# ── Devices (family side) ────────────────────────────────────────────────────

@router.post("/people/{person_id}/devices", status_code=201, dependencies=[Depends(person_guard)])
async def create_device(person_id: uuid.UUID, body: schemas.DeviceCreate,
                        p: Principal = Depends(require_workspace_principal)):
    """Register a watch and get its one-time pairing code (shown once; valid 15 minutes)."""
    ensure_can_manage(p)
    device = await repo.create_device(p, person_id, body.model_dump())
    if device is None:
        raise _not_found("Person not found.")
    return device


@router.get("/people/{person_id}/devices", dependencies=[Depends(person_guard)])
async def list_devices(person_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    ensure_can_read(p)
    devices = await repo.list_devices(p, person_id)
    return {"devices": devices, "count": len(devices)}


@router.post("/devices/{device_id}/revoke", dependencies=[Depends(_RESOURCE_GUARDS["device_id"])])
async def revoke_device(device_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    ensure_can_manage(p)
    device = await repo.revoke_device(p, device_id)
    if device is None:
        raise _not_found("Device not found.")
    return device


# ── Devices (watch side) ─────────────────────────────────────────────────────

@router.post("/devices/pair")
@limiter.limit(PAIR_LIMIT)   # unauthenticated: the code is the credential, so guesses are rationed per address
async def pair_device(request: Request, body: schemas.PairRequest):
    """Exchange a pairing code for a device token. No login: the code is the credential."""
    device = await repo.pair_device(body.pairing_code, app_version=body.app_version)
    if device is None:
        raise HTTPException(status_code=404, detail="Pairing code not recognised or expired.")
    return device


async def _idempotent(principal: Principal, device: dict, key: Optional[str], kind: str, status_code: int, work):
    """Run `work()` once per idempotency key. A repeat of a key the server has
    already answered gets the stored answer back and changes nothing; the watch's
    outbox re-sends until it hears one of these."""
    from fastapi.responses import JSONResponse
    replayed = await sos.replay(principal, device, key)
    if replayed is not None:
        code, body = replayed
        return JSONResponse(status_code=code, content=body, headers={"X-Idempotent-Replay": "true"})
    try:
        body = await work()
    except HTTPException as exc:
        # A refusal is an answer too: the watch must not retry it forever.
        await sos.remember(principal, device, key, kind, exc.status_code, {"detail": exc.detail})
        raise
    payload = jsonable_encoder(body)
    await sos.remember(principal, device, key, kind, status_code, payload)
    return JSONResponse(status_code=status_code, content=payload)


@router.get("/devices/me")
async def device_me(d: tuple[Principal, dict] = Depends(require_device)):
    """The watch's own record, with the carer's fall settings so the detector
    runs with the person's thresholds (or not at all), the heart-rate range the
    watch checks each sample against, and how often ordinary readings go up."""
    principal, device = d
    from . import falls
    async with workspace_connection(principal) as conn:
        settings = await conn.fetchrow("SELECT * FROM care_safety_settings WHERE person_id = $1", device["person_id"])
        state = await sos.current(conn, device["person_id"])
        profile = await personal.get_profile(conn, device["person_id"])
    out = dict(device)
    out["fall"] = falls.watch_settings(dict(settings) if settings else {})
    out["readings"] = sos.watch_settings(dict(settings) if settings else {})
    out["sos"] = state
    out["profile"] = personal.watch_profile(profile)
    return out


@router.post("/devices/me/visits/{visit_id}/acknowledge")
async def device_visit_acknowledge(visit_id: uuid.UUID, d: tuple[Principal, dict] = Depends(require_device)):
    """The watch showed "[Name] is here" and the person saw it."""
    principal, device = d
    row = await visits.acknowledge_from_device(principal, device, visit_id)
    if row is None:
        raise _not_found("No visit in progress with that id.")
    return {"visit_id": row["id"], "acknowledged_at": row["acknowledged_at"]}


@router.get("/devices/me/sos")
async def device_sos(d: tuple[Principal, dict] = Depends(require_device)):
    """The true state of the person's SOS, for the wrist: received, on the way
    (with the responder's name), arrived, closed, or idle."""
    principal, device = d
    return await sos.current_for_device(principal, device)


@router.get("/devices/me/sos/stream")
async def device_sos_stream(d: tuple[Principal, dict] = Depends(require_device)):
    """The same state, pushed: one JSON line now and one on every change, the
    connection held open (the watch reconnects when it closes)."""
    from fastapi.responses import StreamingResponse
    from . import talk
    principal, device = d
    return StreamingResponse(talk.ndjson(sos.stream(principal, device)), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/devices/me/talk")
async def device_talk(body: schemas.Talk, d: tuple[Principal, dict] = Depends(require_device)):
    """Talk to IRA from the watch. Streams one JSON object per line: each
    sentence as it is ready, then `done` with any action for the watch."""
    from fastapi.responses import StreamingResponse
    from . import talk
    principal, device = d
    return StreamingResponse(talk.ndjson(talk.exchange(principal, device["person_id"], body.text, source="watch")),
                             media_type="application/x-ndjson")


@router.post("/devices/me/fall", status_code=201)
async def device_fall_started(body: schemas.FallStart, d: tuple[Principal, dict] = Depends(require_device),
                              key: Optional[str] = Header(default=None, alias=sos.IDEMPOTENCY_HEADER)):
    """The watch suspects a fall and has started its countdown: the server opens
    an asking alert and keeps its own deadline. 409 when fall detection is off."""
    from . import falls
    principal, device = d

    async def work():
        try:
            alert = await falls.start(principal, device, body.model_dump())
        except falls.FallDetectionOff as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if alert is None:
            raise HTTPException(status_code=409, detail="A fall alert is already asking or open for this person.")
        return alert
    return await _idempotent(principal, device, key, "fall_started", 201, work)


@router.post("/devices/me/fall/ok")
async def device_fall_ok(d: tuple[Principal, dict] = Depends(require_device),
                         key: Optional[str] = Header(default=None, alias=sos.IDEMPOTENCY_HEADER)):
    """The person tapped I'm OK during the fall countdown."""
    from . import falls
    principal, device = d

    async def work():
        alert = await falls.answered_ok(principal, device)
        if alert is None:
            raise _not_found("No fall alert is asking for this person.")
        return alert
    return await _idempotent(principal, device, key, "fall_ok", 200, work)


@router.post("/devices/me/report")
async def device_report(body: schemas.DeviceReport, d: tuple[Principal, dict] = Depends(require_device)):
    principal, device = d
    return await repo.record_report(principal, device, body.model_dump())


@router.post("/devices/me/unpair")
async def device_unpair(d: tuple[Principal, dict] = Depends(require_device)):
    """The wearer unpaired this watch from the wrist, after its warning. The token stops
    working at once and the care circle is told that monitoring has stopped. The watch
    clears its pairing only after this answers, so the token is never left live."""
    principal, device = d
    out = await repo.unpair_by_wearer(principal, device)
    if out is None:
        raise HTTPException(status_code=401, detail="Device token not recognised.")
    return out


@router.post("/devices/me/help", status_code=201)
async def device_help(body: schemas.HelpRequest, d: tuple[Principal, dict] = Depends(require_device),
                      key: Optional[str] = Header(default=None, alias=sos.IDEMPOTENCY_HEADER)):
    """The Help button or the spoken word: opens an SOS alert at once. The
    answer carries the SOS state the wrist shows ("Help request received")."""
    principal, device = d

    async def work():
        alert = await repo.record_help_request(principal, device, body.model_dump())
        alert["sos"] = await sos.current_for_device(principal, device)
        return alert
    return await _idempotent(principal, device, key, "help", 201, work)


# ── Push registration for the family and carer apps ──────────────────────────

@router.post("/push/register", status_code=201)
async def register_push(body: schemas.PushRegistration, p: Principal = Depends(require_workspace_principal)):
    """Tie this phone's push token to the signed-in account's circle memberships
    here. A care alert is pushed only to the tokens of the members it is for."""
    if not push.is_valid_token(body.token):
        raise HTTPException(status_code=422, detail="That is not a push token this service accepts.")
    async with workspace_connection(p) as conn:
        reg = await push.register(conn, p, body.token, body.platform, body.person_id)
    if reg.memberships == 0:
        raise HTTPException(status_code=404, detail="This account is not a member of any care circle here.")
    return {"memberships": reg.memberships, "tokens": reg.tokens}


@router.delete("/push/register")
async def unregister_push(body: schemas.PushUnregistration, p: Principal = Depends(require_workspace_principal)):
    """The phone signed out or turned push off: the token is revoked for this account."""
    async with workspace_connection(p) as conn:
        n = await push.unregister(conn, p, body.token)
    return {"revoked": n}


@router.get("/push/registrations")
async def list_push_registrations(p: Principal = Depends(require_workspace_principal)):
    """The signed-in account's own registrations (no token values)."""
    async with workspace_connection(p) as conn:
        return {"registrations": await push.registrations(conn, p)}


# ── Check-ins (the watch's side) ─────────────────────────────────────────────

@router.get("/devices/me/checkins/due")
async def device_due_checkin(d: tuple[Principal, dict] = Depends(require_device)):
    """The check-in to ask now, with its words, or `null`. Asking marks it asked."""
    principal, device = d
    return {"checkin": await checkin_cycle.due_checkin(principal, device)}


@router.post("/devices/me/checkins/{checkin_id}/repeated")
async def device_checkin_repeated(checkin_id: uuid.UUID, d: tuple[Principal, dict] = Depends(require_device)):
    """The watch repeated the question, louder and with vibration."""
    principal, device = d
    row = await checkin_cycle.mark_repeated(principal, device, checkin_id)
    if row is None:
        raise _not_found("No open check-in with that id.")
    return row


@router.post("/devices/me/checkins/{checkin_id}/answer")
async def device_checkin_answer(checkin_id: uuid.UUID, body: schemas.CheckinAnswer,
                                d: tuple[Principal, dict] = Depends(require_device),
                                key: Optional[str] = Header(default=None, alias=sos.IDEMPOTENCY_HEADER)):
    """What the person said, or which button they pressed."""
    principal, device = d

    async def work():
        row = await checkin_cycle.record_answer(principal, device, checkin_id, body.text, via=body.via)
        if row is None:
            raise _not_found("No open check-in with that id.")
        return row
    return await _idempotent(principal, device, key, "checkin_answer", 200, work)


@router.post("/devices/me/checkins/{checkin_id}/missed")
async def device_checkin_missed(checkin_id: uuid.UUID, d: tuple[Principal, dict] = Depends(require_device),
                                key: Optional[str] = Header(default=None, alias=sos.IDEMPOTENCY_HEADER)):
    """Nobody answered, after the repeat."""
    principal, device = d

    async def work():
        row = await checkin_cycle.mark_missed(principal, device, checkin_id)
        if row is None:
            raise _not_found("No open check-in with that id.")
        return row
    return await _idempotent(principal, device, key, "checkin_missed", 200, work)


@router.post("/people/{person_id}/checkins/ask-now", status_code=201, dependencies=[Depends(person_guard)])
async def ask_now(person_id: uuid.UUID, body: schemas.AskNow, p: Principal = Depends(require_workspace_principal)):
    """A family member or carer asks IRA to check in straight away."""
    ensure_can_act(p)
    row = await checkin_cycle.ask_now(p, person_id, kind=body.kind, custom_text=body.text)
    if row is None:
        raise _not_found("Person not found.")
    return row


# ── One app: who you are to the people here, and how it looks for you (111) ──

EXPERIENCES = ("wearer", "family", "carer")


def experience_for(memberships: list[dict], manage: bool) -> str:
    """wearer: a circle member whose relationship is 'self'; carer: a carer, clinician,
    care-organisation member or a workspace administrator; otherwise family."""
    if any(str(m.get("relationship", "")).strip().lower() == "self" for m in memberships):
        return "wearer"
    if manage or any(m.get("role") in ("carer", "clinician", "care_organisation") for m in memberships):
        return "carer"
    return "family"


@router.get("/me/experience")
async def my_experience(p: Principal = Depends(require_workspace_principal)):
    """Which of the app's three experiences this account gets, and for whom."""
    async with workspace_connection(p) as conn:
        rows = [dict(r) for r in await conn.fetch(
            "SELECT m.id, m.person_id, m.role, m.relationship, m.display_name, m.tier, "
            "coalesce(nullif(pp.preferred_name, ''), pp.display_name) AS person_name "
            "FROM care_circle_members m JOIN care_people pp ON pp.id = m.person_id "
            "WHERE m.user_id = $1 AND m.active AND pp.status = 'active' ORDER BY pp.display_name", p.user_id)]
    manage = authz.can_manage(p)
    return {"experience": experience_for(rows, manage), "can_manage": manage,
            "memberships": [{k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in r.items()} for r in rows]}


@router.get("/me/preferences")
async def my_preferences(p: Principal = Depends(require_workspace_principal)):
    """How the app looks for this account, the same on every device."""
    async with workspace_connection(p) as conn:
        row = await conn.fetchrow("SELECT appearance, updated_at FROM care_user_preferences WHERE user_id = $1", p.user_id)
    appearance = row["appearance"] if row else {}
    if isinstance(appearance, str):
        appearance = json.loads(appearance)
    return {"appearance": appearance, "updated_at": row["updated_at"] if row else None}


@router.put("/me/preferences")
async def put_my_preferences(body: schemas.Preferences, p: Principal = Depends(require_workspace_principal)):
    appearance = body.model_dump(exclude_none=True)
    text = json.dumps(appearance)
    if len(text) > 500_000:
        raise HTTPException(status_code=413, detail="The appearance (a photo wallpaper, probably) is too large: at most 500 KB.")
    async with workspace_connection(p) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO care_user_preferences (user_id, appearance) VALUES ($1, $2::jsonb)
            ON CONFLICT (workspace_id, user_id) DO UPDATE SET appearance = EXCLUDED.appearance, updated_at = NOW()
            RETURNING appearance, updated_at
            """, p.user_id, text)
    appearance = row["appearance"]
    if isinstance(appearance, str):
        appearance = json.loads(appearance)
    return {"appearance": appearance, "updated_at": row["updated_at"]}


@router.get("/me/permissions")
async def my_permissions(p: Principal = Depends(require_workspace_principal)):
    """This account's staff role and the actions it may take (authz.STAFF_ROLES, ACTIONS), so
    the dashboard shows only what will work. Every route still checks for itself."""
    return authz.permissions_for(p)


# ── The care plan (116) ──────────────────────────────────────────────────────

async def _person_exists(conn, person_id: uuid.UUID) -> None:
    if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
        raise _not_found("Person not found.")


@router.get("/people/{person_id}/plan", dependencies=[Depends(person_guard)])
async def get_plan(person_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    """The current care plan, or `plan: null` when none has been written yet."""
    ensure_can_read(p)
    async with workspace_connection(p) as conn:
        await _person_exists(conn, person_id)
        return {"plan": await plans.current(conn, p, person_id)}


@router.get("/people/{person_id}/plan/history", dependencies=[Depends(person_guard)])
async def get_plan_history(person_id: uuid.UUID, limit: int = Query(50, ge=1, le=200),
                           p: Principal = Depends(require_workspace_principal)):
    """Every version of the plan, newest first, each with who changed it and why."""
    ensure_can_read(p)
    async with workspace_connection(p) as conn:
        await _person_exists(conn, person_id)
        versions = await plans.history(conn, p, person_id, limit=limit)
    return {"versions": versions, "count": len(versions)}


@router.post("/people/{person_id}/plan", status_code=201, dependencies=[Depends(person_guard)])
async def save_plan(person_id: uuid.UUID, body: schemas.CarePlanVersion, p: Principal = Depends(require_workspace_principal)):
    """A care manager saves a new version of the plan. The old one is kept."""
    ensure_can_manage(p)
    async with workspace_connection(p) as conn:
        await _person_exists(conn, person_id)
        try:
            return await plans.save(conn, p, person_id, body.model_dump(), author_name=p.username or "")
        except plans.PlanConflict as c:
            raise HTTPException(status_code=409, detail=(
                f"Someone else saved the plan first (it is now version {c.args[0]}). "
                "Reload it, then make your change again.")) from c


@router.post("/people/{person_id}/help", status_code=201, dependencies=[Depends(person_guard)])
async def help_from_phone(person_id: uuid.UUID, body: schemas.PhoneHelp, p: Principal = Depends(require_workspace_principal)):
    """Help from the phone: the person themselves (the wearer experience) or a family
    member who is with them. Opens the same SOS the watch would, marked 'phone'."""
    ensure_can_act(p)
    async with workspace_connection(p) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1 AND status = 'active'", person_id):
            raise _not_found("Person not found.")
        member = await conn.fetchrow(
            "SELECT display_name, relationship FROM care_circle_members WHERE person_id = $1 AND user_id = $2 AND active",
            person_id, p.user_id)
        who = "the person" if member and str(member["relationship"]).lower() == "self" else (member["display_name"] if member else "a member of the circle")
        alert = await repo.open_alert(conn, person_id, kind="help_requested", severity="sos",
                                      details={"via": "phone", "note": body.note, "raised_by": who,
                                               "summary": f"Help was asked for from the phone by {who}."})
    return alert


# ── IRA, personal to the person (110) ────────────────────────────────────────

async def _editor_role(conn, p: Principal, person_id: uuid.UUID) -> str:
    members = [dict(r) for r in await conn.fetch(
        "SELECT user_id, role, relationship FROM care_circle_members WHERE person_id = $1 AND active", person_id)]
    return personal.role_for(p, members)


@router.get("/people/{person_id}/profile", dependencies=[Depends(person_guard)])
async def get_profile(person_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    """How IRA speaks to this person, and the topic lists (with consent)."""
    ensure_can_read(p)
    async with workspace_connection(p) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            raise _not_found("Person not found.")
        return await personal.get_profile(conn, person_id)


@router.put("/people/{person_id}/profile", dependencies=[Depends(person_guard)])
async def put_profile(person_id: uuid.UUID, body: schemas.CommunicationProfile, p: Principal = Depends(require_workspace_principal)):
    """The person or their representative changes how IRA speaks to them."""
    ensure_can_act(p)
    async with workspace_connection(p) as conn:
        role = await _editor_role(conn, p, person_id)
    row = await personal.put_profile(p, person_id, body.model_dump(), role=role)
    if row is None:
        raise _not_found("Person or circle member not found.")
    return row


@router.get("/people/{person_id}/reminders", dependencies=[Depends(person_guard)])
async def list_reminders(person_id: uuid.UUID, open_only: bool = False, limit: int = Query(100, ge=1, le=1000),
                         p: Principal = Depends(require_workspace_principal)):
    ensure_can_read(p)
    async with workspace_connection(p) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            raise _not_found("Person not found.")
    rows = await personal.list_reminders(p, person_id, limit=limit, open_only=open_only)
    return {"reminders": rows, "count": len(rows)}


@router.post("/people/{person_id}/reminders", status_code=201, dependencies=[Depends(person_guard)])
async def create_reminder(person_id: uuid.UUID, body: schemas.ReminderCreate, p: Principal = Depends(require_workspace_principal)):
    """A carer or family member sets a reminder the watch will ask about at its time."""
    ensure_can_act(p)
    async with workspace_connection(p) as conn:
        role = await _editor_role(conn, p, person_id)
    row = await personal.create_reminder(p, person_id, body.model_dump(), source="family" if role in ("person", "representative") else "carer")
    if row is None:
        raise _not_found("Person not found.")
    return row


@router.post("/reminders/{reminder_id}/act", dependencies=[Depends(_RESOURCE_GUARDS["reminder_id"])])
async def act_on_reminder(reminder_id: uuid.UUID, body: schemas.ReminderAction, p: Principal = Depends(require_workspace_principal)):
    """Confirm, snooze, skip or cancel a reminder from the phone or the dashboard."""
    ensure_can_act(p)
    row = await personal.act_on_reminder(p, reminder_id, body.action, minutes=body.minutes)
    if row is None:
        raise _not_found("Reminder not found.")
    return row


@router.put("/people/{person_id}/memory/{day}", dependencies=[Depends(person_guard)])
async def edit_memory(person_id: uuid.UUID, day: dt.date, body: schemas.MemoryEdit, p: Principal = Depends(require_workspace_principal)):
    """The person or their representative changes what IRA remembers about a day."""
    ensure_can_act(p)
    async with workspace_connection(p) as conn:
        role = await _editor_role(conn, p, person_id)
        if role not in ("person", "representative", "admin"):
            raise HTTPException(status_code=403, detail="Only the person, their representative or an administrator may change what IRA remembers.")
    row = await personal.edit_memory(p, person_id, day, body.summary, role="person" if role == "person" else "representative")
    if row is None:
        raise _not_found("Person not found.")
    return row


@router.delete("/people/{person_id}/memory/{day}", status_code=204, dependencies=[Depends(person_guard)])
async def forget_memory(person_id: uuid.UUID, day: dt.date, p: Principal = Depends(require_workspace_principal)):
    ensure_can_act(p)
    async with workspace_connection(p) as conn:
        role = await _editor_role(conn, p, person_id)
        if role not in ("person", "representative", "admin"):
            raise HTTPException(status_code=403, detail="Only the person, their representative or an administrator may clear what IRA remembers.")
    if not await personal.forget_day(p, person_id, day):
        raise _not_found("Nothing remembered for that day.")


# ── Scheduled visits (109) ───────────────────────────────────────────────────

@router.post("/people/{person_id}/visits", status_code=201, dependencies=[Depends(person_guard)])
async def create_visit(person_id: uuid.UUID, body: schemas.VisitCreate, p: Principal = Depends(require_workspace_principal)):
    """Plan a visit: who, when, what for, which tasks."""
    ensure_can_act(p)
    row = await visits.create(p, person_id, body.model_dump())
    if row is None:
        raise _not_found("Person or circle member not found.")
    return row


@router.get("/people/{person_id}/visits", dependencies=[Depends(person_guard)])
async def list_visits(person_id: uuid.UUID, since: Optional[dt.datetime] = Query(None),
                      until: Optional[dt.datetime] = Query(None), limit: int = Query(100, ge=1, le=1000),
                      p: Principal = Depends(require_workspace_principal)):
    ensure_can_read(p)
    async with workspace_connection(p) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            raise _not_found("Person not found.")
    rows = await visits.list_for_person(p, person_id, since=since, until=until, limit=limit)
    return {"visits": rows, "count": len(rows)}


@router.get("/visits/{visit_id}", dependencies=[Depends(_RESOURCE_GUARDS["visit_id"])])
async def get_visit(visit_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    ensure_can_read(p)
    row = await visits.get(p, visit_id)
    if row is None:
        raise _not_found("Visit not found.")
    return row


@router.patch("/visits/{visit_id}", dependencies=[Depends(_RESOURCE_GUARDS["visit_id"])])
async def update_visit(visit_id: uuid.UUID, body: schemas.VisitUpdate, p: Principal = Depends(require_workspace_principal)):
    """Move or re-describe a planned visit. A visit that has started does not move."""
    ensure_can_act(p)
    changes = body.model_dump(exclude_none=True)
    if "planned_start" in changes and "planned_end" in changes and changes["planned_end"] <= changes["planned_start"]:
        raise HTTPException(status_code=422, detail="planned_end must be after planned_start")
    row = await visits.update(p, visit_id, changes)
    if row is None:
        raise _not_found("Visit not found.")
    return row


@router.post("/visits/{visit_id}/arrive", dependencies=[Depends(_RESOURCE_GUARDS["visit_id"])])
async def arrive_visit(visit_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    """The carer is with the person: the watch says so, check-ins pause, any
    late or missed alert for this visit closes."""
    ensure_can_act(p)
    row = await visits.arrive(p, visit_id)
    if row is None:
        raise _not_found("Visit not found.")
    return row


@router.post("/visits/{visit_id}/depart", dependencies=[Depends(_RESOURCE_GUARDS["visit_id"])])
async def depart_visit(visit_id: uuid.UUID, body: schemas.VisitDepart, p: Principal = Depends(require_workspace_principal)):
    """The carer leaves: outcome, notes, handover and the tasks done."""
    ensure_can_act(p)
    row = await visits.depart(p, visit_id, body.model_dump())
    if row is None:
        raise _not_found("Visit not found.")
    if row["status"] != "departed":
        raise HTTPException(status_code=409, detail="Only a visit with a recorded arrival can be departed.")
    return row


@router.post("/visits/{visit_id}/cancel", dependencies=[Depends(_RESOURCE_GUARDS["visit_id"])])
async def cancel_visit(visit_id: uuid.UUID, body: schemas.VisitCancel, p: Principal = Depends(require_workspace_principal)):
    ensure_can_act(p)
    row = await visits.cancel(p, visit_id, body.note)
    if row is None:
        raise _not_found("Visit not found.")
    return row


@router.post("/visits/{visit_id}/tasks", status_code=201, dependencies=[Depends(_RESOURCE_GUARDS["visit_id"])])
async def add_visit_task(visit_id: uuid.UUID, body: schemas.VisitTaskCreate, p: Principal = Depends(require_workspace_principal)):
    ensure_can_act(p)
    row = await visits.add_task(p, visit_id, body.title)
    if row is None:
        raise _not_found("Visit not found or already finished.")
    return row


@router.patch("/visits/{visit_id}/tasks/{task_id}", dependencies=[Depends(_RESOURCE_GUARDS["visit_id"])])
async def set_visit_task(visit_id: uuid.UUID, task_id: uuid.UUID, body: schemas.VisitTaskUpdate,
                         p: Principal = Depends(require_workspace_principal)):
    ensure_can_act(p)
    row = await visits.set_task(p, visit_id, task_id, done=body.done, note=body.note)
    if row is None:
        raise _not_found("Task not found.")
    return row


# ── Vitals and check-ins ─────────────────────────────────────────────────────

@router.get("/people/{person_id}/vitals", dependencies=[Depends(person_guard)])
async def get_vitals(
    person_id: uuid.UUID,
    kind: Optional[str] = Query(None),
    since: Optional[dt.datetime] = Query(None),
    until: Optional[dt.datetime] = Query(None),
    limit: int = Query(200, ge=1, le=5000),
    p: Principal = Depends(require_workspace_principal),
):
    ensure_can_read(p)
    if kind is not None and kind not in schemas.VITAL_KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {schemas.VITAL_KINDS}")
    async with workspace_connection(p) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            raise _not_found("Person not found.")
        rows = await repo.fetch_vitals(conn, person_id, kind=kind, since=since, until=until, limit=limit)
        await audit.record_access(conn, p, person_id=person_id, resource_kind="vitals",
                                  detail={"kind": kind, "rows": len(rows)})
    return {"vitals": rows, "count": len(rows)}


@router.get("/people/{person_id}/checkins", dependencies=[Depends(person_guard)])
async def get_checkins(person_id: uuid.UUID, limit: int = Query(50, ge=1, le=1000),
                       p: Principal = Depends(require_workspace_principal)):
    """Check-in history. Summaries always; the person's own words only under consent."""
    ensure_can_read(p)
    async with workspace_connection(p) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            raise _not_found("Person not found.")
        role = await repo.circle_role_of(conn, person_id, p.user_id)
        if role is None and p.is_owner:
            role = "carer"
        consents = await repo.fetch_consents(conn, person_id)
        with_answers = consent.may_read_transcripts(role, consents)
        rows = await repo.fetch_checkins(conn, person_id, limit=limit, with_answers=with_answers)
        await audit.record_access(conn, p, person_id=person_id, resource_kind="checkins",
                                  detail={"rows": len(rows), "answers_included": with_answers})
    return {"checkins": rows, "count": len(rows), "answers_included": with_answers}


# ── Alerts ───────────────────────────────────────────────────────────────────

@router.get("/alerts")
async def list_alerts(person_id: Optional[uuid.UUID] = None, open_only: bool = True,
                      limit: int = Query(100, ge=1, le=1000),
                      p: Principal = Depends(require_workspace_principal)):
    ensure_can_read(p)
    async with workspace_connection(p) as conn:
        visible = await authz.visible_people(conn, p)
    if visible is not None and person_id is not None and person_id not in visible:
        return {"alerts": [], "count": 0}
    alerts = await repo.list_alerts(p, person_id=person_id, open_only=open_only, limit=limit,
                                    only_people=visible)
    return {"alerts": alerts, "count": len(alerts)}


@router.get("/alerts/{alert_id}", dependencies=[Depends(_RESOURCE_GUARDS["alert_id"])])
async def get_alert(alert_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    ensure_can_read(p)
    alert = await repo.get_alert(p, alert_id)
    if alert is None:
        raise _not_found("Alert not found.")
    return alert


@router.get("/people/{person_id}/access-notes", dependencies=[Depends(person_guard)])
async def person_access_notes(person_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    """The profile's entry instructions, for a manager only, every reading logged."""
    ensure_can_read(p)
    out = await repo.read_access_notes(p, person_id)
    if out is None:
        raise _not_found("Person not found.")
    if not out.get("allowed"):
        raise HTTPException(status_code=403, detail="Entry instructions on the profile are shown to managers only; during an alert, tier 1 and 2 recipients see them on the alert.")
    return out


@router.get("/alerts/{alert_id}/access-notes", dependencies=[Depends(_RESOURCE_GUARDS["alert_id"])])
async def alert_access_notes(alert_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    """Key safe, door, pets: for tier 1 and 2 recipients while the alert is open.
    Every opening is written to the access log."""
    ensure_can_read(p)
    out = await repo.open_access_notes(p, alert_id)
    if out is None:
        raise _not_found("Alert not found.")
    if not out["allowed"]:
        raise HTTPException(status_code=403, detail="Access notes are shown only to tier 1 and tier 2 "
                            "recipients while an alert is open.")
    return out


@router.post("/alerts/{alert_id}/acknowledge", dependencies=[Depends(_RESOURCE_GUARDS["alert_id"])])
async def acknowledge_alert(alert_id: uuid.UUID, body: schemas.Acknowledge,
                            p: Principal = Depends(require_workspace_principal)):
    """"I'm on my way": stops the escalation and tells everyone who is going."""
    ensure_can_act(p)
    alert = await repo.acknowledge_alert(p, alert_id, body.model_dump())
    if alert is None:
        raise _not_found("Alert not found.")
    return alert


@router.post("/alerts/{alert_id}/resolve", dependencies=[Depends(_RESOURCE_GUARDS["alert_id"])])
async def resolve_alert(alert_id: uuid.UUID, body: schemas.Resolve,
                        p: Principal = Depends(require_workspace_principal)):
    ensure_can_act(p)
    alert = await repo.resolve_alert(p, alert_id, body.resolution, outcome=body.outcome)
    if alert is None:
        raise _not_found("Alert not found or already resolved.")
    return alert


# ── Routines ─────────────────────────────────────────────────────────────────

@router.get("/people/{person_id}/routines", dependencies=[Depends(person_guard)])
async def list_routines(person_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    ensure_can_read(p)
    routines = await repo.list_routines(p, person_id)
    return {"routines": routines, "count": len(routines)}


@router.post("/people/{person_id}/routines", status_code=201, dependencies=[Depends(person_guard)])
async def add_routine(person_id: uuid.UUID, body: schemas.RoutineCreate,
                      p: Principal = Depends(require_workspace_principal)):
    ensure_can_manage(p)
    routine = await repo.add_routine(p, person_id, body.model_dump())
    if routine is None:
        raise _not_found("Person not found.")
    return routine


@router.patch("/routines/{routine_id}", dependencies=[Depends(_RESOURCE_GUARDS["routine_id"])])
async def update_routine(routine_id: uuid.UUID, body: schemas.RoutineUpdate,
                         p: Principal = Depends(require_workspace_principal)):
    ensure_can_manage(p)
    routine = await repo.update_routine(p, routine_id, body.model_dump(exclude_unset=True))
    if routine is None:
        raise _not_found("Routine not found.")
    return routine


@router.delete("/routines/{routine_id}", status_code=204, dependencies=[Depends(_RESOURCE_GUARDS["routine_id"])])
async def delete_routine(routine_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    ensure_can_manage(p)
    if not await repo.delete_routine(p, routine_id):
        raise _not_found("Routine not found.")
    return None


# ── Medication ───────────────────────────────────────────────────────────────

@router.get("/people/{person_id}/medications", dependencies=[Depends(person_guard)])
async def list_medications(person_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    ensure_can_read(p)
    async with workspace_connection(p) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            raise _not_found("Person not found.")
        meds = await repo.list_medications(conn, person_id)
        await audit.record_access(conn, p, person_id=person_id, resource_kind="medication",
                                  detail={"rows": len(meds)})
    return {"medications": meds, "count": len(meds)}


@router.post("/people/{person_id}/medications", status_code=201, dependencies=[Depends(person_guard)])
async def add_medication(person_id: uuid.UUID, body: schemas.MedicationCreate,
                         p: Principal = Depends(require_workspace_principal)):
    ensure_can_manage(p)
    med = await repo.add_medication(p, person_id, body.model_dump())
    if med is None:
        raise _not_found("Person not found.")
    return med


@router.patch("/medications/{medication_id}", dependencies=[Depends(_RESOURCE_GUARDS["medication_id"])])
async def update_medication(medication_id: uuid.UUID, body: schemas.MedicationUpdate,
                            p: Principal = Depends(require_workspace_principal)):
    ensure_can_manage(p)
    med = await repo.update_medication(p, medication_id, body.model_dump(exclude_unset=True))
    if med is None:
        raise _not_found("Medication not found.")
    return med


@router.delete("/medications/{medication_id}", status_code=204, dependencies=[Depends(_RESOURCE_GUARDS["medication_id"])])
async def delete_medication(medication_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    ensure_can_manage(p)
    if not await repo.delete_medication(p, medication_id):
        raise _not_found("Medication not found.")
    return None


@router.get("/people/{person_id}/medication-events", dependencies=[Depends(person_guard)])
async def list_medication_events(person_id: uuid.UUID, limit: int = Query(100, ge=1, le=1000),
                                 p: Principal = Depends(require_workspace_principal)):
    ensure_can_read(p)
    async with workspace_connection(p) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            raise _not_found("Person not found.")
        events = await repo.list_medication_events(conn, person_id, limit=limit)
        await audit.record_access(conn, p, person_id=person_id, resource_kind="medication",
                                  detail={"view": "events", "rows": len(events)})
    return {"events": events, "count": len(events)}


@router.post("/people/{person_id}/medication-events", status_code=201, dependencies=[Depends(person_guard)])
async def record_medication_event(person_id: uuid.UUID, body: schemas.MedicationEventRecord,
                                  p: Principal = Depends(require_workspace_principal)):
    """A family member or carer confirming a dose was taken (or missed). Who
    recorded it is taken from the signed-in caller, not from the request."""
    ensure_can_act(p)
    try:
        event = await repo.record_medication_event(p, person_id, body.model_dump())
    except repo.DoseConflict:
        raise HTTPException(
            status_code=409,
            detail="This dose is already recorded differently. It is not overwritten. "
                   "To correct it, send the change again with a correction_reason; the old record is kept.")
    if event is None:
        raise _not_found("Medication not found for this person.")
    return event


@router.get("/people/{person_id}/medication-corrections", dependencies=[Depends(person_guard)])
async def list_medication_corrections(person_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    """Every change made to a dose already recorded: what it was, what it became, who changed it and why."""
    ensure_can_read(p)
    async with workspace_connection(p) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            raise _not_found("Person not found.")
        rows = await repo.list_medication_corrections(conn, person_id)
        await audit.record_access(conn, p, person_id=person_id, resource_kind="medication",
                                  detail={"view": "corrections", "rows": len(rows)})
    return {"corrections": rows, "count": len(rows)}


# ── Provider status callbacks ────────────────────────────────────────────────

#: Fields Twilio sends on a status callback; anything else is dropped before
#: the signature is checked against what was actually received.
_TWILIO_FIELD_LIMIT = 64


@router.post("/providers/twilio/status/{tenant_id}/{workspace_id}/{attempt_id}", status_code=204)
async def twilio_status(tenant_id: uuid.UUID, workspace_id: uuid.UUID, attempt_id: uuid.UUID, request: Request,
                        t: str = Query("", max_length=80),
                        x_twilio_signature: Optional[str] = Header(default=None, alias="X-Twilio-Signature")):
    """How far an SMS or a call got, from Twilio. Refused with nothing written
    unless BOTH our token in the URL and Twilio's signature over the full URL
    and the form verify. States only move forward, so a repeat or a late
    callback changes nothing. The message's words are never logged."""
    import os

    from fastapi import Response

    from . import delivery
    from .checkin_cycle import job_principal
    form = await request.form()
    if len(form) > _TWILIO_FIELD_LIMIT:
        raise HTTPException(status_code=400, detail="Too many fields.")
    params = {k: str(v)[:500] for k, v in form.items()}
    url = delivery.status_callback_url(tenant_id, workspace_id, attempt_id)
    if url is None or not delivery.verify_token(tenant_id, workspace_id, attempt_id, t) or not delivery.verify_twilio(
            url, params, x_twilio_signature or "", (os.getenv("IRA_CARE_TWILIO_AUTH_TOKEN") or "").strip()):
        raise HTTPException(status_code=403, detail="Signature not valid.")
    status = (params.get("MessageStatus") or params.get("CallStatus") or "").strip().lower()
    sid = (params.get("MessageSid") or params.get("CallSid") or "").strip()[:200]
    outcome = delivery.TWILIO_STATUS.get(status)
    if outcome is None:
        logger.info("care twilio callback: status %r not used (attempt %s)", status[:20], attempt_id)
        return Response(status_code=204)
    principal = job_principal(tenant_id, workspace_id)
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow("SELECT id, channel FROM care_alert_attempts WHERE id = $1", attempt_id)
        known = (await delivery.current_states(conn, [attempt_id])).get(attempt_id) if row else None
        if row is None or row["channel"] not in ("sms", "call") or (known["provider_ref"] and sid and known["provider_ref"] != sid):
            logger.warning("care twilio callback: no matching attempt %s", attempt_id)
            return Response(status_code=204)
        await delivery.record_state(conn, attempt_id, outcome, source="callback", provider_status=status,
                                    provider_ref=sid)
    return Response(status_code=204)


# ── Transcripts and consents ─────────────────────────────────────────────────

@router.post("/people/{person_id}/talk", dependencies=[Depends(person_guard)])
async def person_talk(person_id: uuid.UUID, body: schemas.Talk, p: Principal = Depends(require_workspace_principal)):
    """Talk to IRA from the person's own phone (the family app, signed in as a
    member). The same stream as the watch; a call action shows the contact and
    the phone dials as the person's own action."""
    from fastapi.responses import StreamingResponse
    from . import talk
    ensure_can_act(p)
    async with workspace_connection(p) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            raise _not_found("Person not found.")
        # Only the person themself may talk AS the person: an active member of
        # THIS person's circle whose relationship is 'self'. A family member, a
        # carer, a workspace admin or another wearer would otherwise put words in
        # the person's mouth: transcripts, memory, reminders and alerts. Checked
        # before the stream starts, so a refusal writes nothing.
        if not await repo.is_wearer(conn, person_id, p.user_id):
            raise HTTPException(status_code=403,
                                detail="Only the person themself can talk to IRA as this person.")
    return StreamingResponse(talk.ndjson(talk.exchange(p, person_id, body.text, source="phone")),
                             media_type="application/x-ndjson")


@router.get("/people/{person_id}/memory", dependencies=[Depends(person_guard)])
async def person_memory(person_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    """What IRA remembers: the rolling summaries of the last week. Summaries,
    never the person's words; still health-adjacent, so the read is logged."""
    from . import talk
    ensure_can_read(p)
    async with workspace_connection(p) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            raise _not_found("Person not found.")
        await audit.record_access(conn, p, person_id=person_id, resource_kind="checkins", detail={"memory": True})
    return {"memory": await talk.memory_for(p, person_id)}


@router.get("/people/{person_id}/transcripts", dependencies=[Depends(person_guard)])
async def get_transcripts(person_id: uuid.UUID, limit: int = Query(100, ge=1, le=1000),
                          p: Principal = Depends(require_workspace_principal)):
    """What the person said. Only with a recorded consent that covers the caller's
    role in the care circle; refused otherwise, and every read is logged."""
    ensure_can_read(p)
    async with workspace_connection(p) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            raise _not_found("Person not found.")
        role = await repo.circle_role_of(conn, person_id, p.user_id)
        if role is None and p.is_owner:
            role = "carer"
        consents = await repo.fetch_consents(conn, person_id)
        if not consent.may_read_transcripts(role, consents):
            raise HTTPException(
                status_code=403,
                detail="Transcripts are private. They can be read only with the person's "
                       "recorded consent for your role in their care circle.")
        rows = await repo.fetch_transcripts(conn, person_id, limit=limit)
        await audit.record_access(conn, p, person_id=person_id, resource_kind="transcript",
                                  detail={"rows": len(rows), "circle_role": role})
    return {"transcripts": rows, "count": len(rows)}


@router.get("/people/{person_id}/consents", dependencies=[Depends(person_guard)])
async def list_consents(person_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    ensure_can_read(p)
    async with workspace_connection(p) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            raise _not_found("Person not found.")
        rows = await repo.fetch_consents(conn, person_id)
    effective = consent.effective_consents(rows)
    return {
        "consents": rows,
        "effective": {k: (bool(v["granted"]) and v["revoked_at"] is None) for k, v in effective.items()},
        "retention_months": consent.retention_months(rows),
    }


@router.post("/people/{person_id}/consents", status_code=201, dependencies=[Depends(person_guard)])
async def record_consent(person_id: uuid.UUID, body: schemas.ConsentRecord,
                         p: Principal = Depends(require_workspace_principal)):
    ensure_can_manage(p)
    row = await repo.record_consent(p, person_id, body.model_dump())
    if row is None:
        raise _not_found("Person not found.")
    return row


@router.post("/consents/{consent_id}/revoke", dependencies=[Depends(_RESOURCE_GUARDS["consent_id"])])
async def revoke_consent(consent_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    ensure_can_manage(p)
    row = await repo.revoke_consent(p, consent_id)
    if row is None:
        raise _not_found("Consent not found or already revoked.")
    return row


# ── Access log ───────────────────────────────────────────────────────────────

@router.get("/people/{person_id}/access-log", dependencies=[Depends(person_guard)])
async def get_access_log(person_id: uuid.UUID, limit: int = Query(200, ge=1, le=5000),
                         p: Principal = Depends(require_workspace_principal)):
    ensure_can_manage(p)
    rows = await repo.fetch_access_log(p, person_id, limit=limit)
    if rows is None:
        raise _not_found("Person not found.")
    return {"entries": rows, "count": len(rows)}


# ── Reports and history ──────────────────────────────────────────────────────

@router.get("/people/{person_id}/reports", dependencies=[Depends(person_guard)])
async def list_reports(person_id: uuid.UUID, kind: Optional[str] = Query(None), limit: int = Query(30, ge=1, le=200),
                       p: Principal = Depends(require_workspace_principal)):
    ensure_can_read(p)
    if kind is not None and kind not in (reports.DAILY, reports.WEEKLY):
        raise HTTPException(status_code=422, detail=f"kind must be {reports.DAILY} or {reports.WEEKLY}")
    async with workspace_connection(p) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            raise _not_found("Person not found.")
        rows = await reports.list_reports(conn, person_id, kind=kind, limit=limit)
        await audit.record_access(conn, p, person_id=person_id, resource_kind="report", detail={"rows": len(rows), "kind": kind})
    return {"reports": rows, "count": len(rows)}


@router.post("/people/{person_id}/reports/generate", status_code=201, dependencies=[Depends(person_guard)])
async def generate_report(person_id: uuid.UUID, body: schemas.GenerateReport, p: Principal = Depends(require_workspace_principal)):
    """Generate (or fetch, if it exists) the report for a day or the week containing it."""
    ensure_can_act(p)
    async with workspace_connection(p) as conn:
        person = await conn.fetchrow("SELECT * FROM care_people WHERE id = $1", person_id)
        if person is None:
            raise _not_found("Person not found.")
        row = await reports.generate(conn, dict(person), body.kind, body.date)
        if row is None:
            start = body.date if body.kind == reports.DAILY else body.date - dt.timedelta(days=body.date.weekday())
            row = (await conn.fetchrow("SELECT * FROM care_reports WHERE person_id = $1 AND kind = $2 AND period_start = $3",
                                       person_id, body.kind, start))
            row = reports._public(row) if row else None
        await audit.record_access(conn, p, person_id=person_id, resource_kind="report", resource_id=row["id"] if row else None,
                                  action="view", detail={"generated": True, "kind": body.kind})
    return row


@router.get("/reports/{report_id}", dependencies=[Depends(_RESOURCE_GUARDS["report_id"])])
async def get_report(report_id: uuid.UUID, p: Principal = Depends(require_workspace_principal)):
    ensure_can_read(p)
    async with workspace_connection(p) as conn:
        row = await reports.get_report(conn, report_id)
        if row is None:
            raise _not_found("Report not found.")
        await audit.record_access(conn, p, person_id=row["person_id"], resource_kind="report", resource_id=report_id)
    return row


@router.get("/people/{person_id}/history/search", dependencies=[Depends(person_guard)])
async def search_history(person_id: uuid.UUID, q: str = Query(..., min_length=1, max_length=200),
                         since: Optional[dt.datetime] = Query(None), until: Optional[dt.datetime] = Query(None),
                         limit: int = Query(50, ge=1, le=500), p: Principal = Depends(require_workspace_principal)):
    """One search across check-in summaries, alerts and (under consent) transcripts. Audited."""
    ensure_can_read(p)
    async with workspace_connection(p) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            raise _not_found("Person not found.")
        return await history.search(conn, p, person_id, q, since=since, until=until, limit=limit)


# ── Operations ───────────────────────────────────────────────────────────────

@router.get("/overview")
async def overview(p: Principal = Depends(require_workspace_principal)):
    """The live overview for the care team: who needs attention, visits due or
    overdue, alerts awaiting a response, stale watches (care/overview.py)."""
    ensure_can_read(p)
    from . import overview as ov
    async with workspace_connection(p) as conn:
        return await ov.build(conn, visible=await authz.visible_people(conn, p))


@router.get("/ops/status")
async def ops_status(p: Principal = Depends(require_workspace_principal)):
    """Whether the care jobs are alive. The dashboard shows "monitoring paused" when not."""
    ensure_can_read(p)
    async with workspace_connection(p) as conn:
        return await ops.status(conn)
