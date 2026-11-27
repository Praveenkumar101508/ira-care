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
    assert {"scrollUntilVisible": {"element": {"id": "sign-out"}, "direction": "DOWN", "timeout": 15000}} in cmds
    assert cmds[-1] == "stopApp"


def test_an_unknown_step_is_refused(j):
    with pytest.raises(ValueError):
        j.flow([{"swipe": "left"}])
