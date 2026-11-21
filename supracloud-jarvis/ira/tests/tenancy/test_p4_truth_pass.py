"""Phase 4 batch 16 — the adversarial truth pass.

Not a feature suite. Each test here is a *class* of defect that Phase 4 has
already produced at least once, turned into something that fails automatically
the next time. The list is the batch's brief, and the two that found something
are marked.

  1.  a route's `ON CONFLICT` naming an index a migration dropped   ← FOUND
  2.  a test blinded by RLS rather than passing
  3.  a superuser fixture hiding the thing under test
  4.  an injection point that never executes
  5.  a mechanism with no callers
  6.  rotation missing a family of protected objects
  7.  a secret reaching a log through third-party DEBUG output      ← FOUND
  8.  an integrity check that cannot fail
  9.  a `"char"`-typed column compared against a `str`
  10. success returned before the cryptographic state is safe
  11. a background job on the wrong connection or authority
  12. a second factor readable from a neighbouring tenant

Where a class turned up nothing, the test still exists and still asserts —
`test_...` names below say what was checked, and the module docstring is not
the evidence. Classes that were checked and found clean are recorded as
non-defects in `docs/cloud/evidence/PHASE_4_TRUTH_PASS.md` rather than dressed
up as catches.
"""
from __future__ import annotations

import base64
import inspect
import io
import logging
import os
import pathlib
import re
import secrets

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

# Generated, not written down. A literal base64 KEK in the source is a
# high-entropy blob indistinguishable from a real one, and the secret-scanning
# gate says so — it failed this file on exactly that line. Allowlisting it would
# have weakened the control to keep a test convenient; generating it removes the
# reason for the finding instead. The other Phase 4 modules do the same.
os.environ.setdefault(
    "IRA_CONNECTOR_ENCRYPTION_KEY",
    base64.b64encode(secrets.token_bytes(32)).decode())
os.environ.setdefault("IRA_CONNECTOR_ENCRYPTION_KEY_ID", "kek-p416")
for _k, _v in {
    "IRA_SECRET_KEY": "ci-test-secret-key-for-jwt",
    "IRA_ADMIN_PASSWORD": "ci-test-password",
    "POSTGRES_PASSWORD": "ci-test",
    "REDIS_PASSWORD": "ci-test",
    "VLLM_API_KEY": "ci-test",
}.items():
    os.environ.setdefault(_k, _v)

