"""Deployment administrators and signed device instructions against a real database (E2.o, D-085,
migration 122, care/deviceadmin.py, care/devicepolicy.py).

Who: only an account with a live row in `deployment_administrators`, which only the operator
writes. A care manager, coordinator, carer, viewer, the person themself, a tenant owner and a
principal flagged as the deployment owner are all refused, and their attempts leave nothing behind.
What: a closed list of actions, at most 15 minutes, for one paired watch named with its own
tenant and workspace; signed; collected only by that watch; revocable; every step audited.
Two people for a factory reset (migration 123, D-089): a request, signed only once a different,
live deployment administrator approves it within 30 minutes."""
from __future__ import annotations

import base64
import datetime as dt
import os
import re
import uuid
from dataclasses import replace

import pytest

asyncpg = pytest.importorskip("asyncpg")
pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from tests.tenancy._care_fixtures import _principal, client, families, pool  # noqa: E402,F401

from care import deviceadmin, devicepolicy  # noqa: E402
from scripts import deployment_admin as operator  # noqa: E402

C = "/api/v1/care"
ISSUE = f"{C}/deployment-admin/device-instructions"
RAW = bytes(range(1, 33))
KEY = ec.derive_private_key(int.from_bytes(RAW, "big"), ec.SECP256R1())
REASON = "support ticket 1234: maintenance"


@pytest.fixture
def policy_key(monkeypatch):
    monkeypatch.setenv(devicepolicy.KEY_ENV, base64.b64encode(RAW).decode())


@pytest.fixture
async def admins(client, families):
    """An operator's grant for a fresh account, and the client able to act as it. The account
    belongs to neither family: an IRA administrator looks after the fleet."""
    from api.middleware.workspace import require_tenant_principal

    client.app.dependency_overrides[require_tenant_principal] = lambda: client.principal
    conn = await asyncpg.connect(ADMIN_DSN)
    made: list[uuid.UUID] = []

    async def new(grant: bool = True) -> uuid.UUID:
        uid = uuid.uuid4()
        await conn.execute("INSERT INTO users (id, email, display_name, status) VALUES ($1, $2, 'ira ops', 'active')",
                           uid, f"ops-{uid.hex[:8]}@example.invalid")
        made.append(uid)
        if grant:
            await operator.grant(conn, uid, "fleet on-call")
        return uid
    new.conn = conn  # type: ignore[attr-defined]
    try:
        yield new
    finally:
        await conn.execute("RESET ROLE")
        await conn.execute("DELETE FROM deployment_administrators WHERE user_id = ANY($1::uuid[])", made)
        await conn.execute("DELETE FROM users WHERE id = ANY($1::uuid[])", made)
        await conn.close()


def act_as_admin(client, families, uid: uuid.UUID, *, auth_age: int = 0, amr=("pwd",)):
    """Signed in as an ordinary account of its own tenant, with no workspace and no roles, whose
    session proved its password `auth_age` seconds ago (D-095: the step-up check reads these
    from the session token; here they are put on the Principal as `principal_from_username`
    would). The default is an administrator who has just signed in."""
    from core.editions import EDITION_CLOUD
    from core.identity import Principal
    import time
    client.principal = Principal(user_id=uid, tenant_id=families["b"]["tenant_id"], session_id=uuid.uuid4(),
                                 edition=EDITION_CLOUD).with_authentication(int(time.time()) - auth_age, amr)
    return client


async def _watch(client, fam) -> tuple[str, str]:
    client.act_as(fam, "admin", "workspace_admin")
    person = (await client.post(f"{C}/people", json={"display_name": "Test Person", "timezone": "UTC"})).json()
    code = (await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})).json()["pairing_code"]
    paired = (await client.post(f"{C}/devices/pair", json={"pairing_code": code})).json()
    return paired["device_token"], str(paired["id"])


def _body(fam, device_id, action="MAINTENANCE_WINDOW", minutes=10, **kw):
    return {"tenant_id": str(fam["tenant_id"]), "workspace_id": str(fam["workspace_id"]), "device_id": device_id,
            "action": action, "minutes": minutes, "reason": REASON, **kw}


async def _collect(client, token):
    r = await client.post(f"{C}/devices/me/admin-instructions", headers={"X-Device-Token": token})
    assert r.status_code == 200, r.text
    return r.json()


async def _count(table: str, tenant_id) -> int:
    """Rows in this test's tenant only: other suites write the same table concurrently."""
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        return await conn.fetchval(f"SELECT count(*) FROM {table} WHERE tenant_id = $1", tenant_id)  # noqa: S608
    finally:
        await conn.close()


