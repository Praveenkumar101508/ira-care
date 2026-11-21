"""Phase 9 — a backup is only a backup if the restore still protects the data.

The disaster-recovery runbook promises that a dump can be restored and the
service resumed. The interesting question is not whether the rows come back —
`pg_dump` is reliable about rows — but whether the *guarantees* come back with
them. Row-level security, FORCE, the append-only triggers, the generated columns
that make audio retention structurally impossible, and the CHECK constraints
that make a privacy-violating usage row unrepresentable are all schema objects,
and a restore that quietly dropped any of them would produce a database that
looks complete, passes a smoke test, serves traffic, and no longer isolates
tenants.

That failure is not hypothetical. The ways to get it are ordinary:

  * restoring into a database whose owner differs from the dump's, so policies
    referencing the app role do not apply to the role that ends up owning the
    tables;
  * `pg_restore --disable-triggers`, which is standard advice for speed and
    silently deactivates the append-only enforcement;
  * a `--section=data` restore onto a hand-built schema that someone recreated
    from the migration files but stopped before the `FORCE` statements.

So this suite performs a real dump and a real restore into a real fresh
database, reconnects **as the unprivileged application role**, and re-asserts
the structural guarantees on the restored copy. It is the DR runbook's evidence,
and `RELEASE_READINESS.md` item R09 cites it.

── What it does not prove ───────────────────────────────────────────────────

That a restore works at production scale, within an RTO, from an offsite
artefact, on a host that has just been rebuilt. This runs `pg_dump | psql`
against a local instance holding test-sized data. It proves the *guarantees*
survive the round trip. Time-bounded recovery on real volumes is R07, and R07 is
recorded as not verified.
"""
from __future__ import annotations

import os
import subprocess
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

#: The unprivileged application role's DSN — the same one the isolation suites
#: use. Its *database* is what gets dumped.
DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")

#: A role permitted to CREATE DATABASE, needed to build the restore target.
#: Separate from DSN on purpose: the application role must not be able to do
#: this, so the test cannot borrow its credentials.
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")

pytestmark = pytest.mark.skipif(
    not (DSN and ADMIN_DSN),
    reason="IRA_TEST_DATABASE_DSN and IRA_TEST_ADMIN_DSN both required for "
           "backup-restore verification",
)


def _dsn_parts(dsn: str) -> dict[str, str]:
    """psycopg2's own parser, so we agree with the driver about the DSN."""
    from psycopg2.extensions import parse_dsn

    return parse_dsn(dsn)


def _swap_database(dsn: str, database: str) -> str:
    parts = _dsn_parts(dsn)
    parts["dbname"] = database
    return " ".join(f"{k}={v}" for k, v in parts.items())


def _run(cmd: list[str], *, env: dict[str, str], stdin=None, stdout=None) -> None:
    result = subprocess.run(
        cmd, env=env, stdin=stdin, stdout=stdout,
        stderr=subprocess.PIPE, text=True, timeout=300,
    )
    if result.returncode != 0:
        pytest.fail(f"{cmd[0]} failed ({result.returncode}): {result.stderr[-2000:]}")


def _pg_env(dsn: str) -> dict[str, str]:
    """Environment for the libpq command-line tools."""
    parts = _dsn_parts(dsn)
    env = dict(os.environ)
    if "password" in parts:
        env["PGPASSWORD"] = parts["password"]
    return env


