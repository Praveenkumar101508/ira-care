"""Exception messages stay out of the logs (remediation C-6).

A driver's error message quotes the value it refused. asyncpg's DataError names the query
argument; a model client echoes the request; an SMTP library the recipient. In a care
product that value is a person's words, medication or address. The log keeps the exception's
type (and, for a traceback, the frames), never its text, however the exception arrives:
as a `%s` argument, as `exc_info`, chained under another exception, or in an f-string.
"""
from __future__ import annotations

import ast
import io
import json
import logging
import re
from pathlib import Path

import pytest

from core.notify.mail import _mask_address
from core.runtime.logging_config import JsonFormatter, TextFormatter
from core.runtime.redaction import EXCEPTION_TEXT_ENV, RedactionFilter

PRIVATE = "Mum took the blue tablet at 9 and lives at 4 Elm Road"
ROOT = Path(__file__).resolve().parents[2]


class DataError(Exception):
    """Shaped like asyncpg's: the rejected value is in the message."""


def _capture(formatter, emit) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    handler.addFilter(RedactionFilter())
    lg = logging.getLogger("ira.test.exctext")
    lg.handlers.clear()
    lg.addHandler(handler)
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    emit(lg)
    handler.flush()
    return stream.getvalue()


def _fail():
    raise DataError(f"invalid input for query argument $3: '{PRIVATE}'")


@pytest.fixture(autouse=True)
def _no_opt_in(monkeypatch):
    monkeypatch.delenv(EXCEPTION_TEXT_ENV, raising=False)


@pytest.mark.parametrize("formatter", [JsonFormatter(service="t"), TextFormatter()], ids=["json", "text"])
def test_an_exception_passed_as_an_argument_logs_its_type_only(formatter):
    def emit(lg):
        try:
            _fail()
        except DataError as exc:
            lg.warning("memory write failed: %s", exc)
            lg.warning("memory write failed: %r", exc)
            lg.warning("memory write failed: %(e)s", {"e": exc})
    out = _capture(formatter, emit)
    assert PRIVATE not in out and "Elm Road" not in out
    assert out.count("DataError") == 3


def test_the_json_line_carries_the_exception_type_not_its_message():
    def emit(lg):
        try:
            _fail()
        except DataError:
            lg.error("failed", exc_info=True)
    line = json.loads(_capture(JsonFormatter(service="t"), emit))
    assert line["exc_type"] == "DataError"
    assert "exc_message" not in line
    assert PRIVATE not in json.dumps(line)


def test_a_text_traceback_keeps_the_frames_and_withholds_every_message_in_the_chain():
    def emit(lg):
        try:
            try:
                _fail()
            except DataError as inner:
                raise RuntimeError(f"while saving: {PRIVATE}") from inner
        except RuntimeError:
            lg.exception("could not save")
    out = _capture(TextFormatter(), emit)
    assert PRIVATE not in out and "Elm Road" not in out
    assert "DataError (message withheld)" in out and "RuntimeError (message withheld)" in out
    assert out.index("DataError") < out.index("RuntimeError (message withheld)"), "the cause comes first"
    assert "_fail" in out and "test_log_exception_text.py" in out, "the frames are what a developer needs"


def test_a_developer_can_ask_for_the_text_locally_but_never_in_production(monkeypatch):
    def emit(lg):
        try:
            _fail()
        except DataError as exc:
            lg.warning("failed: %s", exc)
    monkeypatch.setenv(EXCEPTION_TEXT_ENV, "true")
    monkeypatch.setenv("IRA_ENVIRONMENT", "development")
    assert PRIVATE in _capture(TextFormatter(), emit)
    for env in ("staging", "production"):
        monkeypatch.setenv("IRA_ENVIRONMENT", env)
        assert PRIVATE not in _capture(TextFormatter(), emit), env


_LEVELS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


def _formats_exception_text(tree: ast.AST):
    """Log calls whose message is an f-string (or str()/repr()) of an `except ... as` name.
    Those are formatted before any filter sees them, so the filter cannot help."""
    names = {h.name for h in ast.walk(tree) if isinstance(h, ast.ExceptHandler) and h.name}

    def exc_expr(v) -> bool:
        if isinstance(v, ast.Name):
            return v.id in names
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id in ("str", "repr"):
            return bool(v.args) and isinstance(v.args[0], ast.Name) and v.args[0].id in names
        return isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "exception" and not v.args

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _LEVELS):
            continue
        base = node.func.value
        if not re.search(r"log", base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", ""), re.I):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Call) and arg.func and exc_expr(arg) and not isinstance(arg.func, ast.Attribute):
                yield node.lineno
            for sub in ast.walk(arg):
                if isinstance(sub, ast.FormattedValue) and exc_expr(sub.value):
                    yield node.lineno


def test_no_log_line_formats_an_exception_into_its_message_itself():
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("tests/") or "/tests/" in rel or "site-packages" in rel:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        found += [f"{rel}:{n}" for n in _formats_exception_text(tree)]
    assert not found, (
        "log lines that print an exception's text (use type(exc).__name__, or pass exc as an "
        "argument for the filter to reduce):\n  " + "\n  ".join(found))


def test_the_static_check_sees_the_shapes_it_is_meant_to_catch():
    src = '''
try:
    pass
except Exception as e:
    logger.warning(f"failed: {e}")
    log.error(f"failed: {e!r}")
    _log.info("failed: %s", str(e))
    logger.warning(f"fine: {type(e).__name__}")
    logger.warning("fine: %s", e)
'''
    assert len(list(_formats_exception_text(ast.parse(src)))) == 3


def test_a_console_mail_log_masks_the_recipient():
    assert _mask_address("carer.daughter@example.invalid") == "c***@example.invalid"
    assert _mask_address("no-at-sign") == "***"
