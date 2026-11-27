"""The synthetic fall trigger lives in the watch's debug source set and nowhere else.

The emulator journey injects a suspected fall with `am broadcast` (there is no wrist to drop).
The receiver that takes it, and its manifest entry, are in `watch/app/src/debug` alone, so no
release build carries them; the main source set holds only FallInjection, which nothing in main
feeds. These checks keep that so (the JVM test SyntheticFallReleaseTest does the same from Gradle).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
APP = ROOT / "watch" / "app" / "src"
A = "{http://schemas.android.com/apk/res/android}"
MARKERS = ("SyntheticFallReceiver", "SYNTHETIC_FALL")


def receivers(manifest_xml: str) -> list[str]:
    return [r.get(A + "name") for r in ET.fromstring(manifest_xml).iter("receiver")]


def synthetic_fall_references(src: Path) -> list[str]:
    """Files outside src/debug and src/test that name the receiver or its action."""
    out = []
    for source_set in sorted(p for p in src.iterdir() if p.is_dir() and p.name not in ("debug", "test")):
        for f in sorted(p for p in source_set.rglob("*") if p.is_file()):
            text = f.read_text(encoding="utf-8", errors="replace")
            if any(m in text for m in MARKERS):
                out.append(str(f.relative_to(src)))
    return out


def test_debug_source_set_declares_the_receiver_and_main_only_the_boot_receiver():
    debug = (APP / "debug" / "AndroidManifest.xml").read_text(encoding="utf-8")
    assert receivers(debug) == ["care.ira.watch.debug.SyntheticFallReceiver"]
    assert "care.ira.watch.debug.SYNTHETIC_FALL" in debug
    assert (APP / "debug" / "java" / "care" / "ira" / "watch" / "debug" / "SyntheticFallReceiver.kt").exists()
    assert receivers((APP / "main" / "AndroidManifest.xml").read_text(encoding="utf-8")) == [".service.BootReceiver"]
    for manifest in APP.glob("*/AndroidManifest.xml"):
        if manifest.parent.name not in ("debug", "main"):
            assert receivers(manifest.read_text(encoding="utf-8")) == [], manifest


def test_no_other_source_set_names_the_receiver():
    assert synthetic_fall_references(APP) == []


def test_the_receiver_feeds_the_detector_path_and_nothing_in_main_feeds_it():
    receiver = (APP / "debug/java/care/ira/watch/debug/SyntheticFallReceiver.kt").read_text(encoding="utf-8")
    assert "FallInjection.inject(" in receiver and "FallInjection.fromExtras(" in receiver
    main = [p for p in (APP / "main").rglob("*.kt") if "FallInjection.inject(" in p.read_text(encoding="utf-8")]
    assert main == []
    service = (APP / "main/java/care/ira/watch/service/CareForegroundService.kt").read_text(encoding="utf-8")
    assert "FallInjection.events.collect { onSuspectedFall(it) }" in service
    # The detector's own events go through the same method, so an injected fall proves that path.
    # A detector's suspected fall reaches onSuspectedFall: at once, or after the confirmation stage
    # when the carer has turned it on (round 9, FallConfirmation).
    assert "is FallEvent.SuspectedFall -> {" in service
    assert "if (deps.fallSettings.confirmBeforeAsking) pending = pending ?: event else onSuspectedFall(event)" in service
    assert "if (decision.confirm) onSuspectedFall(held)" in service


def test_negative_control_a_receiver_in_release_is_caught(tmp_path):
    (tmp_path / "debug").mkdir()
    (tmp_path / "debug" / "AndroidManifest.xml").write_text('<manifest><receiver android:name="care.ira.watch.debug.SyntheticFallReceiver"/></manifest>')
    assert synthetic_fall_references(tmp_path) == []
    (tmp_path / "release").mkdir()
    (tmp_path / "release" / "AndroidManifest.xml").write_text('<manifest><receiver android:name="care.ira.watch.debug.SyntheticFallReceiver"/></manifest>')
    assert synthetic_fall_references(tmp_path) == ["release/AndroidManifest.xml"]
    (tmp_path / "main" / "java").mkdir(parents=True)
    (tmp_path / "main" / "java" / "X.kt").write_text('sendBroadcast(Intent("care.ira.watch.debug.SYNTHETIC_FALL"))')
    assert synthetic_fall_references(tmp_path) == ["main/java/X.kt", "release/AndroidManifest.xml"]
