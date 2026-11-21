"""Phase 4 batch 05 — a KEK rotation reaches every key it is responsible for.

`CLOUD_PHASE_STATUS.md` gives Phase 4 one instruction about inherited work:
*"migration `030`; re-verify after Phase 3"*. This is that re-verification, and
it found a defect I had introduced two batches earlier.

── What it found ───────────────────────────────────────────────────────────

`mfa_crypto`'s docstring states the hazard precisely: *"what WOULD strand them
is a KEK rotation with nothing to follow it: every wrapped DEK names the KEK it
was wrapped under, and `_unwrap_dek` refuses a mismatch."* P4-02 added a SECOND
population of KEK-wrapped keys — one per voice enrolment (`062`) — and did not
add the follow-up.

So `rotate_tenant` would have minted a new KEK, re-wrapped the connector DEK and
every MFA DEK, and left every voice record key wrapped under a KEK that no
longer matched. After the contract stage that is unrecoverable: `063` drops the
plaintext, the ciphertext is the only copy, and its key no longer unwraps. A
routine key rotation would have destroyed every enrolled voiceprint in the
deployment **and reported success**.

── Why the guard is structural rather than one more test ───────────────────

A test that checked "rotation re-wraps voice keys" would have been written after
the fix and would say nothing about the next table somebody adds. The defect was
not that voice was missed; it was that nothing connected "this table holds
KEK-wrapped material" to "rotation must reach it".

`test_every_kek_wrapped_table_is_reached_by_rotation` closes that: it asks the
LIVE SCHEMA which tables carry a `kek_id`, and requires each to be named in
`ROTATED`. Adding a third population without wiring rotation now fails a test
nobody had to remember to write.
"""
from __future__ import annotations

import base64
import json
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

for _k, _v in {
    "IRA_SECRET_KEY": "ci-test-secret-key-for-jwt",
    "IRA_ADMIN_PASSWORD": "ci-test-password",
    "POSTGRES_PASSWORD": "ci-test",
    "REDIS_PASSWORD": "ci-test",
    "VLLM_API_KEY": "ci-test",
}.items():
    os.environ.setdefault(_k, _v)

OLD = {"IRA_CONNECTOR_ENCRYPTION_KEY": base64.b64encode(b"O" * 32).decode(),
       "IRA_CONNECTOR_ENCRYPTION_KEY_ID": "kek-old"}
NEW = {"IRA_CONNECTOR_ENCRYPTION_KEY": base64.b64encode(b"N" * 32).decode(),
       "IRA_CONNECTOR_ENCRYPTION_KEY_ID": "kek-new"}
os.environ.setdefault("IRA_CONNECTOR_ENCRYPTION_KEY",
                      OLD["IRA_CONNECTOR_ENCRYPTION_KEY"])

TEMPLATE = "[0.31, 0.42, 0.53]"

#: Every table that holds key material wrapped under the KEK, and the function
#: responsible for re-wrapping it when the KEK changes.
#:
#: This is the list `test_every_kek_wrapped_table_is_reached_by_rotation`
#: compares against the live schema. It is a claim about coverage, so adding an
#: entry means the rotation genuinely reaches it — not that the test should stop
#: complaining.
ROTATED: dict[str, str] = {
    "tenant_data_keys":
        "rotate_tenant steps 1-5 — the connector DEK is re-minted and every "
        "credential re-sealed under the new version",
    "user_mfa_keys":
        "rotate_tenant step 6 — security.mfa_crypto.rewrap_tenant_mfa_keys; "
        "the DEK is re-wrapped, never re-minted, and no seed is decrypted",
    "voice_profiles":
        "rotate_tenant step 7 — security.voice_crypto.rewrap_tenant_voice_keys; "
        "per-ENROLMENT record keys, re-wrapped with no template decrypted",
}


@pytest.fixture
async def pool():
    """`utils.db._pool` pointed at the ADMIN dsn, restored afterwards.

    The admin DSN rather than the application one, deliberately: the fixtures
    below are the private-edition owner path and carry no `user_id`, so a
    connection subject to `voice_profiles`'s USER_PRIVATE policy matches none of
    them. A rotation run under a filtered role would re-wrap zero keys and
    report success — the P2-05 shape, and here it would leave every enrolment
    wrapped under a KEK about to be retired.
    """
    import utils.db as db_module

    # P4-16-ALLOW-SUPERUSER-POOL: the local single-owner path. The principal
    # here carries no user_id, so a role subject to voice_profiles's
    # USER_PRIVATE policy matches zero rows and the rotation would report
    # success having re-wrapped nothing — the P2-05 shape, which is worse than
    # the bypass. The CLOUD rotation path is covered under ira_app by
    # tests/tenancy/test_p4_acceptance.py, which is what makes this defensible.
    created = await asyncpg.create_pool(dsn=ADMIN_DSN, min_size=1, max_size=6)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


@pytest.fixture
async def admin(pool):
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute("DELETE FROM voice_profiles")
        yield conn
    finally:
        await conn.execute("DELETE FROM voice_profiles")
        await conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# The structural guard
