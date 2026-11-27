"""The iOS simulator journey's pure parts (scripts/simulator/ios_journey.py): choosing a simulator
from simctl's listing, reading the app's journey probe from its console, and telling a new crash
report of the app from the rest. Nothing here runs xcrun.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]


@pytest.fixture(scope="module")
def journey():
    spec = importlib.util.spec_from_file_location("ios_journey_under_test", ROOT / "scripts" / "simulator" / "ios_journey.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LISTING = json.dumps({"devices": {
    "com.apple.CoreSimulator.SimRuntime.iOS-17-5": [
        {"name": "iPhone 15", "udid": "A", "state": "Shutdown", "isAvailable": True},
        {"name": "iPad Air", "udid": "B", "state": "Shutdown", "isAvailable": True}],
    "com.apple.CoreSimulator.SimRuntime.iOS-18-1": [
        {"name": "iPhone 16", "udid": "C", "state": "Shutdown", "isAvailable": True},
        {"name": "iPhone 16 Pro", "udid": "D", "state": "Shutdown", "isAvailable": False}],
    "com.apple.CoreSimulator.SimRuntime.watchOS-11-0": [
        {"name": "Apple Watch Series 10", "udid": "E", "state": "Shutdown", "isAvailable": True}],
}})


def test_the_named_iphone_is_chosen_when_asked_for(journey):
    assert journey.pick_device(LISTING, "iPhone 15")["udid"] == "A"


def test_without_a_name_the_newest_available_iphone_is_chosen_and_never_a_watch_or_ipad(journey):
    chosen = journey.pick_device(LISTING)
    assert chosen["udid"] == "C" and "iOS-18-1" in chosen["runtime"]


def test_an_unavailable_simulator_is_never_chosen_even_by_name(journey):
    assert journey.pick_device(LISTING, "iPhone 16 Pro")["udid"] == "C", "falls back to the newest available"


def test_a_booted_iphone_is_preferred(journey):
    data = json.loads(LISTING)
    data["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-17-5"][0]["state"] = "Booted"
    assert journey.pick_device(json.dumps(data))["udid"] == "A"


def test_no_iphone_means_none(journey):
    assert journey.pick_device(json.dumps({"devices": {}})) is None


CONSOLE = """2026-11-26 23:10:01.123 IRACARE[512:9000] Running "main" with {"rootTag":1}
2026-11-26 23:10:02.001 IRACARE[512:9000] ira-journey {"screen":"sign-in","signedIn":false}
2026-11-26 23:10:02.002 IRACARE[512:9000] ira-journey not json at all
2026-11-26 23:10:09.500 IRACARE[512:9000] ira-journey {"screen":"home","signedIn":true}
"""


def test_probe_lines_are_read_from_the_console_in_order_and_junk_is_skipped(journey):
    assert journey.probe_lines(CONSOLE) == [{"screen": "sign-in", "signedIn": False}, {"screen": "home", "signedIn": True}]
    assert journey.screens(CONSOLE) == ["sign-in", "home"]
    assert journey.screens("no probe here\n") == []


def test_only_new_crash_reports_naming_the_app_count(journey):
    before = {"IRACARE-2026-11-26-100000.ips", "Safari-2026-11-26-100100.ips"}
    after = before | {"IRACARE-2026-11-26-231005.ips", "Mail-2026-11-26-231006.ips"}
    assert journey.new_crash_reports(before, after, "IRACARE") == ["IRACARE-2026-11-26-231005.ips"]
    assert journey.new_crash_reports(before, before, "IRACARE") == []


def test_the_probe_listener_collects_what_the_app_posts_and_ignores_junk(journey):
    import urllib.request

    listener = journey.ProbeListener(port=0).start()
    port = listener.server.server_address[1]
    try:
        for body in (b'{"screen": "sign-in", "signedIn": false}', b"not json", b'{"screen": "home", "signedIn": true}'):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/journey", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - loopback
                assert r.status == 204
        assert listener.screens() == ["sign-in", "home"]
    finally:
        listener.stop()


def test_the_probe_is_read_from_the_unified_log_and_errors_are_picked_out(journey):
    log = ('2026-11-27 10:31:02.1 IRACARE[811:9a] [javascript] ira-journey {"screen":"sign-in","signedIn":false}\n'
           "2026-11-27 10:31:02.2 IRACARE[811:9a] some framework chatter\n"
           "2026-11-27 10:31:03.0 IRACARE[811:9a] [javascript] Unhandled JS Exception: boom\n")
    assert journey.screens(log) == ["sign-in"]
    picked = journey.diagnostic_lines(log)
    assert len(picked) == 2 and "chatter" not in " ".join(picked)


def test_the_pid_is_read_from_simctl_launch(journey):
    assert journey.launch_pid("care.ira.family: 4321\n", "care.ira.family") == 4321
    assert journey.launch_pid("An error was encountered processing the command", "care.ira.family") is None
    assert journey.launch_pid("", "care.ira.family") is None