def _verify_as_the_watch(item: dict, device_id: str) -> dict:
    """What DevicePolicy.kt does, in Python: signature over the bytes received, then the fields."""
    KEY.public_key().verify(base64.b64decode(item["signature"]), item["payload"].encode(), ec.ECDSA(hashes.SHA256()))
    version, dev, action, issued, expires, nonce = item["payload"].split("|")
    assert version == devicepolicy.PAYLOAD_VERSION and dev == device_id and action in devicepolicy.ACTIONS
    assert 0 < int(expires) - int(issued) <= devicepolicy.MAX_WINDOW_MS
    assert re.fullmatch(r"[0-9a-f]{32}", nonce)
    return {"action": action, "nonce": nonce, "issued": int(issued), "expires": int(expires)}


async def test_no_care_or_tenant_role_is_a_deployment_administrator(client, families, admins, policy_key):
    token, device = await _watch(client, families["a"])
    fam = families["a"]
    from core.identity.db_context import workspace_connection
    async with workspace_connection(deviceadmin.target_principal(fam["tenant_id"], fam["workspace_id"])) as conn:
        # The member is the workspace's coordinator (D-081) for this test.
        await conn.execute("INSERT INTO care_staff_designations (user_id, designation) VALUES ($1, 'coordinator')",
                           fam["member"])
    before = await _count("care_device_admin_instructions", families["a"]["tenant_id"])
    ungranted = await admins(grant=False)
    callers = [
        ("care manager", _principal(fam, "admin", "workspace_admin")),
        ("coordinator", _principal(fam, "member", "member")),
        ("carer", _principal(fam, "member", "member")),
        ("viewer", _principal(fam, "viewer", "viewer")),
        ("the person themself", _principal(fam, "wearer", "member")),
        ("tenant owner, no workspace", _principal(fam, "admin", "workspace_admin").without_workspace()),
        ("deployment owner flag", replace(_principal(fam, "admin", "workspace_admin"), is_owner=True,
                                          roles=frozenset({"primary_owner"}))),
        ("an account with no grant", act_as_admin(client, families, ungranted).principal),
    ]
    for who, principal in callers:
        client.principal = principal
        r = await client.post(ISSUE, json=_body(fam, device))
        assert r.status_code == 403, (who, r.text)
        r = await client.post(f"{ISSUE}/{uuid.uuid4()}/revoke", json={"tenant_id": str(fam["tenant_id"]),
                              "workspace_id": str(fam["workspace_id"]), "reason": REASON})
        assert r.status_code == 403, (who, r.text)
        r = await client.get(ISSUE, params={"tenant_id": str(fam["tenant_id"]), "workspace_id": str(fam["workspace_id"]),
                                            "device_id": device})
        assert r.status_code == 403, (who, r.text)
    assert await _count("care_device_admin_instructions", families["a"]["tenant_id"]) == before
    assert (await _collect(client, token)) == {"instructions": [], "revoked_nonces": []}


async def test_an_instruction_is_signed_for_one_watch_collected_once_and_fully_audited(client, families, admins, policy_key):
    token, device = await _watch(client, families["a"])
    other_token, other_device = await _watch(client, families["a"])  # a second watch, same workspace
    uid = await admins()
    act_as_admin(client, families, uid)
    r = await client.post(ISSUE, json=_body(families["a"], device, minutes=5))
    assert r.status_code == 201, r.text
    issued = r.json()
    assert issued["action"] == "MAINTENANCE_WINDOW" and issued["issued_by"] == str(uid)
    assert "signature" not in issued and "payload" not in issued

    got = await _collect(client, token)
    assert [i["id"] for i in got["instructions"]] == [issued["id"]]
    fields = _verify_as_the_watch(got["instructions"][0], device)
    assert fields["action"] == "MAINTENANCE_WINDOW" and fields["expires"] - fields["issued"] == 5 * 60_000
    assert fields["nonce"] == issued["nonce"]
    assert (await _collect(client, other_token))["instructions"] == [], "another watch never sees it"
    again = await _collect(client, token)  # not yet reported on: handed over again, delivered once
    assert [i["id"] for i in again["instructions"]] == [issued["id"]]

    out = f"{C}/devices/me/admin-instructions/{issued['id']}/outcome"
    hdr = {"X-Device-Token": token}
    assert (await client.post(out, headers={"X-Device-Token": other_token},
                              json={"outcome": "applied"})).status_code == 404
    assert (await client.post(out, headers=hdr, json={"outcome": "restored"})).status_code == 409
    assert (await client.post(out, headers=hdr, json={"outcome": "applied"})).status_code == 200
    assert (await client.post(out, headers=hdr, json={"outcome": "applied"})).status_code == 409
    assert (await _collect(client, token))["instructions"] == [], "applied: never handed over again"
    assert (await client.post(out, headers=hdr, json={"outcome": "restored", "reason": "window expired"})).status_code == 200

    act_as_admin(client, families, uid)
    fam = families["a"]
    h = (await client.get(ISSUE, params={"tenant_id": str(fam["tenant_id"]), "workspace_id": str(fam["workspace_id"]),
                                         "device_id": device})).json()["instructions"]
    assert len(h) == 1 and h[0]["outcome"] == "restored" and h[0]["delivered_at"]
    trail = [(e["event"], e["actor_kind"], str(e["actor_id"])) for e in h[0]["events"]]
    assert trail == [("issued", "deployment_admin", str(uid)), ("delivered", "device", device),
                     ("applied", "device", device), ("restored", "device", device)]
    assert h[0]["events"][0]["detail"] == "MAINTENANCE_WINDOW for 5 min"
    assert (await client.get(ISSUE, params={"tenant_id": str(fam["tenant_id"]), "workspace_id": str(fam["workspace_id"]),
                                            "device_id": other_device})).json()["instructions"] == []


