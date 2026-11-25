"""Deployment administrators and signed device instructions against a real database (E2.o, D-082,
migration 121, care/deviceadmin.py, care/devicepolicy.py).

Who: only an account with a live row in `deployment_administrators`, which only the operator
writes. A care manager, coordinator, carer, viewer, the person themself, a tenant owner and a
principal flagged as the deployment owner are all refused, and their attempts leave nothing behind.
What: a closed list of actions, at most 15 minutes, for one paired watch named with its own
tenant and workspace; signed; collected only by that watch; revocable; every step audited."""
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


def act_as_admin(client, families, uid: uuid.UUID):
    from core.editions import EDITION_CLOUD
    from core.identity import Principal
    # Signed in as an ordinary account of its own tenant, with no workspace and no roles.
    client.principal = Principal(user_id=uid, tenant_id=families["b"]["tenant_id"], session_id=uuid.uuid4(),
                                 edition=EDITION_CLOUD)
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


async def _count(table: str) -> int:
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        return await conn.fetchval(f"SELECT count(*) FROM {table}")  # noqa: S608 - constant names
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
    before = await _count("care_device_admin_instructions")
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
    assert await _count("care_device_admin_instructions") == before
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
    before = await _count("care_device_admin_instructions")
    # A's watch named with B's tenant and workspace (and the mixed triples): not found, nothing written.
    for t, w in ((families["b"], families["b"]), (families["a"], families["b"]), (families["b"], families["a"])):
        body = _body(families["a"], device_a) | {"tenant_id": str(t["tenant_id"]), "workspace_id": str(w["workspace_id"])}
        assert (await client.post(ISSUE, json=body)).status_code == 404
    assert await _count("care_device_admin_instructions") == before

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
    with pytest.raises(ValueError):
        await deviceadmin.issue(admin_id=uuid.uuid4(), tenant_id=families["a"]["tenant_id"],
                                workspace_id=families["a"]["workspace_id"], device_id=uuid.UUID(device),
                                action="MAINTENANCE_WINDOW", minutes=16, reason=REASON, key=KEY)
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
    pending = (await client.post(ISSUE, json=_body(families["a"], device, action="FACTORY_RESET"))).json()
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
    before = await _count("care_device_admin_instructions")
    assert (await client.post(ISSUE, json=_body(families["a"], device))).status_code == 503
    assert await _count("care_device_admin_instructions") == before


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
