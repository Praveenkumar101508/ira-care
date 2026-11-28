"""The phone emulator journey's pure helpers (scripts/emulator/adbkit.py): what each step checks,
with a recorded shape of the app's screens. Nothing here touches adb or a server.
"""
from __future__ import annotations

import importlib.util
import pathlib
import struct
import zlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
PKG = "care.ira.family"


@pytest.fixture(scope="module")
def kit():
    spec = importlib.util.spec_from_file_location("adbkit_under_test", ROOT / "scripts" / "emulator" / "adbkit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def node(text="", rid="", desc="", selected=False) -> dict:
    out = {"text": text, "content-desc": desc, "class": "android.view.View", "package": PKG}
    if rid:
        out["resource-id"] = rid
    if selected:
        out["selected"] = "true"
    return out


def test_a_react_native_test_id_is_found_bare_or_qualified(kit):
    nodes = [node("Sign in", rid=f"{PKG}:id/sign-in"), node("x", rid="username")]
    assert kit.find_id(nodes, "sign-in")["text"] == "Sign in"
    assert kit.find_id(nodes, "username")["text"] == "x"
    assert kit.find_id(nodes, "password") is None
    assert kit.find_id(nodes, "sign") is None, "a prefix is not a match"


def test_adb_text_escapes_what_the_device_shell_would_eat(kit):
    assert kit.adb_text("ada@example.com") == "ada@example.com"
    assert kit.adb_text("two words") == "two%swords"
    assert kit.adb_text("a&b$c") == "a\\&b\\$c"


def test_jwt_claims_are_read_without_a_key(kit):
    import base64
    import json

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    token = f"{b64({'alg': 'HS256'})}.{b64({'sub': 'ada', 'jti': 'j1'})}.sig"
    assert kit.jwt_claims(token) == {"sub": "ada", "jti": "j1"}
    with pytest.raises(ValueError):
        kit.jwt_claims("not.a.jwt.at.all")


def test_the_apps_sessions_are_the_ones_the_script_did_not_open(kit):
    sessions = [{"id": "1", "user_agent": "ira-care-phone-journey/1"}, {"id": "2", "user_agent": "okhttp/4.12"}]
    assert [s["id"] for s in kit.app_sessions(sessions, "ira-care-phone-journey/1")] == ["2"]


def test_reading_labels_match_what_the_app_draws(kit):
    assert kit.reading_label({"kind": "heart_rate", "value": 71.6}) == "Heart rate 72 bpm"
    assert kit.reading_label({"kind": "steps", "value": 1203}) == "1203 steps"
    assert kit.reading_label({"kind": "on_wrist", "value": 1}) == "Watch on the wrist"
    assert kit.reading_label({"kind": "on_wrist", "value": 0}) == "Watch off the wrist"
    assert kit.reading_label({"kind": "battery", "value": 64}) == "Battery 64 %"


def test_the_person_record_needs_the_name_and_every_latest_reading(kit):
    status = {"latest_readings": [{"kind": "heart_rate", "value": 72}, {"kind": "battery", "value": 64}]}
    shown = [node("Ada Hartley"), node("Heart rate 72 bpm · 10:00"), node("Battery 64 %")]
    assert kit.person_record_problems(shown, "Ada Hartley", status) == []
    assert "reading 'Battery 64 %' not shown" in kit.person_record_problems(shown[:2], "Ada Hartley", status)
    assert kit.person_record_problems(shown, "Bert Lowe", status)[0].startswith("title")
    assert kit.person_record_problems(shown, "Ada Hartley", {"latest_readings": []}) == [
        "the API reports no latest readings to compare with"]


def test_the_plan_tab_needs_each_active_medicine_and_the_goals(kit):
    meds = [{"id": "m1", "name": "Ramipril", "dose": "5 mg", "times": ["08:00:00"], "active": True},
            {"id": "m2", "name": "Old one", "active": False}]
    plan = {"goals": "Stay steady on the stairs"}
    shown = [node("Ramipril · 5 mg · 08:00"), node(rid="care-plan-card"), node("Goals: Stay steady on the stairs")]
    assert kit.plan_problems(shown, meds, plan) == []
    assert kit.plan_problems(shown[1:], meds, plan) == ["medication 'Ramipril · 5 mg · 08:00' not shown"]
    assert kit.plan_problems(shown, meds, None) == ["the API reports no care plan to compare with"]
    assert kit.plan_problems(shown[:1], meds, plan) == ["no care plan card"]
    assert kit.plan_problems(shown[:2], meds, plan) == ["the care plan's goals are not shown"]
    assert kit.plan_problems(shown[:2], meds, {"goals": ""}) == [], "a plan without goals needs only its card"


def test_offline_the_home_screen_must_say_so_and_never_all_well(kit):
    assert kit.offline_problems([node(kit.STALE_WORDS)]) == []
    assert kit.offline_problems([node(kit.ALL_WELL_WORDS)]) == [
        "no out-of-date notice", f"{kit.ALL_WELL_WORDS!r} shown while offline"]
    assert kit.recovered_problems([node(kit.ALL_WELL_WORDS)]) == []
    assert kit.recovered_problems([node(kit.STALE_WORDS)]) == ["still says out of date", f"{kit.ALL_WELL_WORDS!r} not back"]


def test_the_carers_home_has_the_carers_cards_and_nothing_of_the_family_experience(kit):
    carer = [node("Today"), node(rid="card-visits"), node(rid="card-people"), node(rid="card-handover")]
    assert kit.carer_home_problems(carer) == []
    leaked = carer + [node(rid="card-person"), node("Sravani Perni")]
    problems = kit.carer_home_problems(leaked, family_words=("Sravani Perni",))
    assert "family-only card-person shown to the carer" in problems
    assert "previous account's words shown: 'Sravani Perni'" in problems
    assert kit.carer_home_problems(carer[1:]) == ["no 'Today' title"]
    # Talk to IRA is the wearer's alone (C-7.b).
    assert kit.carer_home_problems(carer + [node(rid="home-talk")]) == ["wearer-only home-talk shown to the carer"]


def test_after_sign_out_nothing_of_the_account_may_remain(kit):
    assert kit.nothing_revealed_problems([node("Sign in")], ("Ada",)) == []
    assert kit.nothing_revealed_problems([node("Ada"), node(rid="tab-home")], ("Ada",)) == [
        "private words shown: 'Ada'", "signed-in element tab-home shown"]


def test_every_live_app_session_must_be_revoked_by_sign_out(kit):
    before = [{"id": "s1", "revoked_at": None}, {"id": "s0", "revoked_at": "2026-11-26T10:00:00Z"}]
    assert kit.revocation_problems(before, [{"id": "s1", "revoked_at": "2026-11-26T11:00:00Z"}]) == []
    assert kit.revocation_problems(before, [{"id": "s1", "revoked_at": None}]) == ["session s1 still live after sign-out"]
    assert kit.revocation_problems([before[1]], []) == ["no live app session before sign-out"]


def _png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A tiny 8-bit RGB PNG of one colour, filter type 0, as screencap could write it."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def test_a_screenshot_pixel_tells_a_dark_wallpaper_from_the_cream(kit):
    assert kit.png_pixel(_png(4, 3, (20, 20, 30)), 1, 1) == (20, 20, 30)
    assert kit.is_dark((20, 20, 30)) and not kit.is_dark((250, 246, 238))
    with pytest.raises(ValueError):
        kit.png_pixel(_png(4, 3, (0, 0, 0)), 9, 9)
    with pytest.raises(ValueError):
        kit.png_pixel(b"not a png", 0, 0)


def test_an_empty_screen_reading_is_tried_again_and_never_stale(monkeypatch, tmp_path):
    """Runs 46040834373 and 46045629293: one empty uiautomator reading ended the sign-in step.
    The reading is now tried again, and the old file is removed before each try."""
    import argparse
    import importlib.util
    import sys
    sys.path.insert(0, str(ROOT / "scripts" / "emulator"))
    spec = importlib.util.spec_from_file_location("phone_smoke_under_test", ROOT / "scripts" / "emulator" / "phone_smoke.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    s = mod.PhoneSmoke(argparse.Namespace(out=str(tmp_path)))
    xml = '<hierarchy><node resource-id="username" text="" content-desc="Username" bounds="[0,0][10,10]"/></hierarchy>'
    answers, calls = iter(["", "", xml]), []

    def fake_adb(*args, **kw):
        calls.append(args)
        return next(answers) if args[:2] == ("exec-out", "cat") else ""

    s.adb = fake_adb
    assert s.dump(), "the third reading is used"
    assert sum(1 for c in calls if c[:3] == ("shell", "rm", "-f")) == 3
    # negative control: three empty readings give an empty screen, not an old one
    answers = iter(["", "", ""])
    assert s.dump() == []