@pytest.fixture(scope="module")
def restored(tmp_path_factory):
    """Dump the live test database and restore it into a throwaway one.

    Module-scoped: the dump/restore is the expensive part and every assertion
    below reads the same restored copy.
    """
    source_db = _dsn_parts(DSN)["dbname"]
    target_db = f"ira_restore_{uuid.uuid4().hex[:12]}"
    dump_path = tmp_path_factory.mktemp("backup") / "dump.sql"

    admin = psycopg2.connect(ADMIN_DSN)
    admin.autocommit = True
    admin_cur = admin.cursor()

    try:
        # 1. Back up. Schema AND data: the seeded catalogues (capability rungs,
        #    connector scopes, model providers) are part of what must come back,
        #    and a schema-only dump would not notice losing them.
        admin_parts = _dsn_parts(ADMIN_DSN)
        with open(dump_path, "wb") as fh:
            _run(
                [
                    "pg_dump",
                    "-h", admin_parts.get("host", "localhost"),
                    "-p", str(admin_parts.get("port", "5432")),
                    "-U", admin_parts["user"],
                    "-d", source_db,
                    # Ownership IS dumped (no --no-owner): the restored tables
                    # must end up owned by the same role, or FORCE ROW LEVEL
                    # SECURITY would be asserted against a role that is not the
                    # owner and would pass for the wrong reason.
                    "--quote-all-identifiers",
                ],
                env=_pg_env(ADMIN_DSN),
                stdout=fh,
            )

        # 2. Restore into a fresh database owned by the same role as the source,
        #    so the FORCE assertions below mean what they should.
        # CREATE DATABASE cannot be parameterised; the name is a generated hex
        # suffix, quoted by the server rather than by string formatting here.
        admin_cur.execute("SELECT pg_catalog.quote_ident(%s)", (target_db,))
        quoted = admin_cur.fetchone()[0]
        admin_cur.execute(f"CREATE DATABASE {quoted}")

        target_admin_dsn = _swap_database(ADMIN_DSN, target_db)
        target_parts = _dsn_parts(target_admin_dsn)
        # Extensions must exist before the dump's objects that depend on them;
        # a plain dump emits CREATE EXTENSION, but only a superuser may run it
        # for `vector`, which is why the restore runs as the admin role.
        with open(dump_path, "rb") as fh:
            _run(
                [
                    "psql",
                    "-h", target_parts.get("host", "localhost"),
                    "-p", str(target_parts.get("port", "5432")),
                    "-U", target_parts["user"],
                    "-d", target_db,
                    "-q", "-o", os.devnull,
                ],
                env=_pg_env(ADMIN_DSN),
                stdin=fh,
            )

        # 3. Reconnect as the UNPRIVILEGED role. Checking the restore as the
        #    superuser that performed it would hide exactly the failure this
        #    suite is looking for.
        app_conn = psycopg2.connect(_swap_database(DSN, target_db))
        app_conn.autocommit = True
        try:
            yield app_conn
        finally:
            app_conn.close()
    finally:
        admin_cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (target_db,),
        )
        admin_cur.execute(f'DROP DATABASE IF EXISTS "{target_db}"')
        admin.close()


def _source_conn():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    return conn


def _fetch(conn, sql: str, args: tuple = ()) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(sql, args)
    return cur.fetchall()


_TABLES = (
    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 1"
)


def test_the_restored_database_is_not_the_source(restored):
    """Guards against every assertion below silently testing the original."""
    source_db = _dsn_parts(DSN)["dbname"]
    restored_db = _fetch(restored, "SELECT current_database()")[0][0]
    assert restored_db != source_db, (
        "the fixture handed back a connection to the source database; the "
        "restore assertions would pass without a restore having happened"
    )


def test_the_app_role_is_not_superuser_on_the_restored_copy(restored):
    row = _fetch(restored, "SELECT usesuper FROM pg_user WHERE usename = current_user")
    assert row and row[0][0] is False


def test_every_table_came_back(restored):
    source = _source_conn()
    try:
        expected = {r[0] for r in _fetch(source, _TABLES)}
    finally:
        source.close()
    actual = {r[0] for r in _fetch(restored, _TABLES)}
    assert expected, "the source database has no tables — nothing was verified"
    missing = expected - actual
    assert not missing, f"table(s) lost in the restore: {sorted(missing)}"


def test_row_level_security_survived_the_restore(restored):
    """The headline. RLS and FORCE are per-table schema state, and a restore
    that dropped either would leave a database that isolates nothing."""
    source = _source_conn()
    try:
        expected = {
            name: (rls, forced)
            for name, rls, forced in _fetch(source, _TABLES)
            if rls
        }
    finally:
        source.close()
    assert expected, "no table in the source has RLS enabled — nothing verified"

    actual = {name: (rls, forced) for name, rls, forced in _fetch(restored, _TABLES)}
    broken = []
    for table, (rls, forced) in sorted(expected.items()):
        got = actual.get(table)
        if got is None:
            broken.append(f"{table}: missing from the restore")
        elif got != (rls, forced):
            broken.append(
                f"{table}: source had enabled={rls} forced={forced}, "
                f"restore has enabled={got[0]} forced={got[1]}"
            )
    assert not broken, (
        "row-level security did not survive the restore:\n  " + "\n  ".join(broken)
    )


def test_policies_survived_the_restore(restored):
    source = _source_conn()
    q = "SELECT tablename, policyname FROM pg_policies WHERE schemaname='public'"
    try:
        expected = set(_fetch(source, q))
    finally:
        source.close()
    assert expected, "the source has no policies — nothing verified"
    missing = expected - set(_fetch(restored, q))
    assert not missing, f"policies lost in the restore: {sorted(missing)}"


