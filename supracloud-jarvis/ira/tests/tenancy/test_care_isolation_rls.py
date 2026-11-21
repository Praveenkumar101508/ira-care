"""IRA CARE (099): adversarial isolation of the fifteen care tables, against a
real PostgreSQL as the runtime role.

Two families (tenants), each with a default workspace and a person. Family A
writes a full set of rows about its person; family B, a second workspace of
family A, and an unbound connection each try to read, change and delete them.

Also: the access log is append-only even for its own writer, and the
per-person erasure removes everything about the person except the access log.

Running them: see the header of test_tenant_isolation_rls.py. The chain must
include 099_care_foundation.sql.
"""
from __future__ import annotations

import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 not installed")
from psycopg2 import errors as pgerr  # noqa: E402

from tests.tenancy import rls_workspace  # noqa: E402

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN or not ADMIN_DSN,
    reason="IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set — real-database RLS tests skipped",
)

CARE_TABLES = (
    "care_people", "care_circle_members", "care_devices", "care_vitals",
    "care_safety_settings", "care_checkins", "care_alerts", "care_alert_attempts",
    "care_acknowledgements", "care_routines", "care_medications",
    "care_medication_events", "care_transcripts", "care_consents", "care_access_log",
)


def _bind(conn, tenant, workspace, user=None):
    cur = conn.cursor()
    cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))
    cur.execute("SELECT set_config('app.workspace_id', %s, false)", (str(workspace),))
    cur.execute("SELECT set_config('app.user_id', %s, false)", (str(user) if user else "",))
    return cur


def _unbind(conn):
    cur = conn.cursor()
    for k in ("app.tenant_id", "app.workspace_id", "app.user_id"):
        cur.execute(f"SELECT set_config('{k}', '', false)")
    return cur


@pytest.fixture
def families():
    """Two tenants with default workspaces, created as the privileged role;
    content written as ira_app through the policies."""
    admin = psycopg2.connect(ADMIN_DSN)
    admin.autocommit = True
    app = psycopg2.connect(DSN)
    app.autocommit = True
    run = uuid.uuid4().hex[:10]
    fams = {}
    for key in ("a", "b"):
        tenant = uuid.uuid4()
        cur = admin.cursor()
        cur.execute("INSERT INTO tenants (id, name, slug, edition) VALUES (%s, %s, %s, 'cloud')",
                    (str(tenant), f"care-{key}-{run}", f"care-{key}-{run}"))
        acur = _bind(app, tenant, uuid.uuid4())  # workspace is created below
        ws = rls_workspace.ensure_workspace(acur, tenant)
        fams[key] = {"tenant": tenant, "workspace": ws}
    # A second workspace inside family A, to prove the boundary is the
    # workspace and not only the tenant.
    a = fams["a"]
    cur = _bind(app, a["tenant"], a["workspace"])
    cur.execute("SELECT organisation_id FROM workspaces WHERE id = %s", (str(a["workspace"]),))
    org = cur.fetchone()[0]
    ws2 = uuid.uuid4()
    cur.execute(
        "INSERT INTO workspaces (id, tenant_id, organisation_id, name, slug, is_default, status) "
        "VALUES (%s, %s, %s, 'Second', %s, FALSE, 'active')",
        (str(ws2), str(a["tenant"]), org, f"second-{run}"))
    a["workspace2"] = ws2
    _unbind(app)
    yield {"admin": admin, "app": app, **fams}
    # Tear down: everything hangs off the tenants. The access log does not
    # cascade (RESTRICT), so it goes first, as the privileged role.
    cur = admin.cursor()
    for key in ("a", "b"):
        cur.execute("DELETE FROM care_access_log WHERE tenant_id = %s", (str(fams[key]["tenant"]),))
        cur.execute("DELETE FROM tenants WHERE id = %s", (str(fams[key]["tenant"]),))
    admin.close()
    app.close()


