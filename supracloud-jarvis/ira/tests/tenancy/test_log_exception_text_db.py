"""Exception text stays out of the logs with real driver errors from a real PostgreSQL (C-6.b,
verification).

tests/runtime/test_log_exception_text.py proves the rule with exceptions shaped like asyncpg's.
Here the errors are the real ones: PostgreSQL itself refusing a value (its message quotes the
value), asyncpg refusing an argument before it is sent (its message quotes the value too), and a
unique-key violation (its detail quotes the key). Each is logged every way the backend logs, through
the production handler (`core.runtime.logging_config.configure_logging`, both formats), and the
private sentence must never reach the output while the exception's type does. A negative control
shows the same records with the developer switch on (IRA_LOG_EXCEPTION_TEXT=1, ignored in a
production-like environment) do carry the text, so the check can see it.
"""
from __future__ import annotations

import io
import logging
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")
DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from core.runtime import logging_config  # noqa: E402
from core.runtime.redaction import EXCEPTION_TEXT_ENV  # noqa: E402

PRIVATE = "Mum took the blue tablet at 9 and lives at 4 Elm Road"


async def real_errors() -> list[BaseException]:
    """Three real errors whose messages quote the private sentence."""
    out: list[BaseException] = []
    conn = await asyncpg.connect(ADMIN_DSN)
    table = f"log_text_{uuid.uuid4().hex[:8]}"
    try:
        try:   # the server refuses it: invalid input syntax for type integer: "Mum took ..."
            await conn.fetchval("SELECT $1::text::int", PRIVATE)
        except asyncpg.PostgresError as e:
            out.append(e)
        try:   # asyncpg refuses it before sending: invalid input for query argument $1: 'Mum took ...'
            await conn.fetchval("SELECT $1::int", PRIVATE)
        except (asyncpg.DataError, asyncpg.PostgresError) as e:
            out.append(e)
        await conn.execute(f"CREATE TEMP TABLE {table} (note text PRIMARY KEY)")
        await conn.execute(f"INSERT INTO {table} VALUES ($1)", PRIVATE)
        try:   # duplicate key value violates unique constraint; DETAIL: Key (note)=(Mum took ...) already exists
            await conn.execute(f"INSERT INTO {table} VALUES ($1)", PRIVATE)
        except asyncpg.UniqueViolationError as e:
            out.append(e)
    finally:
        await conn.close()
    for e in out:
        assert "Mum took" in (str(e) + str(getattr(e, "detail", "") or "")), f"the driver's own message should quote it: {e!r}"
    return out


def log_every_way(errors: list[BaseException], fmt: str) -> str:
    """Log each error as the backend's code paths do, through the production handler."""
    os.environ["IRA_LOG_FORMAT"] = fmt
    stream = io.StringIO()
    handler = logging_config.configure_logging(service="ira-care-test")
    handler.setStream(stream)
    log = logging.getLogger("ira.care.test_exception_text")
    log.setLevel(logging.DEBUG)
    try:
        for e in errors:
            try:
                raise e
            except BaseException:  # noqa: BLE001
                log.exception("care: the write failed")
            log.error("care: the write failed: %s", e)
            log.warning("care: the write failed: %r", e)
            try:
                try:
                    raise e
                except BaseException as inner:  # noqa: BLE001
                    raise RuntimeError("could not record the check-in") from inner
            except RuntimeError:
                log.exception("care: chained")
            log.error("care: the write failed", exc_info=e)
    finally:
        logging.getLogger().removeHandler(handler)
    return stream.getvalue()


@pytest.fixture
def production_logging(monkeypatch):
    monkeypatch.delenv(EXCEPTION_TEXT_ENV, raising=False)
    saved = list(logging.getLogger().handlers), logging.getLogger().level
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved[0]:
        root.addHandler(h)
    root.setLevel(saved[1])
    os.environ.pop("IRA_LOG_FORMAT", None)


@pytest.mark.parametrize("fmt", ["json", "text"])
async def test_real_driver_errors_reach_the_log_without_their_text(production_logging, fmt):
    errors = await real_errors()
    out = log_every_way(errors, fmt)
    assert out.count("\n") >= 5 * len(errors), out[:500]
    assert PRIVATE not in out
    assert "Mum took" not in out and "Elm Road" not in out
    for e in errors:   # the type is kept, so an operator still knows what failed
        assert type(e).__name__ in out, (type(e).__name__, out[:800])


async def test_negative_control_the_developer_switch_shows_the_text(production_logging, monkeypatch):
    monkeypatch.setenv(EXCEPTION_TEXT_ENV, "1")
    monkeypatch.setenv("IRA_ENVIRONMENT", "development")
    from core.runtime.redaction import exception_text_allowed
    if not exception_text_allowed():
        pytest.skip("this environment ignores the developer switch (production-like settings)")
    out = log_every_way(await real_errors(), "text")
    assert "Mum took" in out
