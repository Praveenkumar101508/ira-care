"""The care logs carry no name, address, phone number, private topic or reading (requirement C-6.a,
verification), across the whole care journey on a real PostgreSQL.

The unit tests (tests/care/test_care_safety_answers.py) check the log lines one function writes.
This runs the complete 22-step care journey (tests/tenancy/test_care_journey_db.py: sign-up,
circle, consent, check-ins, IRA's conversation, the watch's readings, a fall, SOS escalation with
messages through the recording provider, the watch's network failing and losing answers, offline
readings delivered later, resolution) with every logger in the process recorded at DEBUG, and then
reads every line that was written, its arguments and any exception text included.

What must never appear: the person's full and preferred name, her address, the family member's and
carer's names, the family phone number, the private topic she consented to, and the readings the
watch sent ("71 bpm" and its neighbours, written as the value and unit). A negative control shows
the recorder does catch such a line when one is written.
"""
from __future__ import annotations

import logging
import os
import re

import pytest

pytest.importorskip("asyncpg")
pytest.importorskip("httpx")
if not os.getenv("IRA_TEST_DATABASE_DSN") or not os.getenv("IRA_TEST_ADMIN_DSN"):
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy import test_care_journey_db as journey  # noqa: E402
from tests.tenancy._care_fixtures import _principal, client, pool  # noqa: E402,F401
from tests.tenancy.test_care_journey_db import FAMILY_PHONE, org  # noqa: E402,F401

#: Every personal value the journey gives the server (test_care_journey_db.py).
PERSONAL = ("Edith Example", "Edith", "1 Example Street", "Example Street", "Daughter Dee", "Carer Cal",
            FAMILY_PHONE, FAMILY_PHONE.lstrip("+"), "garden roses")
#: A reading written out: the journey's offline heart rates are 71 to 80 bpm.
READING = re.compile(r"\b(7[1-9]|80)\s*bpm\b", re.IGNORECASE)


class Everything(logging.Handler):
    """Every record any logger writes, formatted the way a log shipper would see it."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception as e:  # noqa: BLE001 - a record that cannot be formatted is still evidence
            text = f"{record.name} UNFORMATTABLE {record.msg!r} {record.args!r} ({type(e).__name__})"
        extra = {k: v for k, v in record.__dict__.items() if k not in logging.LogRecord("", 0, "", 0, "", (), None).__dict__}
        self.lines.append(text + (f" extra={extra!r}" if extra else ""))


@pytest.fixture
def recorder():
    root = logging.getLogger()
    handler = Everything()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    # Loggers that were quietened elsewhere are opened for the run, then put back.
    quiet = {name: lg.level for name, lg in logging.Logger.manager.loggerDict.items()
             if isinstance(lg, logging.Logger) and lg.level > logging.DEBUG}
    for name in quiet:
        logging.getLogger(name).setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
        for name, level in quiet.items():
            logging.getLogger(name).setLevel(level)


def offences(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        hits = [p for p in PERSONAL if p in line] + (["a reading"] if READING.search(line) else [])
        if hits:
            out.append(f"{hits}: {line[:300]}")
    return out


def test_the_recorder_catches_a_personal_line(recorder):
    """Negative control: the check below would see a name, a phone number or a reading."""
    log = logging.getLogger("ira.care.test_control")
    log.info("about %s at %s", "Edith Example", FAMILY_PHONE)
    log.debug("heart rate 72 bpm")
    try:
        raise ValueError("1 Example Street")
    except ValueError:
        log.exception("failed")
    found = offences(recorder.lines)
    assert len(found) == 3, found


@pytest.fixture
def log_only_too(monkeypatch):
    """The journey's recording provider (it counts sends) also hands every message to the log-only
    provider, the one a deployment without provider keys uses, so its log lines are read as well."""
    from care import providers
    from tests.tenancy.test_care_delivery_db import Recording
    shadow = providers.LogOnlyProvider()
    sms, call = Recording.send_sms, Recording.call

    async def send_sms(self, to, text, *, status_callback=None):
        await shadow.send_sms(to, text)
        return await sms(self, to, text, status_callback=status_callback)

    async def place_call(self, to, text, *, status_callback=None):
        await shadow.call(to, text)
        return await call(self, to, text, status_callback=status_callback)

    monkeypatch.setattr(Recording, "send_sms", send_sms)
    monkeypatch.setattr(Recording, "call", place_call)
    return shadow


async def test_no_log_line_of_the_whole_care_journey_carries_personal_data(client, org, recorder, log_only_too):
    await journey.test_the_complete_care_journey_in_twenty_two_steps(client, org)
    # The messages did name the person (so a leak would have had something to leak).
    assert any("Edith" in text for _, _, text in log_only_too.log), log_only_too.log[:3]
    care = [ln for ln in recorder.lines if ln.startswith("ira.care")]
    assert len(recorder.lines) > 50 and any("(log only)" in ln for ln in care), f"the journey should log: {len(recorder.lines)} lines, {len(care)} care lines"
    print(f"log lines read: {len(recorder.lines)}, from the care modules: {len(care)}, of them the provider: {sum("(log only)" in ln for ln in care)}")
    found = offences(recorder.lines)
    assert not found, "personal data in the logs:\n" + "\n".join(found[:30])
