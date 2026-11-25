"""The OTA update route against a real database (E4.d, care/ota.py, migration 120).

POST /care/devices/me/os-update: an unpaired caller gets 401; a paired watch gets a manifest
signed with the deployment's key, or nothing; never an image below its rollback index; a 0 %
stage and a withdrawn release reach nobody; no key means 503; and the runtime role cannot write
the catalogue at all (publishing is an operator action)."""
from __future__ import annotations

import base64
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")
pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from tests.tenancy._care_fixtures import client, families, pool  # noqa: E402,F401

from care import ota  # noqa: E402

C = "/api/v1/care"
SEED = bytes(range(1, 33))
KEY = Ed25519PrivateKey.from_private_bytes(SEED)


@pytest.fixture
def signing_key(monkeypatch):
    monkeypatch.setenv(ota.SIGNING_KEY_ENV, base64.b64encode(SEED).decode())


@pytest.fixture
async def catalogue():
    """Releases published as the operator would; removed afterwards (no residue)."""
    conn = await asyncpg.connect(ADMIN_DSN)
    run = uuid.uuid4().hex[:8]
    made: list[str] = []

    async def publish(index: int, pct: int = 100, **kw) -> str:
        rid = await ota.publish(conn, version=f"t{run}-{index}-{len(made)}", build_fingerprint=f"ira/ira_watch_cf/t{run}-{index}-{len(made)}:user",
                                rollback_index=index, payload_url=f"https://updates.example.invalid/{run}-{index}.zip",
                                payload_sha256="b" * 64, payload_size=123456, rollout_percent=pct, **kw)
        made.append(rid)
        return rid
    publish.conn = conn  # type: ignore[attr-defined]
    try:
        yield publish
    finally:
        await conn.execute("DELETE FROM os_releases WHERE id = ANY($1::uuid[])", made)
        await conn.close()


async def _watch(client, fam) -> tuple[str, str]:
    client.act_as(fam, "admin", "workspace_admin")
    person = (await client.post(f"{C}/people", json={"display_name": "Test Person", "timezone": "UTC"})).json()
    code = (await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})).json()["pairing_code"]
    paired = (await client.post(f"{C}/devices/pair", json={"pairing_code": code})).json()
    return paired["device_token"], str(paired.get("device_id") or paired.get("id") or "")


async def _ask(client, token: str | None, fingerprint: str, index: int):
    headers = {"X-Device-Token": token} if token else {}
    return await client.post(f"{C}/devices/me/os-update", headers=headers,
                             json={"build_fingerprint": fingerprint, "rollback_index": index})


async def test_an_unpaired_caller_is_refused(client, families, signing_key):
    assert (await _ask(client, None, "ira/ira_watch_cf/x:user", 0)).status_code == 401
    assert (await _ask(client, "not-a-device-token", "ira/ira_watch_cf/x:user", 0)).status_code == 401


async def test_a_paired_watch_gets_a_signed_manifest_it_can_verify(client, families, signing_key, catalogue):
    rid = await catalogue(7)
    token, _ = await _watch(client, families["a"])
    r = await _ask(client, token, "ira/ira_watch_cf/older:user", 6)
    assert r.status_code == 200, r.text
    signed = r.json()["update"]
    assert signed is not None
    body = ota.verify(signed, KEY.public_key(), device_id=__import__("json").loads(signed["manifest"])["device_id"])
    assert body["release_id"] == rid and body["rollback_index"] == 7


async def test_a_downgrade_is_never_offered(client, families, signing_key, catalogue):
    await catalogue(3)
    token, _ = await _watch(client, families["a"])
    r = await _ask(client, token, "ira/ira_watch_cf/newer:user", 9)
    assert r.status_code == 200 and r.json()["update"] is None


async def test_a_zero_stage_and_a_withdrawn_release_reach_nobody(client, families, signing_key, catalogue):
    await catalogue(40, pct=0)
    withdrawn = await catalogue(41)
    await ota.withdraw(catalogue.conn, withdrawn)
    token, _ = await _watch(client, families["a"])
    r = await _ask(client, token, "ira/ira_watch_cf/x:user", 39)
    assert r.json()["update"] is None


async def test_no_signing_key_means_no_answer(client, families, catalogue, monkeypatch):
    monkeypatch.delenv(ota.SIGNING_KEY_ENV, raising=False)
    await catalogue(50)
    token, _ = await _watch(client, families["a"])
    assert (await _ask(client, token, "ira/ira_watch_cf/x:user", 0)).status_code == 503


async def test_the_runtime_role_cannot_write_the_catalogue(pool):
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute("INSERT INTO os_releases (version, build_fingerprint, rollback_index, payload_url,"
                               " payload_sha256, payload_size) VALUES ('x1', 'ira/x/evil:user', 99,"
                               " 'https://e.invalid/x', $1, 1)", "c" * 64)
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute("UPDATE os_releases SET rollout_percent = 100")


async def test_the_catalogue_refuses_to_publish_below_the_highest_index(catalogue):
    await catalogue(60)
    with pytest.raises(ValueError, match="below the highest"):
        await catalogue(59)