async def test_another_tenants_watch_cannot_be_named_reached_or_reported_on(client, families, admins, policy_key):
    token_a, device_a = await _watch(client, families["a"])
    token_b, device_b = await _watch(client, families["b"])
    uid = await admins()
    act_as_admin(client, families, uid)
    before = sum([await _count("care_device_admin_instructions", f["tenant_id"]) for f in families.values()])
    # A's watch named with B's tenant and workspace (and the mixed triples): not found, nothing written.
    for t, w in ((families["b"], families["b"]), (families["a"], families["b"]), (families["b"], families["a"])):
        body = _body(families["a"], device_a) | {"tenant_id": str(t["tenant_id"]), "workspace_id": str(w["workspace_id"])}
        assert (await client.post(ISSUE, json=body)).status_code == 404
    assert sum([await _count("care_device_admin_instructions", f["tenant_id"]) for f in families.values()]) == before

    issued = (await client.post(ISSUE, json=_body(families["a"], device_a))).json()
    assert (await _collect(client, token_b)) == {"instructions": [], "revoked_nonces": []}
    r = await client.post(f"{C}/devices/me/admin-instructions/{issued['id']}/outcome",
                          headers={"X-Device-Token": token_b}, json={"outcome": "refused"})
    assert r.status_code == 404
    act_as_admin(client, families, uid)
    r = await client.post(f"{ISSUE}/{issued['id']}/revoke", json={"tenant_id": str(families["b"]["tenant_id"]),
                          "workspace_id": str(families["b"]["workspace_id"]), "reason": REASON})
    assert r.status_code == 404, "revoking through another tenant's scope finds nothing"
    assert len((await _collect(client, token_a))["instructions"]) == 1


async def test_only_the_closed_list_and_at_most_fifteen_minutes(client, families, admins, policy_key, pool):
    token, device = await _watch(client, families["a"])
    act_as_admin(client, families, await admins())
    for action in ("RUN_SHELL", "maintenance_window", "LIFT_DEBUGGING", "MAINTENANCE_WINDOW; rm -rf /", ""):
        assert (await client.post(ISSUE, json=_body(families["a"], device, action=action))).status_code == 422, action
    for minutes in (0, 16, 600, -1):
        assert (await client.post(ISSUE, json=_body(families["a"], device, minutes=minutes))).status_code == 422, minutes
    assert (await client.post(ISSUE, json=_body(families["a"], device, reason="x"))).status_code == 422
    for action, minutes in (("MAINTENANCE_WINDOW", 16), ("RUN_SHELL", 5)):  # the module refuses on its own too
        with pytest.raises(ValueError):
            await deviceadmin.issue(admin_id=uuid.uuid4(), tenant_id=families["a"]["tenant_id"],
                                    workspace_id=families["a"]["workspace_id"], device_id=uuid.UUID(device),
                                    action=action, minutes=minutes, reason=REASON, key=KEY)
    assert (await _collect(client, token))["instructions"] == []

    # The table refuses the same things if the code ever did not.
    from core.identity.db_context import workspace_connection
    p = deviceadmin.target_principal(families["a"]["tenant_id"], families["a"]["workspace_id"])
    row = "($1, $2, $3, NOW(), NOW() + $4::interval, $5, 'a long enough reason', 'p', 's', 'k')"
    sql = ("INSERT INTO care_device_admin_instructions (device_id, action, nonce, issued_at, expires_at, issued_by,"
           f" reason, payload, signature, key_id) VALUES {row}")
    for action, window in (("RUN_SHELL", dt.timedelta(minutes=5)), ("MAINTENANCE_WINDOW", dt.timedelta(minutes=16)),
                           ("MAINTENANCE_WINDOW", dt.timedelta(0))):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with workspace_connection(p) as conn:
                await conn.execute(sql, uuid.UUID(device), action, uuid.uuid4().hex, window, uuid.uuid4())