# ─────────────────────────────────────────────────────────────────────────────


async def test_every_kek_wrapped_table_is_reached_by_rotation(admin):
    """**The guard whose absence let a KEK rotation destroy every voiceprint.**

    Asked of the live schema, not of a list somebody maintains: a `kek_id`
    column means "this table holds material wrapped under the key-encryption
    key", and every such table must be named in `ROTATED`.

    A test asserting "rotation re-wraps voice keys" would have been written
    after the fix and would say nothing about the next table. This one fails
    before the next one ships.
    """
    live = {r["table_name"] for r in await admin.fetch(
        "SELECT table_name FROM information_schema.columns "
        " WHERE table_schema = 'public' AND column_name = 'kek_id'")}

    assert live, "no table carries kek_id — the query is wrong, not the schema"

    unreached = sorted(live - set(ROTATED))
    assert not unreached, (
        f"table(s) holding KEK-wrapped key material that rotation does not "
        f"reach: {unreached}.\n\n"
        f"Every wrapped key names the KEK it was wrapped under and `_unwrap_dek` "
        f"refuses a mismatch, so a KEK rotation leaves these permanently "
        f"unopenable — and after the contract stage there is no plaintext to "
        f"fall back to. Wire it into `rotate_tenant` and add it to ROTATED with "
        f"the step that reaches it.")

    phantom = sorted(set(ROTATED) - live)
    assert not phantom, (
        f"ROTATED names table(s) with no kek_id column: {phantom}. A coverage "
        f"claim about a table that holds no key material is padding, and it "
        f"makes the real entries harder to trust.")


def test_every_rotated_table_names_the_step_that_reaches_it():
    """"Covered" has to mean something a reader can check."""
    for table, how in ROTATED.items():
        assert "rotate_tenant" in how, f"{table}: {how!r} names no rotation step"
        assert len(how) > 40, f"{table}: the coverage note says nothing useful"


def test_the_rotation_result_reports_every_population_separately():
    """A single "keys rotated" count would hide a population that moved zero.

    Each of the three is a different operation on different material, and a
    rotation that re-wrapped no voice key when there were voice keys must be
    distinguishable from one that had none.
    """
    from core.connectors.keyring import RotationResult

    fields = set(RotationResult.__dataclass_fields__)
    for expected in ("credentials_rewrapped", "mfa_keys_rewrapped",
                     "voice_keys_rewrapped", "mfa_keys_failed",
                     "voice_keys_failed"):
        assert expected in fields, f"RotationResult does not report {expected}"


# ─────────────────────────────────────────────────────────────────────────────
# The behaviour, against a real rotation
# ─────────────────────────────────────────────────────────────────────────────


async def _enrolled(admin, suffix: str = "") -> dict:
    from security.voice_crypto import seal_template

    row = await admin.fetchrow(
        "INSERT INTO voice_profiles (owner_name, embedding) VALUES ($1, '') "
        "RETURNING id, enrollment_version", f"p4-rot{suffix}")
    sealed = seal_template(row["id"], row["enrollment_version"], TEMPLATE, env=OLD)
    await admin.execute(
        "UPDATE voice_profiles SET template_ciphertext=$2, template_nonce=$3, "
        "  wrapped_dek=$4, wrap_nonce=$5, kek_id=$6, encryption_key_version=$7, "
        "  encrypted_at=NOW(), migration_status='ENCRYPTED' WHERE id=$1",
        row["id"], sealed["template_ciphertext"], sealed["template_nonce"],
        sealed["wrapped_dek"], sealed["wrap_nonce"], sealed["kek_id"],
        sealed["encryption_key_version"])
    return dict(row)


async def test_a_template_stops_opening_when_the_kek_moves_without_a_rewrap(admin):
    """The failure this batch exists to prevent, demonstrated before the fix is
    applied. Without it, the re-wrap below proves nothing — a template that
    opened under the new KEK anyway would mean the KEK was not load-bearing."""
    from security.voice_crypto import VoiceKeyUnavailable, use_template

    row = await _enrolled(admin)
    full = dict(await admin.fetchrow(
        "SELECT * FROM voice_profiles WHERE id=$1", row["id"]))

    with pytest.raises(VoiceKeyUnavailable):
        await use_template(full, json.loads, env=NEW)


async def test_a_rewrapped_template_opens_under_the_new_kek(admin):
    from security.voice_crypto import rewrap_tenant_voice_keys, use_template

    row = await _enrolled(admin)
    principal = await _principal(admin)

    count, failures = await rewrap_tenant_voice_keys(
        principal, old_env=OLD, new_env=NEW)
    assert failures == []
    assert count == 1

    full = dict(await admin.fetchrow(
        "SELECT * FROM voice_profiles WHERE id=$1", row["id"]))
    assert full["kek_id"] == NEW["IRA_CONNECTOR_ENCRYPTION_KEY_ID"]
    assert await use_template(full, json.loads, env=NEW) == json.loads(TEMPLATE)


