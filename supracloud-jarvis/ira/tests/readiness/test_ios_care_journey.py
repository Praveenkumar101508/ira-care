"""The iOS care journey's pure parts (scripts/simulator/ios_care_journey.py): Maestro's screen tree
turned into the node form the shared Android checks read, and the flows it writes. Nothing here
runs Maestro or a simulator."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture(scope="module")
def j():
    sys.path.insert(0, str(ROOT / "scripts" / "emulator"))
    spec = importlib.util.spec_from_file_location("ios_care_journey_under_test", ROOT / "scripts" / "simulator" / "ios_care_journey.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# A Maestro `hierarchy` answer for an iOS screen, cut down: a tree of attribute maps.
TREE = {"attributes": {}, "children": [
    {"attributes": {"resource-id": "tab-home", "accessibilityText": "Home", "selected": "true"}, "children": []},
    {"attributes": {"resource-id": "card-person", "accessibilityText": "", "text": ""}, "children": [
        {"attributes": {"text": "Ada", "accessibilityText": ""}, "children": []}]},
    {"attributes": {"resource-id": "mode-dark", "accessibilityText": "Dark", "selected": "false"}, "children": []},
    {"attributes": {"text": "Cannot reach the care server. Alerts may be out of date."}, "children": []},
    {"attributes": {"bounds": "[0,0][10,10]"}, "children": []},
]}


def test_the_tree_becomes_nodes_the_shared_checks_read(j):
    from adbkit import find_id, has_text, is_selected, offline_problems
    nodes = j.nodes_from_hierarchy(TREE)
    assert find_id(nodes, "tab-home") is not None and find_id(nodes, "card-person") is not None
    assert has_text(nodes, "Ada")
    assert is_selected(find_id(nodes, "tab-home")) and not is_selected(find_id(nodes, "mode-dark"))
    assert offline_problems(nodes) == []
    assert all(n["text"] or n["content-desc"] or n["resource-id"] for n in nodes), "empty nodes are dropped"
    assert find_id(j.nodes_from_hierarchy({}), "tab-home") is None


def test_a_label_stands_in_for_missing_text(j):
    nodes = j.nodes_from_hierarchy({"attributes": {"resource-id": "x", "accessibilityText": "Sign in"}})
    assert nodes[0]["text"] == "Sign in"


def test_flows_are_valid_yaml_with_the_app_id_and_quoted_text(j):
    text = j.flow([{"launch": True}, {"tap": "advanced"}, {"type": ["password", 'a"b: c#d']},
                   {"scroll_to": "sign-out"}, {"tap_text": "Back"}, {"stop": True}])
    header, body = text.split("---\n")
    assert yaml.safe_load(header) == {"appId": "care.ira.family"}
    cmds = yaml.safe_load(body)
    assert cmds[0] == {"launchApp": {"stopApp": False}}
    assert {"tapOn": {"id": "advanced"}} in cmds
    assert {"inputText": 'a"b: c#d'} in cmds, "awkward characters survive quoting"
    # the keyboard is dismissed on the heading, never with hideKeyboard (run 46053932767)
    assert "hideKeyboard" not in cmds and {"pressKey": "Enter"} in cmds
    # a field is cleared before typing, so the server field's default address is replaced (run 46057616511)
    i = cmds.index({"inputText": 'a"b: c#d'})
    assert cmds[i - 1] == {"eraseText": 80} and cmds[i - 2] == {"tapOn": {"id": "password"}}
    assert {"scrollUntilVisible": {"element": {"id": "sign-out"}, "direction": "DOWN", "timeout": 15000}} in cmds
    assert cmds[-1] == "stopApp"
    # every tap and every typed field is scrolled into view first (run 46032445452)
    i = cmds.index({"tapOn": {"id": "advanced"}})
    assert cmds[i - 1] == {"scrollUntilVisible": {"element": {"id": "advanced"}, "direction": "DOWN", "timeout": 10000,
                                         "visibilityPercentage": 50, "centerElement": True, "optional": True}}
    up = yaml.safe_load(j.flow([{"tap": "sign-in", "up": True}]).split("---\n")[1])
    assert up[0]["scrollUntilVisible"]["direction"] == "UP" and up[1] == {"tapOn": {"id": "sign-in"}}


def test_a_system_sheet_is_answered_only_when_it_is_there(j):
    # iOS's "Save Password?" sheet covered the app after sign-in (run 46093555159).
    cmds = yaml.safe_load(j.flow([{"dismiss_text": "Not Now"}]).split("---\n")[1])
    assert cmds == [{"tapOn": {"text": "Not Now", "optional": True}}]


def test_the_password_sheet_is_answered_until_it_is_gone_and_never_when_absent(j, monkeypatch):
    monkeypatch.setattr(j.time, "sleep", lambda s: None)
    sheet = [{"text": "Save Password?", "content-desc": "", "resource-id": ""}, {"text": "Not Now", "content-desc": "", "resource-id": ""}]
    home = [{"text": "Home", "content-desc": "", "resource-id": "tab-home"}]

    def journey(screens):
        jr = object.__new__(j.Journey)
        jr.ran = []
        jr.nodes = lambda: screens.pop(0) if screens else home
        jr.run = lambda steps: jr.ran.append(steps) or True
        return jr

    jr = journey([sheet, home])   # up when read, gone after one "Not Now"
    assert jr.answer_password_sheet() == home and jr.ran == [[{"dismiss_text": "Not Now"}]]
    jr = journey([sheet, sheet, sheet, sheet])   # never goes: three tries, then the screen is returned as it is
    assert jr.answer_password_sheet() == sheet and len(jr.ran) == 3
    jr = journey([home])   # no sheet: nothing tapped
    assert jr.answer_password_sheet() == home and jr.ran == []


def test_typing_one_keystroke_at_a_time_types_every_character(j):
    cmds = yaml.safe_load(j.flow([{"type": ["username", "ab@c"], "slow": True}]).split("---\n")[1])
    assert [c["inputText"] for c in cmds if "inputText" in c] == ["a", "b", "@", "c"]


def test_an_unknown_step_is_refused(j):
    with pytest.raises(ValueError):
        j.flow([{"swipe": "left"}])