async def test_revocation_reaches_the_watch_and_cannot_be_undone(client, families, admins, policy_key):
    token, device = await _watch(client, families["a"])
    uid = await admins()
    act_as_admin(client, families, uid)
    fam = families["a"]
    target = {"tenant_id": str(fam["tenant_id"]), "workspace_id": str(fam["workspace_id"]), "reason": REASON}
    first = (await client.post(ISSUE, json=_body(fam, device))).json()
    second = (await client.post(ISSUE, json=_body(fam, device))).json()
    await _collect(client, token)  # the watch already holds both
    r = await client.post(f"{ISSUE}/{first['id']}/revoke", json=target)
    assert r.status_code == 200 and r.json()["revoked_by"] == str(uid)
    assert (await client.post(f"{ISSUE}/{first['id']}/revoke", json=target)).status_code == 409
    assert (await client.post(f"{ISSUE}/{uuid.uuid4()}/revoke", json=target)).status_code == 404
    got = await _collect(client, token)
    assert [i["id"] for i in got["instructions"]] == [second["id"]]
    assert got["revoked_nonces"] == [first["nonce"]]

    act_as_admin(client, families, uid)
    h = (await client.get(ISSUE, params={"tenant_id": str(fam["tenant_id"]), "workspace_id": str(fam["workspace_id"]),
                                         "device_id": device})).json()["instructions"]
    revoked = next(i for i in h if i["id"] == first["id"])
    assert [e["event"] for e in revoked["events"]] == ["issued", "delivered", "revoked"]
    assert revoked["events"][-1]["detail"] == REASON

    from core.identity.db_context import workspace_connection
    p = deviceadmin.target_principal(fam["tenant_id"], fam["workspace_id"])
    for sql in ("UPDATE care_device_admin_instructions SET revoked_at = NULL, revoked_by = NULL WHERE id = $1",
                "UPDATE care_device_admin_instructions SET expires_at = expires_at + interval '1 minute' WHERE id = $1",
                "UPDATE care_device_admin_instructions SET payload = 'x' WHERE id = $1",
                "DELETE FROM care_device_admin_instructions WHERE id = $1",
                "DELETE FROM care_device_admin_events WHERE instruction_id = $1"):
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            async with workspace_connection(p) as conn:
                await conn.execute(sql, uuid.UUID(first["id"]))


async def test_a_revoked_administrator_can_do_nothing_and_what_they_issued_is_withdrawn(client, families, admins, policy_key):
    token, device = await _watch(client, families["a"])
    uid = await admins()
    act_as_admin(client, families, uid)
    held = (await client.post(ISSUE, json=_body(families["a"], device))).json()
    await _collect(client, token)
    # Signed but not yet collected. (A factory reset would not be signed at all on one person's
    # say-so since 123; what happens to one is tested under "Two people for a factory reset".)
    pending = (await client.post(ISSUE, json=_body(families["a"], device, action="RESTORE_RESTRICTIONS"))).json()
    assert await operator.revoke(admins.conn, uid) == 1
    assert await operator.revoke(admins.conn, uid) == 0

    act_as_admin(client, families, uid)
    assert (await client.post(ISSUE, json=_body(families["a"], device))).status_code == 403
    fam = families["a"]
    assert (await client.post(f"{ISSUE}/{held['id']}/revoke", json={"tenant_id": str(fam["tenant_id"]),
            "workspace_id": str(fam["workspace_id"]), "reason": REASON})).status_code == 403
    got = await _collect(client, token)
    assert got["instructions"] == [], "nothing a revoked administrator issued is handed over"
    assert sorted(got["revoked_nonces"]) == sorted([held["nonce"], pending["nonce"]])
    # A second, live administrator is unaffected by the first one's revocation.
    act_as_admin(client, families, await admins())
    assert (await client.post(ISSUE, json=_body(fam, device))).status_code == 201


async def test_an_expired_instruction_is_never_handed_over(client, families, admins, policy_key):
    token, device = await _watch(client, families["a"])
    uid = await admins()
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
    old = await deviceadmin.issue(admin_id=uid, tenant_id=families["a"]["tenant_id"],
                                  workspace_id=families["a"]["workspace_id"], device_id=uuid.UUID(device),
                                  action="MAINTENANCE_WINDOW", minutes=15, reason=REASON, key=KEY, now=past)
    assert old["expires_at"] < dt.datetime.now(dt.timezone.utc)
    assert (await _collect(client, token)) == {"instructions": [], "revoked_nonces": []}