def _write_full_set(cur) -> dict:
    """One row in every care table, as family A in its default workspace."""
    cur.execute("INSERT INTO care_people (display_name, preferred_name) VALUES ('Test Person A', 'Tess') RETURNING id")
    person = cur.fetchone()[0]
    cur.execute("INSERT INTO care_safety_settings (person_id) VALUES (%s)", (person,))
    cur.execute("INSERT INTO care_circle_members (person_id, display_name, role, tier) "
                "VALUES (%s, 'Family Member A', 'family', 1) RETURNING id", (person,))
    member = cur.fetchone()[0]
    cur.execute("INSERT INTO care_devices (person_id, status, token_hash) VALUES (%s, 'paired', %s) RETURNING id",
                (person, uuid.uuid4().hex + uuid.uuid4().hex))
    device = cur.fetchone()[0]
    cur.execute("INSERT INTO care_vitals (person_id, device_id, kind, value, recorded_at) "
                "VALUES (%s, %s, 'heart_rate', 72, NOW()) RETURNING id", (person, device))
    vital = cur.fetchone()[0]
    cur.execute("INSERT INTO care_checkins (person_id, scheduled_at, question_kind, status, answer_text) "
                "VALUES (%s, NOW(), 'meal', 'answered', 'Had porridge') RETURNING id", (person,))
    checkin = cur.fetchone()[0]
    cur.execute("INSERT INTO care_alerts (person_id, kind, severity) VALUES (%s, 'help_requested', 'sos') RETURNING id",
                (person,))
    alert = cur.fetchone()[0]
    cur.execute("INSERT INTO care_alert_attempts (alert_id, tier, member_id, channel, outcome) "
                "VALUES (%s, 1, %s, 'push', 'logged') RETURNING id", (alert, member))
    attempt = cur.fetchone()[0]
    cur.execute("INSERT INTO care_acknowledgements (alert_id, member_id, kind) VALUES (%s, %s, 'on_my_way') RETURNING id",
                (alert, member))
    ack = cur.fetchone()[0]
    cur.execute("INSERT INTO care_routines (person_id, name, kind) VALUES (%s, 'Lunch', 'meal') RETURNING id", (person,))
    routine = cur.fetchone()[0]
    cur.execute("INSERT INTO care_medications (person_id, name) VALUES (%s, 'Test tablet') RETURNING id", (person,))
    med = cur.fetchone()[0]
    cur.execute("INSERT INTO care_medication_events (medication_id, person_id, due_at, status) "
                "VALUES (%s, %s, NOW(), 'taken') RETURNING id", (med, person))
    med_event = cur.fetchone()[0]
    cur.execute("INSERT INTO care_transcripts (person_id, checkin_id, speaker, text) "
                "VALUES (%s, %s, 'person', 'I had porridge') RETURNING id", (person, checkin))
    transcript = cur.fetchone()[0]
    cur.execute("INSERT INTO care_consents (person_id, kind, granted, granted_by) "
                "VALUES (%s, 'transcripts_family', TRUE, 'person') RETURNING id", (person,))
    consent = cur.fetchone()[0]
    cur.execute("INSERT INTO care_access_log (person_id, resource_kind) VALUES (%s, 'vitals') RETURNING id", (person,))
    log = cur.fetchone()[0]
    return {
        "care_people": person, "care_circle_members": member, "care_devices": device,
        "care_vitals": vital, "care_safety_settings": person, "care_checkins": checkin,
        "care_alerts": alert, "care_alert_attempts": attempt, "care_acknowledgements": ack,
        "care_routines": routine, "care_medications": med, "care_medication_events": med_event,
        "care_transcripts": transcript, "care_consents": consent, "care_access_log": log,
    }


_PK = {"care_safety_settings": "person_id"}


def _count(cur, table, handle) -> int:
    cur.execute(f"SELECT count(*) FROM {table} WHERE {_PK.get(table, 'id')} = %s", (handle,))
    return cur.fetchone()[0]


@pytest.fixture
def written(families):
    a = families["a"]
    cur = _bind(families["app"], a["tenant"], a["workspace"])
    handles = _write_full_set(cur)
    _unbind(families["app"])
    return handles


def test_the_app_role_is_not_a_superuser(families):
    cur = families["app"].cursor()
    cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
    assert cur.fetchone() == (False, False)


def test_every_care_table_is_forced(families):
    cur = families["admin"].cursor()
    cur.execute("SELECT relname FROM pg_class WHERE relname = ANY(%s) AND relrowsecurity AND relforcerowsecurity",
                (list(CARE_TABLES),))
    assert {r[0] for r in cur.fetchall()} == set(CARE_TABLES)


@pytest.mark.parametrize("table", CARE_TABLES)
def test_the_writer_can_read_back_what_it_wrote(families, written, table):
    """Without this the 'nobody can see it' tests below could pass vacuously."""
    a = families["a"]
    cur = _bind(families["app"], a["tenant"], a["workspace"])
    assert _count(cur, table, written[table]) == 1


@pytest.mark.parametrize("table", CARE_TABLES)
def test_the_other_family_cannot_read_it(families, written, table):
    b = families["b"]
    cur = _bind(families["app"], b["tenant"], b["workspace"])
    assert _count(cur, table, written[table]) == 0


@pytest.mark.parametrize("table", CARE_TABLES)
def test_the_same_familys_other_workspace_cannot_read_it(families, written, table):
    a = families["a"]
    cur = _bind(families["app"], a["tenant"], a["workspace2"])
    assert _count(cur, table, written[table]) == 0