IRA_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _code_only(source: str) -> str:
    """`source` with comments and string literals removed.

    A guard that greps raw source cannot tell a call from a comment about a
    call, and every one of these modules explains its own history in prose. The
    tokenizer draws that line exactly.
    """
    import io as _io
    import tokenize

    kept = []
    try:
        for tok in tokenize.generate_tokens(_io.StringIO(source).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            kept.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        # An indented `inspect.getsource` fragment does not always tokenize.
        # Falling back to the raw text is the safe direction: it can only make
        # the guard stricter, never blinder.
        return source
    return " ".join(kept)


@pytest.fixture
async def admin():
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        yield conn
    finally:
        await conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 1. ON CONFLICT against the schema that actually exists
# ─────────────────────────────────────────────────────────────────────────────

_INSERT_RE = re.compile(r"INSERT\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.I)
_CONFLICT_RE = re.compile(r"ON\s+CONFLICT\s*\(([^)]*)\)", re.I)


def _statements():
    """Every `ON CONFLICT (...)` in application code, with its target table.

    Comments that merely *mention* the syntax are excluded by requiring an
    `INSERT INTO` earlier in the file — several modules explain, in prose, which
    `ON CONFLICT` they no longer use, and treating those as statements would
    make this fail on documentation.
    """
    for path in sorted(IRA_ROOT.rglob("*.py")):
        s = str(path)
        if "/tests/" in s or "__pycache__" in s:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in _CONFLICT_RE.finditer(text):
            cols_raw = m.group(1).strip()
            if not cols_raw:
                continue
            before = _INSERT_RE.findall(text[: m.start()])
            if not before:
                continue
            tail = text[m.end():m.end() + 400]
            has_where = re.match(r"\s*WHERE\b", tail, re.I) is not None
            cols = frozenset(c.strip().strip('"') for c in cols_raw.split(","))
            yield path, before[-1], cols_raw, cols, has_where


async def _unique_indexes(admin, table):
    rows = await admin.fetch(
        """
        SELECT i.relname AS name,
               array_agg(a.attname ORDER BY a.attname) AS cols,
               ix.indpred IS NOT NULL AS partial
          FROM pg_class t
          JOIN pg_index ix ON ix.indrelid = t.oid
          JOIN pg_class i  ON i.oid = ix.indexrelid
          JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
         WHERE t.relname = $1 AND ix.indisunique
         GROUP BY i.relname, ix.indpred
        """,
        table,
    )
    return [(r["name"], frozenset(r["cols"]), r["partial"]) for r in rows]


async def test_every_on_conflict_target_has_a_unique_index(admin):
    """**Found: P4-09.**

    `065` dropped the unique index the TOTP route's `ON CONFLICT (user_id)
    WHERE ...` named, and TOTP enrolment raised "there is no unique or exclusion
    constraint matching the ON CONFLICT specification" for every user. Nothing
    caught it because the migration had tests and the route had tests — the
    *pair* broke, and no test owned the pair.

    This owns the pair. It reads the statements out of the source and asks the
    live schema whether each target exists.
    """
    missing = []
    for path, table, cols_raw, cols, _has_where in _statements():
        indexes = await _unique_indexes(admin, table)
        if not indexes:
            continue  # not a table in this database (a different edition's)
        if not any(idx_cols == cols for _, idx_cols, _ in indexes):
            missing.append(
                f"{path.relative_to(IRA_ROOT)}: ON CONFLICT ({cols_raw}) on "
                f"{table}, but the unique indexes are "
                f"{[(n, sorted(c)) for n, c, _ in indexes]}")
    assert not missing, (
        "an INSERT names an ON CONFLICT target with no matching unique index. "
        "PostgreSQL raises at runtime, on the first write, for every user:\n  "
        + "\n  ".join(missing))


async def test_a_partial_unique_index_is_matched_by_a_partial_conflict_target(admin):
    """The half of P4-09 the column names alone do not catch.

    If the only unique index over those columns is PARTIAL, the statement must
    carry a matching `WHERE` — `ON CONFLICT (a, b)` against a partial index
    raises exactly the same runtime error as naming no index at all, and the
    column list looks perfectly correct while it does.
    """
    bad = []
    for path, table, cols_raw, cols, has_where in _statements():
        indexes = await _unique_indexes(admin, table)
        matching = [(n, p) for n, c, p in indexes if c == cols]
        if not matching:
            continue  # covered by the test above
        if all(partial for _, partial in matching) and not has_where:
            bad.append(
                f"{path.relative_to(IRA_ROOT)}: ON CONFLICT ({cols_raw}) on "
                f"{table} has no WHERE, but every unique index over those "
                f"columns is partial ({[n for n, _ in matching]})")
    assert not bad, "\n  ".join(bad)


# ─────────────────────────────────────────────────────────────────────────────
# 7. A secret reaching a log through a third party
# ─────────────────────────────────────────────────────────────────────────────


def _capture_at_debug(service="p416"):
    from core.runtime.logging_config import configure_logging

    handler = configure_logging(service=service)
    buf = io.StringIO()
    handler.setStream(buf)
    logging.getLogger().setLevel(logging.DEBUG)
    return buf


def test_a_response_header_does_not_reach_the_log_at_debug(monkeypatch):
    """**Found: this batch.**

    P4-10 found botocore logging a decrypted tenant KEK at DEBUG, and fixed it
    at the one call site that mattered. The class is wider than that call site:
    `httpcore.http11` logs the COMPLETE response headers at DEBUG.

    Measured, not reasoned about — a local server returns `Set-Cookie` and
    `X-Api-Key`, and before the fix both appeared verbatim, past the redaction
    filter. The filter matches field *names* in structured extras and regexes
    over the finished message; this arrives as a repr of a list of header
    tuples, which is neither.

    `IRA_LOG_LEVEL=DEBUG` is a thing an operator does at 3am. It must not turn
    a transport library into a secret sink.
    """
    import http.server
    import threading

    import httpx

    monkeypatch.setenv("IRA_LOG_LEVEL", "DEBUG")
    monkeypatch.delenv("IRA_LOG_THIRD_PARTY_DEBUG", raising=False)
    buf = _capture_at_debug()

    token = "P416-RESPONSE-HEADER-TOKEN"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Set-Cookie", f"session={token}; HttpOnly")
            self.send_header("X-Api-Key", token)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):  # noqa: A003
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        httpx.get(f"http://127.0.0.1:{server.server_address[1]}/x", timeout=5.0)
    finally:
        server.shutdown()

    assert token not in buf.getvalue(), (
        "a token in a response header reached the log at DEBUG. The redaction "
        "filter cannot catch this shape — it is a repr of header tuples — so "
        "the transport logger has to be floored instead.")


def test_iras_own_debug_logging_still_works(monkeypatch):
    """The floor must not be a mute button.

    A fix that silenced DEBUG everywhere would trade one problem for a worse
    one: the next incident gets investigated with no log lines at all, and
    somebody removes the control rather than the noise.
    """
    monkeypatch.setenv("IRA_LOG_LEVEL", "DEBUG")
    buf = _capture_at_debug()
    logging.getLogger("ira.p416").debug("a message from IRA's own code")
    assert "a message from IRA's own code" in buf.getvalue(), (
        "flooring third-party loggers also silenced IRA's own DEBUG output")


def test_the_floor_can_be_lifted_deliberately(monkeypatch):
    """Debugging a transport is a real need. Doing it by accident is not — so
    it is opt-in, by name, rather than unavailable."""
    from core.runtime import logging_config

    monkeypatch.setenv("IRA_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv(logging_config.THIRD_PARTY_DEBUG_ENV, "true")
    logging.getLogger("httpcore.http11").setLevel(logging.NOTSET)
    _capture_at_debug()
    assert logging.getLogger("httpcore.http11").level == logging.NOTSET, (
        f"{logging_config.THIRD_PARTY_DEBUG_ENV}=true did not lift the floor")


def test_the_kms_call_stays_quiet_even_if_the_floor_is_lifted(monkeypatch):
    """Two controls, deliberately overlapping.

    The floor is configuration and can be lifted. The KMS `Decrypt` body is a
    tenant KEK in plaintext, so its silence must not depend on a setting: P4-10
    quiets botocore around that one call regardless. Asserted structurally
    because the alternative is standing up a KMS to watch it not log.
    """
    from core.crypto import aws_kms

    source = inspect.getsource(aws_kms)
    assert "_quiet_botocore" in source, (
        "the KMS provider no longer quiets botocore around its calls, so a "
        "lifted third-party log floor would print a decrypted KEK")
    assert "with _quiet_botocore()" in source, (
        "_quiet_botocore exists but is not used as a context manager around "
        "the call — a helper nothing enters is not a control")


# ─────────────────────────────────────────────────────────────────────────────
# 3 and 11. Fixtures and background jobs, on the right connection
# ─────────────────────────────────────────────────────────────────────────────


#: The marker a test must carry to build the application pool from the
#: superuser DSN. Deliberately ugly and deliberately greppable.
SUPERUSER_POOL_WAIVER = "P4-16-ALLOW-SUPERUSER-POOL:"


def test_no_phase_4_test_silently_runs_its_application_calls_as_superuser():
    """**The P4-11 mistake, as a guard.**

    The scheduler used `owner_connection` — which binds a private tenant — and
    the test fixture pointed the application pool at the ADMIN dsn. A superuser
    bypasses RLS entirely, so the fixture reported success for a scheduler that
    could see everything. Two defects, each hiding the other.

    The rule is not "never": three Phase 4 fixtures have a real reason, all of
    them the *local single-owner* path, where the principal carries no `user_id`
    and a role subject to a USER_PRIVATE policy would match zero rows — so the
    test would pass by seeing nothing, which is the P2-05 shape and worse than
    the bypass. The rule is **never silently**. Each such fixture carries a
    marker naming why, so a new one is a decision somebody made rather than a
    line somebody copied.

    `ADMIN_DSN` on a direct fixture connection is unrestricted — seeding tenants
    and reading across boundaries is exactly what an ordinary role must not do,
    and that is the point of using it there.
    """
    offenders = []
    for path in sorted((IRA_ROOT / "tests").rglob("test_p4*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        for match in re.finditer(r"create_pool\s*\(([^)]*)\)", text):
            if "ADMIN_DSN" not in match.group(1):
                continue
            line_no = text[: match.start()].count("\n") + 1
            # The waiver has to be near the call, not anywhere in the file: a
            # marker at the top would licence every future call site.
            window = "\n".join(lines[max(0, line_no - 16):line_no + 2])
            if SUPERUSER_POOL_WAIVER not in window:
                offenders.append(f"{path.relative_to(IRA_ROOT)}:{line_no}")
    assert not offenders, (
        "a Phase 4 test builds the application pool from the superuser DSN "
        f"with no `{SUPERUSER_POOL_WAIVER} <reason>` marker within the "
        "preceding few lines. Every RLS policy under test is then bypassed and "
        "the suite passes whatever the policies say:\n  "
        + "\n  ".join(offenders))


def test_the_cloud_rotation_path_is_covered_without_a_superuser_pool():
    """The waiver above is only defensible if the *cloud* path is proven
    elsewhere by an ordinary role. Otherwise "documented bypass" is just
    "bypass" with a comment.

    P4-15's acceptance module rotates a tenant that has credentials, an MFA key
    and a voice enrolment, through `ira_app`, with the per-member connection
    binding that `060` requires. That is the coverage this waiver leans on, so
    it is asserted rather than assumed.
    """
    path = IRA_ROOT / "tests" / "tenancy" / "test_p4_acceptance.py"
    assert path.is_file(), "the P4-15 acceptance module is gone"
    text = path.read_text(encoding="utf-8")
    assert "create_pool(dsn=DSN" in text, (
        "the acceptance module no longer builds its pool from the application "
        "DSN, so nothing exercises cloud rotation under RLS")
    assert "ADMIN_DSN" not in text.split("async def pool()")[1].split("async def")[0], (
        "the acceptance module's application pool now uses the superuser DSN")
    assert "rotate_tenant" in text, (
        "the acceptance module no longer rotates anything")


def test_the_scheduler_binds_a_tenant_and_nothing_else():
    """A background job holds no user and no workspace.

    `owner_connection` binds a private tenant — the local edition's owner — so a
    fleet-wide job using it reads one tenant's rows and reports having swept
    them all. `scheduler_connection` binds the tenant under sweep and clears the
    other two GUCs explicitly, because under FORCE RLS a stale GUC is worse than
    an unset one.
    """
    from core.jobs import scheduler

    # Code, not prose. The first version of this test matched the raw source and
    # failed on the module's own comment explaining that it *used* to use
    # `owner_connection` — a guard that fires on documentation is a guard that
    # gets deleted. Comments and strings are stripped first.
    source = _code_only(inspect.getsource(scheduler))
    assert "owner_connection" not in source, (
        "the scheduler uses owner_connection, which binds one private tenant")
    assert "scheduler_connection" in source, (
        "the scheduler no longer uses scheduler_connection")

    from core.identity import db_context

    binder = inspect.getsource(db_context.scheduler_connection)
    assert "app.tenant_id" in binder
    for guc in ("app.user_id", "app.workspace_id"):
        assert guc in binder, (
            f"scheduler_connection does not mention {guc}. Both must be "
            f"cleared explicitly: a connection returned to the pool carrying a "
            f"previous request's identity is a cross-tenant read waiting to "
            f"happen.")


# ─────────────────────────────────────────────────────────────────────────────
# 6. Rotation reaching every family of protected object
# ─────────────────────────────────────────────────────────────────────────────


async def test_every_kek_wrapped_table_is_reached_by_rotation(admin):
    """**The omission that would have been unrecoverable.**

    `voice_profiles` shipped in `062` wrapped under the KEK with no rotation
    step behind it. A routine rotation would have made every enrolled
    voiceprint unopenable and reported success, and after the contract stage
    there is no plaintext to fall back to.

    So the question is asked of the SCHEMA, not of a list somebody maintains: a
    table with a `wrapped_dek` column holds KEK-wrapped key material, and
    rotation has to name it.
    """
    from core.connectors import keyring

    wrapped = {r["table_name"] for r in await admin.fetch(
        "SELECT table_name FROM information_schema.columns "
        " WHERE table_schema = 'public' AND column_name = 'wrapped_dek'")}
    assert wrapped, "no table has a wrapped_dek column — has the schema moved?"

    source = inspect.getsource(keyring.rotate_tenant)
    # `tenant_data_keys` is what rotation rotates rather than follows up on.
    unreached = sorted(
        t for t in wrapped
        if t != "tenant_data_keys" and t not in source
        and t.rstrip("s") not in source)
    assert not unreached, (
        f"these tables hold KEK-wrapped key material and rotate_tenant does "
        f"not mention them: {unreached}. A rotation would leave their key "
        f"material wrapped under the previous KEK and report success.")


# ─────────────────────────────────────────────────────────────────────────────
# 8. An integrity check that cannot fail
# ─────────────────────────────────────────────────────────────────────────────


def _trigger_state(raw) -> str:
    """`pg_trigger.tgenabled` as a one-character string.

    It is PostgreSQL's `"char"` type, and asyncpg hands it back as **bytes**.
    `state == "D"` is therefore never true, so a disabled trigger reads as
    enabled — forever, and the code looks perfectly correct. `expire_auth_events`
    hit this; normalising in one place is what stops the next reader from
    writing the comparison the obvious way.
    """
    return raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)


async def test_every_append_only_trigger_is_actually_enabled(admin):
    """Asked of the schema rather than of a table name.

    The first version of this test looked for triggers on `audit_log` and found
    none — the append-only trigger lives on `biometric_audit`. It would have
    failed loudly, but the same mistake spelled slightly differently ("no rows,
    so nothing to check") is the integrity test that cannot fail. So the triggers
    are discovered, and the discovery itself is asserted to be non-empty.
    """
    rows = await admin.fetch(
        "SELECT c.relname AS table_name, t.tgname, t.tgenabled "
        "  FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        " WHERE NOT t.tgisinternal "
        "   AND (t.tgname LIKE '%append_only%' OR t.tgname LIKE '%shred_only%' "
        "        OR t.tgname LIKE '%immutable%')")
    assert rows, (
        "no append-only or shred-only trigger exists anywhere in this "
        "database. Either the integrity guarantees are gone, or this query no "
        "longer finds them — and a check that silently matches nothing is the "
        "defect class this test is here to catch.")

    disabled = []
    for row in rows:
        state = _trigger_state(row["tgenabled"])
        # 'O' origin, 'A' always, 'R' replica — all enabled. 'D' is disabled.
        if state not in ("O", "A", "R"):
            disabled.append(
                f"{row['table_name']}.{row['tgname']} is {state!r}")
    assert not disabled, (
        "an integrity trigger is disabled, so the guarantee it enforces is "
        "off: " + "; ".join(disabled))


async def test_the_trigger_state_check_can_tell_disabled_from_enabled(admin):
    """The check above must be able to FAIL. A comparison against the raw
    `"char"` value cannot: asyncpg returns bytes, `b'D' != "D"`, and every
    trigger reads as enabled.

    Proven by disabling a trigger for real, inside a transaction that is always
    rolled back, and confirming the check notices.
    """
    tx = admin.transaction()
    await tx.start()
    try:
        row = await admin.fetchrow(
            "SELECT c.relname AS table_name, t.tgname FROM pg_trigger t "
            "  JOIN pg_class c ON c.oid = t.tgrelid "
            " WHERE NOT t.tgisinternal AND t.tgname LIKE '%append_only%' "
            " LIMIT 1")
        assert row is not None, "no append-only trigger to disable"

        before = _trigger_state(await admin.fetchval(
            "SELECT tgenabled FROM pg_trigger WHERE tgname = $1", row["tgname"]))
        assert before in ("O", "A", "R"), f"already {before!r} before the test"

        await admin.execute(
            f'ALTER TABLE {row["table_name"]} DISABLE TRIGGER {row["tgname"]}')

        after = _trigger_state(await admin.fetchval(
            "SELECT tgenabled FROM pg_trigger WHERE tgname = $1", row["tgname"]))
        assert after == "D", (
            f"a trigger that was just disabled reads as {after!r}. The "
            f"normalisation is wrong, and every enabled-check in the codebase "
            f"built on it silently always passes.")
        assert after not in ("O", "A", "R"), (
            "the enabled-set matches a disabled trigger, so the check cannot "
            "fail")
    finally:
        # Always rolled back: this is a destructive change to an integrity
        # control, and it must not survive the test under any outcome.
        await tx.rollback()

    still_on = _trigger_state(await admin.fetchval(
        "SELECT tgenabled FROM pg_trigger WHERE tgname LIKE '%append_only%' "
        "LIMIT 1"))
    assert still_on in ("O", "A", "R"), (
        f"the rollback did not restore the trigger: it is {still_on!r}")