async def test_only_a_paired_watch_collects_and_only_a_paired_watch_can_be_named(client, families, admins, policy_key):
    fam = families["a"]
    client.act_as(fam, "admin", "workspace_admin")
    person = (await client.post(f"{C}/people", json={"display_name": "Test Person", "timezone": "UTC"})).json()
    pending = (await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})).json()
    act_as_admin(client, families, await admins())
    assert (await client.post(ISSUE, json=_body(fam, pending["id"]))).status_code == 404
    assert (await client.post(f"{C}/devices/me/admin-instructions")).status_code == 401
    assert (await client.post(f"{C}/devices/me/admin-instructions",
                              headers={"X-Device-Token": "cd1.bogus.bogus"})).status_code == 401


async def test_no_key_means_no_instruction(client, families, admins, monkeypatch):
    monkeypatch.delenv(devicepolicy.KEY_ENV, raising=False)
    _, device = await _watch(client, families["a"])
    act_as_admin(client, families, await admins())
    before = await _count("care_device_admin_instructions", families["a"]["tenant_id"])
    assert (await client.post(ISSUE, json=_body(families["a"], device))).status_code == 503
    assert await _count("care_device_admin_instructions", families["a"]["tenant_id"]) == before


async def test_the_runtime_cannot_make_anyone_an_administrator_even_with_every_grant(pool, admins):
    """R-47: a provisioning step that hands ira_app ALL on every table must not hand it this.
    Row-level security is the write lock; the grant is restored afterwards."""
    victim = await admins(grant=False)
    conn = admins.conn
    async with pool.acquire() as app:
        for sql in ("INSERT INTO deployment_administrators (user_id, label) VALUES ($1, 'self-granted')",
                    "UPDATE deployment_administrators SET revoked_at = NULL WHERE user_id = $1",
                    "DELETE FROM deployment_administrators WHERE user_id = $1"):
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await app.execute(sql, victim)
        await conn.execute("RESET ROLE")
        await conn.execute("GRANT ALL ON deployment_administrators TO ira_app")
        try:
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await app.execute("INSERT INTO deployment_administrators (user_id, label) VALUES ($1, 'self-granted')",
                                  victim)
            assert await app.execute("DELETE FROM deployment_administrators") == "DELETE 0"
            assert await app.execute("UPDATE deployment_administrators SET revoked_at = NULL") == "UPDATE 0"
        finally:
            await conn.execute("REVOKE ALL ON deployment_administrators FROM ira_app")
            await conn.execute("GRANT SELECT ON deployment_administrators TO ira_app")
        assert not await deviceadmin.is_deployment_admin(app, victim)


async def test_the_operator_grants_and_revokes_with_the_migrators_credentials(admins):
    """The documented path: scripts/deployment_admin.py as ira_migrator (SET ROLE ira_schema_owner)."""
    who = await admins(grant=False)
    migrator_dsn = re.sub(r"//[^@]+@", "//ira_migrator:ira_migrator@", ADMIN_DSN)
    conn = await asyncpg.connect(migrator_dsn)
    try:
        await operator.grant(conn, who, "migrator path")
        assert await deviceadmin.is_deployment_admin(admins.conn, who)
        assert await operator.revoke(conn, who) == 1
        assert not await deviceadmin.is_deployment_admin(admins.conn, who)
    finally:
        await conn.close()


async def test_the_runtime_can_ask_about_its_caller_and_cannot_list_administrators(pool, families, admins):
    """A live grant is visible only on a connection bound to its own account."""
    from core.identity.db_context import tenant_connection
    live = await admins()
    gone = await admins()
    await operator.revoke(admins.conn, gone)
    me = act_as_admin_principal(families, live)
    async with tenant_connection(me) as conn:
        assert await deviceadmin.is_deployment_admin(conn, live)
    someone_else = act_as_admin_principal(families, uuid.uuid4())
    async with tenant_connection(someone_else) as conn:
        assert not await deviceadmin.is_deployment_admin(conn, live), "asked about another account: no"
        visible = {r["user_id"] for r in await conn.fetch("SELECT user_id FROM deployment_administrators")}
        assert live not in visible and gone in visible, "live grants are not listable; revoked ones are"
    async with pool.acquire() as conn:  # no account bound at all
        assert live not in {r["user_id"] for r in await conn.fetch("SELECT user_id FROM deployment_administrators")}


def act_as_admin_principal(families, uid: uuid.UUID):
    from core.editions import EDITION_CLOUD
    from core.identity import Principal
    return Principal(user_id=uid, tenant_id=families["b"]["tenant_id"], session_id=uuid.uuid4(), edition=EDITION_CLOUD)


# ── Two people for a factory reset (migration 123, D-089, security review S15) ──────────────


def _target(fam) -> dict:
    return {"tenant_id": str(fam["tenant_id"]), "workspace_id": str(fam["workspace_id"])}