def test_triggers_survived_the_restore_and_are_enabled(restored):
    """Append-only enforcement is triggers, and `--disable-triggers` is a
    documented restore option that would leave them present but inert."""
    q = (
        "SELECT c.relname, t.tgname, t.tgenabled "
        "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND NOT t.tgisinternal ORDER BY 1, 2"
    )
    source = _source_conn()
    try:
        expected = {(t, g) for t, g, _ in _fetch(source, q)}
    finally:
        source.close()
    assert expected, "the source has no user triggers — nothing verified"

    restored_rows = _fetch(restored, q)
    missing = expected - {(t, g) for t, g, _ in restored_rows}
    assert not missing, f"trigger(s) lost in the restore: {sorted(missing)}"

    # 'D' is disabled; 'O' (origin) is the normal enabled state.
    inert = [f"{t}.{g}" for t, g, enabled in restored_rows if enabled == "D"]
    assert not inert, (
        f"trigger(s) restored but DISABLED: {inert}. An append-only table whose "
        "trigger is disabled accepts UPDATE and DELETE silently."
    )


def test_generated_columns_are_still_generated(restored):
    """`audio_stored`/`transcript_stored` are GENERATED ALWAYS AS (FALSE): the
    reason Phase 6 can say realtime audio is not retained. Restored as plain
    boolean columns they would become writable, and the claim would be false."""
    q = (
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND is_generated='ALWAYS' ORDER BY 1,2"
    )
    source = _source_conn()
    try:
        expected = set(_fetch(source, q))
    finally:
        source.close()
    assert expected, "the source has no generated columns — nothing verified"
    missing = expected - set(_fetch(restored, q))
    assert not missing, (
        f"generated column(s) lost their GENERATED ALWAYS definition in the "
        f"restore: {sorted(missing)} — they are now writable."
    )


def test_check_constraints_survived_the_restore(restored):
    """The Phase 8 constraints that make a privacy-violating row
    unrepresentable, plus every other CHECK in the schema."""
    q = (
        "SELECT rel.relname, con.conname FROM pg_constraint con "
        "JOIN pg_class rel ON rel.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = rel.relnamespace "
        "WHERE n.nspname='public' AND con.contype='c' ORDER BY 1,2"
    )
    source = _source_conn()
    try:
        expected = set(_fetch(source, q))
    finally:
        source.close()
    assert expected, "the source has no CHECK constraints — nothing verified"
    missing = expected - set(_fetch(restored, q))
    assert not missing, f"CHECK constraint(s) lost in the restore: {sorted(missing)}"


def test_seeded_catalogues_came_back_with_rows(restored):
    """Global catalogue tables the service reads at startup. These carry no
    tenant_id, so RLS does not hide them and a count is meaningful."""
    for table in ("realtime_capability_profiles", "connector_scopes", "model_providers"):
        source = _source_conn()
        try:
            expected = _fetch(source, f'SELECT count(*) FROM "{table}"')[0][0]
        finally:
            source.close()
        if not expected:
            continue  # nothing seeded in this database; nothing to compare
        actual = _fetch(restored, f'SELECT count(*) FROM "{table}"')[0][0]
        assert actual == expected, (
            f"{table}: source had {expected} row(s), restore has {actual}"
        )


def test_the_restored_copy_still_denies_an_unset_tenant_context(restored):
    """The end-to-end version: not "is the flag set" but "does it deny"."""
    cur = restored.cursor()
    cur.execute("SELECT set_config('app.tenant_id', '', false)")
    scoped = [
        r[0] for r in _fetch(
            restored,
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind='r' AND EXISTS ("
            "  SELECT 1 FROM information_schema.columns col "
            "  WHERE col.table_schema='public' AND col.table_name=c.relname "
            "    AND col.column_name='tenant_id') ORDER BY 1",
        )
    ]
    assert scoped, "no tenant-scoped tables in the restored copy"
    leaked = []
    for table in scoped:
        cur.execute(f'SELECT count(*) FROM "{table}"')  # noqa: S608 - introspected
        if cur.fetchone()[0] != 0:
            leaked.append(table)
    assert not leaked, (
        f"the restored database returned rows with no tenant context: {leaked}. "
        "The restore produced a database that does not isolate tenants."
    )