async def test_the_rewrap_does_not_change_the_enrolment_version(admin):
    """The AAD binds the version. A row that gained one here would strand the
    template sealed under the old one — a re-wrap is not a re-enrolment."""
    from security.voice_crypto import rewrap_tenant_voice_keys

    row = await _enrolled(admin)
    before = row["enrollment_version"]
    await rewrap_tenant_voice_keys(await _principal(admin),
                                   old_env=OLD, new_env=NEW)
    after = await admin.fetchval(
        "SELECT enrollment_version FROM voice_profiles WHERE id=$1", row["id"])
    assert after == before


async def test_the_ciphertext_is_untouched_by_a_rewrap(admin):
    """Re-wrapping the key must not re-seal the template — that would mean
    holding every plaintext voiceprint in memory during routine maintenance."""
    from security.voice_crypto import rewrap_tenant_voice_keys

    row = await _enrolled(admin)
    before = await admin.fetchval(
        "SELECT template_ciphertext FROM voice_profiles WHERE id=$1", row["id"])
    await rewrap_tenant_voice_keys(await _principal(admin),
                                   old_env=OLD, new_env=NEW)
    after = await admin.fetchval(
        "SELECT template_ciphertext FROM voice_profiles WHERE id=$1", row["id"])
    assert bytes(after) == bytes(before)


async def test_a_shredded_enrolment_is_skipped_rather_than_failing(admin):
    """Its key is destroyed deliberately. Counting it as a failure would make
    every rotation after a crypto-shred report a problem that is the intended
    state."""
    from security.voice_crypto import rewrap_tenant_voice_keys

    keep = await _enrolled(admin, "-keep")
    shred = await _enrolled(admin, "-shred")
    await admin.execute(
        "UPDATE voice_profiles SET wrapped_dek=NULL, wrap_nonce=NULL, "
        "  deletion_status='crypto_shredded' WHERE id=$1", shred["id"])

    count, failures = await rewrap_tenant_voice_keys(
        await _principal(admin), old_env=OLD, new_env=NEW)
    assert failures == []
    assert count == 1, "the shredded enrolment should not have been re-wrapped"
    assert keep["id"] is not None


async def test_one_unopenable_enrolment_does_not_abandon_the_rest(admin):
    """Failures are collected, not raised. Stopping would leave the remaining
    enrolments wrapped under a KEK that is about to be retired — turning one
    broken row into all of them."""
    from security.voice_crypto import rewrap_tenant_voice_keys

    # The BAD row is created FIRST, and that ordering is the test.
    #
    # A planted `break` after the first failure walked past the earlier version,
    # which created the good row first: the good row was re-wrapped, the bad one
    # failed, and the counts matched whether or not the loop continued. The
    # assertion could only catch the defect if the bad row happened to come
    # first, which made it a coin flip on row order — the same shape as a
    # single-byte leak check.
    #
    # `rewrap_tenant_voice_keys` now orders deterministically, so creating the
    # bad row first means a `break` leaves the good one wrapped under the old
    # KEK and the count drops to zero.
    bad = await _enrolled(admin, "-bad")
    good = await _enrolled(admin, "-good")
    await admin.execute(
        "UPDATE voice_profiles SET wrapped_dek=$2 WHERE id=$1",
        bad["id"], b"\x00" * 60)

    count, failures = await rewrap_tenant_voice_keys(
        await _principal(admin), old_env=OLD, new_env=NEW)
    assert count == 1, (
        "the rotation stopped at the first unopenable enrolment, leaving the "
        "rest wrapped under a KEK that is about to be retired — one broken row "
        "turned into all of them")
    assert failures == [str(bad["id"])]

    after = await admin.fetchval(
        "SELECT kek_id FROM voice_profiles WHERE id=$1", good["id"])
    assert after == NEW["IRA_CONNECTOR_ENCRYPTION_KEY_ID"]


async def test_the_failure_list_carries_no_template_and_no_key(admin):
    from security.voice_crypto import rewrap_tenant_voice_keys

    bad = await _enrolled(admin, "-bad")
    await admin.execute(
        "UPDATE voice_profiles SET wrapped_dek=$2 WHERE id=$1",
        bad["id"], b"\x00" * 60)

    _, failures = await rewrap_tenant_voice_keys(
        await _principal(admin), old_env=OLD, new_env=NEW)
    blob = " ".join(failures)
    assert "0.31" not in blob and "0.42" not in blob
    assert failures == [str(bad["id"])], "only identifiers belong in this list"


async def _principal(admin):
    """A principal whose bound connection can see the fixture rows.

    `voice_profiles` is USER_PRIVATE, but these fixtures carry no `user_id` —
    they are the private-edition owner path. The rewrap runs through
    `tenant_connection`, so it needs a principal; the rows it must reach are the
    unowned ones, which an admin-seeded fixture and a superuser DSN make
    visible.
    """
    from core.identity.principal import Principal

    return Principal(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(),
                     session_id=uuid.uuid4(), edition="cloud",
                     username="", is_owner=False)