async def _history(client, fam, device) -> list[dict]:
    r = await client.get(ISSUE, params={**_target(fam), "device_id": device})
    assert r.status_code == 200, r.text
    return r.json()["instructions"]


async def _row(instruction_id) -> dict:
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        return dict(await conn.fetchrow("SELECT * FROM care_device_admin_instructions WHERE id = $1",
                                        uuid.UUID(str(instruction_id))))
    finally:
        await conn.close()


async def _request_reset(client, families, uid, device, minutes=10) -> dict:
    act_as_admin(client, families, uid)
    r = await client.post(ISSUE, json=_body(families["a"], device, action="FACTORY_RESET", minutes=minutes))
    assert r.status_code == 202, r.text
    return r.json()


async def _approve(client, families, uid, instruction_id):
    act_as_admin(client, families, uid)
    return await client.post(f"{ISSUE}/{instruction_id}/approve", json=_target(families["a"]))


async def test_a_factory_reset_is_signed_only_when_a_second_administrator_approves(client, families, admins,
                                                                                    policy_key):
    token, device = await _watch(client, families["a"])
    fam = families["a"]
    first, second, ungranted = await admins(), await admins(), await admins(grant=False)

    req = await _request_reset(client, families, first, device, minutes=5)
    assert req["state"] == "pending_approval" and req["status"] == "awaiting_approval"
    assert req["nonce"] is None and req["issued_at"] is None and req["expires_at"] is None
    assert req["issued_by"] == str(first) and req["window_minutes"] == 5
    stored = await _row(req["id"])
    assert stored["payload"] is None and stored["signature"] is None, "nothing signed yet"
    assert (await _collect(client, token)) == {"instructions": [], "revoked_nonces": []}

    # The requester cannot approve their own request: refused, audited, still unsigned.
    r = await _approve(client, families, first, req["id"])
    assert r.status_code == 403, r.text
    # An account with no grant cannot approve at all.
    r = await _approve(client, families, ungranted, req["id"])
    assert r.status_code == 403 and r.json()["message"] == deviceadmin.REFUSED
    assert (await _row(req["id"]))["state"] == "pending_approval"
    assert (await _collect(client, token))["instructions"] == []

    # A different, live administrator approves: signed now, for the five minutes asked for.
    r = await _approve(client, families, second, req["id"])
    assert r.status_code == 200, r.text
    signed = r.json()
    assert signed["state"] == "signed" and signed["approved_by"] == str(second) and signed["issued_by"] == str(first)
    got = await _collect(client, token)
    assert [i["id"] for i in got["instructions"]] == [req["id"]]
    fields = _verify_as_the_watch(got["instructions"][0], device)
    assert fields["action"] == "FACTORY_RESET" and fields["expires"] - fields["issued"] == 5 * 60_000
    assert fields["nonce"] == signed["nonce"]
    assert (await _approve(client, families, second, req["id"])).status_code == 409, "approved once only"

    h = next(i for i in await _history(client, fam, device) if i["id"] == req["id"])
    trail = [(e["event"], str(e["actor_id"])) for e in h["events"]]
    assert trail == [("requested", str(first)), ("approval_refused", str(first)), ("approved", str(second)),
                     ("delivered", device)]
    assert "second, different" in h["events"][1]["detail"]

    # The other two actions are unchanged: one administrator, signed at once.
    for action in ("MAINTENANCE_WINDOW", "RESTORE_RESTRICTIONS"):
        act_as_admin(client, families, first)
        r = await client.post(ISSUE, json=_body(fam, device, action=action))
        assert r.status_code == 201 and r.json()["state"] == "signed" and r.json()["nonce"], action


async def test_a_request_not_approved_in_time_lapses_unsigned(client, families, admins, policy_key):
    token, device = await _watch(client, families["a"])
    first, second = await admins(), await admins()
    asked = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=deviceadmin.APPROVAL_MINUTES + 1)
    req = await deviceadmin.issue(admin_id=first, tenant_id=families["a"]["tenant_id"],
                                  workspace_id=families["a"]["workspace_id"], device_id=uuid.UUID(device),
                                  action="FACTORY_RESET", minutes=10, reason=REASON, key=KEY, now=asked)
    r = await _approve(client, families, second, req["id"])
    assert r.status_code == 409 and "lapsed" in r.json()["message"]
    assert (await _row(req["id"]))["signature"] is None
    assert (await _collect(client, token)) == {"instructions": [], "revoked_nonces": []}
    act_as_admin(client, families, second)
    h = next(i for i in await _history(client, families["a"], device) if i["id"] == str(req["id"]))
    assert h["status"] == "lapsed" and [e["event"] for e in h["events"]] == ["requested", "approval_refused"]

    # Control: the same request made a minute inside the window is approved.
    inside = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=deviceadmin.APPROVAL_MINUTES - 1)
    ok = await deviceadmin.issue(admin_id=first, tenant_id=families["a"]["tenant_id"],
                                 workspace_id=families["a"]["workspace_id"], device_id=uuid.UUID(device),
                                 action="FACTORY_RESET", minutes=10, reason=REASON, key=KEY, now=inside)
    assert (await _approve(client, families, second, ok["id"])).status_code == 200