@pytest.mark.parametrize("table", CARE_TABLES)
def test_an_unbound_connection_reads_nothing(families, written, table):
    cur = _unbind(families["app"])
    cur.execute(f"SELECT count(*) FROM {table}")
    assert cur.fetchone()[0] == 0


def test_the_other_family_cannot_change_or_delete_the_person(families, written):
    b = families["b"]
    cur = _bind(families["app"], b["tenant"], b["workspace"])
    cur.execute("UPDATE care_people SET display_name = 'taken over' WHERE id = %s", (written["care_people"],))
    assert cur.rowcount == 0
    cur.execute("DELETE FROM care_people WHERE id = %s", (written["care_people"],))
    assert cur.rowcount == 0
    a = families["a"]
    cur = _bind(families["app"], a["tenant"], a["workspace"])
    cur.execute("SELECT display_name FROM care_people WHERE id = %s", (written["care_people"],))
    assert cur.fetchone()[0] == "Test Person A"


def test_a_family_cannot_insert_into_the_other_familys_workspace(families):
    """Naming another tenant's ids explicitly is refused, not silently remapped."""
    a, b = families["a"], families["b"]
    cur = _bind(families["app"], a["tenant"], a["workspace"])
    with pytest.raises(pgerr.InsufficientPrivilege):
        cur.execute("INSERT INTO care_people (tenant_id, workspace_id, display_name) VALUES (%s, %s, 'intruder')",
                    (str(b["tenant"]), str(b["workspace"])))


def test_an_unbound_connection_cannot_insert(families):
    cur = _unbind(families["app"])
    with pytest.raises((pgerr.NotNullViolation, pgerr.InsufficientPrivilege)):
        cur.execute("INSERT INTO care_people (display_name) VALUES ('nobody')")


def test_the_access_log_is_append_only_even_for_its_writer(families, written):
    a = families["a"]
    cur = _bind(families["app"], a["tenant"], a["workspace"])
    with pytest.raises(pgerr.InsufficientPrivilege):
        cur.execute("UPDATE care_access_log SET action = 'export' WHERE id = %s", (written["care_access_log"],))
    with pytest.raises(pgerr.InsufficientPrivilege):
        cur.execute("DELETE FROM care_access_log WHERE id = %s", (written["care_access_log"],))
    # Nobody rewrites an audit row, the privileged role included: the trigger,
    # not the grant, is what refuses. Only DELETE by the schema owner (the
    # retention purge) is let through.
    acur = families["admin"].cursor()
    with pytest.raises(pgerr.InsufficientPrivilege):
        acur.execute("UPDATE care_access_log SET action = 'export' WHERE id = %s", (written["care_access_log"],))


def test_readings_and_attempts_cannot_be_rewritten(families, written):
    a = families["a"]
    cur = _bind(families["app"], a["tenant"], a["workspace"])
    with pytest.raises(pgerr.InsufficientPrivilege):
        cur.execute("UPDATE care_vitals SET value = 0 WHERE id = %s", (written["care_vitals"],))
    with pytest.raises(pgerr.InsufficientPrivilege):
        cur.execute("UPDATE care_alert_attempts SET outcome = 'delivered' WHERE id = %s", (written["care_alert_attempts"],))
    with pytest.raises(pgerr.InsufficientPrivilege):
        cur.execute("DELETE FROM care_alert_attempts WHERE id = %s", (written["care_alert_attempts"],))


def test_erasing_the_person_removes_everything_but_the_access_log(families, written):
    a = families["a"]
    cur = _bind(families["app"], a["tenant"], a["workspace"])
    cur.execute("DELETE FROM care_people WHERE id = %s", (written["care_people"],))
    assert cur.rowcount == 1
    for table, handle in written.items():
        expected = 1 if table == "care_access_log" else 0
        assert _count(cur, table, handle) == expected, table


def test_no_built_in_heart_rate_range(families, written):
    """The settings row exists from the start, and its range is unset."""
    a = families["a"]
    cur = _bind(families["app"], a["tenant"], a["workspace"])
    cur.execute("SELECT heart_rate_min, heart_rate_max FROM care_safety_settings WHERE person_id = %s",
                (written["care_people"],))
    assert cur.fetchone() == (None, None)


def test_a_paired_device_must_carry_a_token(families, written):
    a = families["a"]
    cur = _bind(families["app"], a["tenant"], a["workspace"])
    with pytest.raises(pgerr.CheckViolation):
        cur.execute("INSERT INTO care_devices (person_id, status) VALUES (%s, 'paired')", (written["care_people"],))
