"""Model weights whose licence forbids commercial use stay out of the commercial service
(owner direction D-109, docs/ai/MODEL_WEIGHTS.md).

OmniVoice's pre-trained weights are CC BY-NC. The cloud edition refuses to start with it selected
(tests/test_cloud_edition_boot_refusals.py); these tests hold the rest: nothing that builds or
configures a release selects, downloads or bakes in a non-commercial voice model, and the TTS
factory never loads one in the cloud edition even if a process skipped the settings check.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
IRA = ROOT / "supracloud-jarvis" / "ira"
#: Everything that builds or configures a release of the service.
RELEASE_FILES = sorted(
    [p for p in (ROOT / "deploy").rglob("*") if p.is_file()]
    + [p for p in (ROOT / ".github" / "workflows").glob("*.yml")]
)
NONCOMMERCIAL = re.compile(r"omnivoice|k2-fsa/OmniVoice", re.I)


def test_the_release_files_are_found():
    """The control: without files to read, the next test would pass on nothing."""
    names = {p.name for p in RELEASE_FILES}
    assert any(n.startswith("Dockerfile") for n in names) and any(n.endswith(".yml") for n in names)


@pytest.mark.parametrize("path", RELEASE_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_release_file_selects_or_fetches_a_noncommercial_voice_model(path):
    text = path.read_text(encoding="utf-8", errors="ignore")
    assert not NONCOMMERCIAL.search(text), f"{path.relative_to(ROOT)} names a non-commercial voice model"


def test_the_engine_list_names_omnivoice():
    import sys
    sys.path.insert(0, str(IRA))
    from config import NONCOMMERCIAL_VOICE_ENGINES
    assert "omnivoice" in NONCOMMERCIAL_VOICE_ENGINES


def test_the_tts_factory_never_loads_omnivoice_in_the_cloud_edition(monkeypatch):
    import sys
    import types
    sys.path.insert(0, str(IRA))
    loaded = []
    fake_omni = types.ModuleType("voice.tts_omnivoice")
    fake_omni.is_available = lambda: (True, "")
    fake_omni.IRAOmniVoiceTTS = lambda **kw: loaded.append("omnivoice") or "omnivoice"
    fake_kokoro = types.ModuleType("voice.tts")
    fake_kokoro.IRAKokoroTTS = lambda **kw: "kokoro"
    monkeypatch.setitem(sys.modules, "voice.tts_omnivoice", fake_omni)
    monkeypatch.setitem(sys.modules, "voice.tts", fake_kokoro)
    from voice import tts_factory

    monkeypatch.setenv("IRA_VOICE_ENGINE", "omnivoice")
    monkeypatch.setenv("IRA_EDITION", "cloud")
    assert tts_factory.make_tts("af_bella") == "kokoro" and loaded == []
    # Negative control: the private edition (research) still reaches the engine.
    monkeypatch.setenv("IRA_EDITION", "private")
    assert tts_factory.make_tts("af_bella") == "omnivoice" and loaded == ["omnivoice"]