async def test_revoking_either_administrator_before_signing_kills_the_request(client, families, admins, policy_key):
    token, device = await _watch(client, families["a"])
    fam = families["a"]

    # The requester is revoked: nobody can approve what they asked for.
    requester, approver = await admins(), await admins()
    req = await _request_reset(client, families, requester, device)
    assert await operator.revoke(admins.conn, requester) == 1
    r = await _approve(client, families, approver, req["id"])
    assert r.status_code == 409 and "no longer a deployment administrator" in r.json()["message"]
    assert (await _row(req["id"]))["state"] == "pending_approval"

    # The would-be approver is revoked: they can approve nothing; a third, live one still can.
    requester2, approver2, third = await admins(), await admins(), await admins()
    req2 = await _request_reset(client, families, requester2, device)
    assert await operator.revoke(admins.conn, approver2) == 1
    assert (await _approve(client, families, approver2, req2["id"])).status_code == 403
    assert (await _row(req2["id"]))["state"] == "pending_approval"
    assert (await _approve(client, families, third, req2["id"])).status_code == 200

    # A request withdrawn (revoked) before approval cannot be approved.
    req3 = await _request_reset(client, families, third, device)
    act_as_admin(client, families, third)
    assert (await client.post(f"{ISSUE}/{req3['id']}/revoke", json={**_target(fam), "reason": REASON})).status_code == 200
    r = await _approve(client, families, requester2, req3["id"])
    assert r.status_code == 409 and "withdrawn" in r.json()["message"]

    got = await _collect(client, token)
    assert [i["id"] for i in got["instructions"]] == [req2["id"]], "only the properly approved reset"
    act_as_admin(client, families, third)
    h = {i["id"]: i for i in await _history(client, fam, device)}
    assert [e["event"] for e in h[req["id"]]["events"]] == ["requested", "approval_refused"]
    assert h[req3["id"]]["status"] == "withdrawn"
    assert [e["event"] for e in h[req3["id"]]["events"]] == ["requested", "revoked", "approval_refused"]


@pytest.mark.parametrize("who", ["requester", "approver"])
async def test_revoking_either_administrator_after_signing_withdraws_the_reset(client, families, admins,
                                                                                policy_key, who):
    token, device = await _watch(client, families["a"])
    requester, approver = await admins(), await admins()
    req = await _request_reset(client, families, requester, device)
    signed = (await _approve(client, families, approver, req["id"])).json()
    assert [i["id"] for i in (await _collect(client, token))["instructions"]] == [req["id"]]
    assert await operator.revoke(admins.conn, requester if who == "requester" else approver) == 1
    got = await _collect(client, token)
    assert got["instructions"] == [] and got["revoked_nonces"] == [signed["nonce"]]


async def test_a_one_person_reset_from_before_123_is_withdrawn_not_delivered(client, families, admins, policy_key):
    """A FACTORY_RESET signed on one administrator's say-so (possible before migration 123) is put
    on the watch's revocation list. Such a row can no longer be written; the test writes it the
    way 122 would have, with the 123 triggers off for this one session only."""
    token, device = await _watch(client, families["a"])
    uid = await admins()
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    ins = devicepolicy.new_instruction(device, "FACTORY_RESET", now_ms=int(now.timestamp() * 1000))
    sig = devicepolicy.signed(ins, KEY)
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute("SET session_replication_role = replica")
        await conn.execute(
            "INSERT INTO care_device_admin_instructions (tenant_id, workspace_id, device_id, action, nonce, issued_at,"
            " expires_at, issued_by, reason, payload, signature, key_id)"
            " VALUES ($1, $2, $3, 'FACTORY_RESET', $4, $5, $6, $7, $8, $9, $10, $11)",
            families["a"]["tenant_id"], families["a"]["workspace_id"], uuid.UUID(device), ins.nonce, now,
            now + dt.timedelta(minutes=10), uid, REASON, sig["payload"], sig["signature"], sig["key_id"])
    finally:
        await conn.close()
    got = await _collect(client, token)
    assert got["instructions"] == [] and got["revoked_nonces"] == [ins.nonce]


async def test_the_table_refuses_a_one_person_reset_whatever_the_code_does(client, families, admins, policy_key):
    _, device = await _watch(client, families["a"])
    fam = families["a"]
    requester, approver = await admins(), await admins()
    from core.identity.db_context import workspace_connection
    p = deviceadmin.target_principal(fam["tenant_id"], fam["workspace_id"])

    async def runtime(sql, *args):
        async with workspace_connection(p) as conn:
            return await conn.execute(sql, *args)

    # A reset written straight in as signed, as 122 allowed: refused.
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await runtime("INSERT INTO care_device_admin_instructions (device_id, action, nonce, expires_at, issued_by,"
                      " reason, payload, signature, key_id) VALUES ($1, 'FACTORY_RESET', $2, NOW() + interval"
                      " '5 minutes', $3, 'a long enough reason', 'p', 's', 'k')",
                      uuid.UUID(device), uuid.uuid4().hex, requester)
    # Requests longer than 30 minutes, or for another action, or half-signed: refused.
    req_sql = ("INSERT INTO care_device_admin_instructions (device_id, action, state, issued_at, issued_by, reason,"
               " requested_at, approval_deadline, window_minutes, nonce)"
               " VALUES ($1, $2, 'pending_approval', NULL, $3, 'a long enough reason', $4, $5, 10, $6)")
    now = dt.datetime.now(dt.timezone.utc)
    for action, deadline, nonce in (("FACTORY_RESET", now + dt.timedelta(minutes=31), None),
                                    ("MAINTENANCE_WINDOW", now + dt.timedelta(minutes=10), None),
                                    ("FACTORY_RESET", now + dt.timedelta(minutes=10), uuid.uuid4().hex)):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await runtime(req_sql, uuid.UUID(device), action, requester, now, deadline, nonce)

    async def pending(asked: dt.datetime) -> uuid.UUID:
        rid = uuid.uuid4()
        await runtime(req_sql.replace("(device_id,", "(id, device_id,").replace("VALUES ($1,", "VALUES ($7, $1,"),
                      uuid.UUID(device), "FACTORY_RESET", requester, asked, asked + dt.timedelta(minutes=30), None, rid)
        return rid

    approve_sql = ("UPDATE care_device_admin_instructions SET state = 'signed', nonce = $2, issued_at = NOW(),"
                   " expires_at = NOW() + interval '10 minutes', payload = 'p', signature = 's', key_id = 'k',"
                   " approved_by = $3, approved_at = NOW() WHERE id = $1")
    rid = await pending(now)
    for approved_by in (requester, None):  # the requester, or nobody
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await runtime(approve_sql, rid, uuid.uuid4().hex, approved_by)
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):  # approving and rewriting the reason
        await runtime(approve_sql.replace("state = 'signed',", "state = 'signed', reason = 'another reason here',"),
                      rid, uuid.uuid4().hex, approver)
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):  # signing without approving
        await runtime("UPDATE care_device_admin_instructions SET payload = 'p' WHERE id = $1", rid)
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await runtime("DELETE FROM care_device_admin_instructions WHERE id = $1", rid)
    lapsed = await pending(now - dt.timedelta(minutes=31))
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await runtime(approve_sql, lapsed, uuid.uuid4().hex, approver)
    # Control: the one legitimate change, by somebody else in time, is accepted, and cannot go back.
    assert await runtime(approve_sql, rid, uuid.uuid4().hex, approver) == "UPDATE 1"
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await runtime("UPDATE care_device_admin_instructions SET state = 'pending_approval' WHERE id = $1", rid)
    # A requester revoked since asking: the table refuses the approval too.
    revoked_one = await pending(now)
    await operator.revoke(admins.conn, requester)
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await runtime(approve_sql, revoked_one, uuid.uuid4().hex, approver)


async def test_the_123_rollback_refuses_while_a_reset_request_exists(client, families, admins, policy_key):
    """Dropping the approval columns would lose who approved a wipe. The script runs inside a
    transaction that is always rolled back, with its own BEGIN and COMMIT taken out, so a
    rollback that did not refuse could still not damage the test database."""
    import pathlib
    _, device = await _watch(client, families["a"])
    await _request_reset(client, families, await admins(), device)
    script = (pathlib.Path(__file__).resolve().parents[3] / "postgres" / "rollback"
              / "123_device_admin_factory_reset_approval_down.sql").read_text(encoding="utf-8")
    body = "\n".join(line for line in script.splitlines() if line.strip() not in ("BEGIN;", "COMMIT;"))
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            with pytest.raises(asyncpg.exceptions.RaiseError, match="Refusing rollback"):
                await conn.execute(body)
        finally:
            await tx.rollback()
        assert await conn.fetchval("SELECT count(*) FROM information_schema.columns WHERE table_name ="
                                   " 'care_device_admin_instructions' AND column_name = 'approved_by'") == 1
    finally:
        await conn.close()
