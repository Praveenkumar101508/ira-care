"""The stand-in device test: the test build's server address, its server, and what is public.

.github/workflows/watch-device-test.yml runs a care server a real watch can reach, from a public
repository, so its logs and summary are public. These checks keep it:

- manual only, with an hours input of 1 to 5 and a job limit under GitHub's 6 hours that still
  leaves room for start-up;
- reading exactly one repository secret (IRA_WATCH_TEST_DASHBOARD_PASSWORD), generating and masking
  every other secret before its first use, and printing neither the API's log nor a pairing code;
- configuring no text, call or SOS provider (log-only), and checking the tunnel client's digest;
- shut down by itself, and building only the debug APK (the release build is untouched).

scripts/ci/check_watch_release_server.py (the built APKs) and the seed's coordinator password are
checked here too, each with a negative control.
"""
from __future__ import annotations

import copy
import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / ".github" / "workflows" / "watch-device-test.yml"
SECRET_NAME = "IRA_WATCH_TEST_DASHBOARD_PASSWORD"
GENERATED = ["PG_SUPER", "PG_APP", "PG_MIGRATOR", "PG_WORKER", "REDIS_PW", "IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD"]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _doc() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(doc: dict, job: str) -> list[dict]:
    return doc["jobs"][job]["steps"]


def problems(doc: dict) -> list[str]:
    """Everything wrong with the workflow against the rules in the module docstring."""
    out: list[str] = []
    on = doc.get("on", doc.get(True)) or {}
    if set(on) != {"workflow_dispatch"}:
        out.append(f"triggers must be workflow_dispatch only, found {sorted(on)}")
    hours = ((on.get("workflow_dispatch") or {}).get("inputs") or {}).get("hours") or {}
    options = [int(o) for o in hours.get("options", [])]
    if not options or min(options) < 1 or max(options) > 5 or hours.get("default") != "3":
        out.append(f"hours must be a choice of 1 to 5 with default 3, found {options} / {hours.get('default')}")
    job = doc["jobs"].get("test-server", {})
    limit = job.get("timeout-minutes", 10_000)
    if options and not (max(options) * 60 + 15 <= limit < 360):
        out.append(f"test-server timeout {limit} must cover the longest stay plus start-up and stay under 360")
    text = yaml.safe_dump(doc)
    secrets_used = set(re.findall(r"secrets\.([A-Z0-9_]+)", text))
    if secrets_used != {SECRET_NAME}:
        out.append(f"exactly one repository secret may be read ({SECRET_NAME}), found {sorted(secrets_used)}")
    if re.search(r"TWILIO_(ACCOUNT_SID|AUTH_TOKEN|FROM)\s*[:=]", text) or "IRA_CARE_SMS" in text:
        out.append("a messaging provider is configured")
    runs = [s.get("run", "") for s in _steps(doc, "test-server")]
    gen = next((r for r in runs if "secrets.token_hex" in r), "")
    if not all(n in gen for n in GENERATED) or gen.find("::add-mask::") > gen.find('>> "$GITHUB_ENV"'):
        out.append("every generated secret must be masked before it is written anywhere")
    for r in runs:
        for line in r.splitlines():
            if re.search(r"\b(cat|tail|head|less)\b[^|]*api\.log", line) or re.search(r"\b(cat|tail|head)\b[^|]*seed\.json", line):
                out.append(f"prints a private log or the seed: {line.strip()}")
            if "GITHUB_STEP_SUMMARY" in r and re.search(r"echo .*\$(PG_|REDIS_PW|IRA_SECRET_KEY|IRA_ADMIN_PASSWORD|IRA_DEMO_COORDINATOR)", line):
                out.append(f"puts a secret in the summary: {line.strip()}")
    for s in _steps(doc, "test-server"):
        if "upload-artifact" in str(s.get("uses", "")):
            out.append("the server job uploads files (its logs are private)")
    if not any("cloudflared\" tunnel" in r and "trycloudflare" in r for r in runs) or not any("sha256sum" in r and ".digest" in r for r in runs):
        out.append("the tunnel must be a Cloudflare Quick Tunnel with its client checked against the release digest")
    if not any("HOURS * 3600" in r for r in runs):
        out.append("the server must stop by itself after the chosen hours")
    last = _steps(doc, "test-server")[-1]
    if last.get("if") != "always()" or "docker rm -f" not in last.get("run", ""):
        out.append("the last step must always shut everything down")
    apk = " ".join(s.get("run", "") for s in _steps(doc, "watch-apk"))
    if "assembleWearosDebug" not in apk or "Release" in apk:
        out.append("the APK job builds the debug (test) build only")
    return out


def test_the_device_test_workflow_follows_its_rules():
    assert problems(_doc()) == []


@pytest.mark.parametrize("mutate, expect", [
    (lambda d: d[True].update({"push": None}), "triggers"),
    (lambda d: d[True]["workflow_dispatch"]["inputs"]["hours"]["options"].append("6"), "hours"),
    (lambda d: d["jobs"]["test-server"].update({"timeout-minutes": 360}), "timeout"),
    (lambda d: d["jobs"]["test-server"]["env"].update({"X": "${{ secrets.IRA_OTHER }}"}), "exactly one repository secret"),
    (lambda d: d["jobs"]["test-server"]["env"].update({"IRA_CARE_TWILIO_AUTH_TOKEN": "x"}), "messaging provider"),
    (lambda d: _steps(d, "test-server")[-2].update({"run": "tail -n 50 \"$RUNNER_TEMP/api.log\""}), "private log"),
    (lambda d: _steps(d, "test-server").insert(3, {"uses": "actions/upload-artifact@x", "with": {"path": "x"}}), "uploads files"),
    (lambda d: _steps(d, "test-server").pop(), "shut everything down"),
    (lambda d: _steps(d, "watch-apk")[3].update({"run": "./gradlew assembleWearosRelease"}), "debug (test) build only"),
])
def test_negative_controls(mutate, expect):
    doc = copy.deepcopy(_doc())
    if True not in doc:
        doc[True] = doc.pop("on")
    mutate(doc)
    assert any(expect in p for p in problems(doc)), problems(doc)


def test_masking_order_negative_control():
    doc = copy.deepcopy(_doc())
    for s in _steps(doc, "test-server"):
        if "secrets.token_hex" in s.get("run", ""):
            s["run"] = s["run"].replace('echo "::add-mask::$value"\n', "") + '\necho "::add-mask::$value"\n'
    assert any("masked before" in p for p in problems(doc))


def test_the_summary_names_the_secret_and_never_a_code():
    text = WORKFLOW.read_text(encoding="utf-8")
    summary = text[text.index("## IRA CARE watch test server"):text.index('>> "$GITHUB_STEP_SUMMARY"')]
    assert SECRET_NAME in summary and "praveen@example.com" in summary
    assert "cp1." not in summary and "${{ secrets" not in summary


def test_manual_workflow_list_includes_it():
    manual = _load_module("manual_wf", Path(__file__).with_name("test_manual_workflows.py"))
    assert manual.MANUAL.get("watch-device-test.yml") == 340


# ── the built APKs ───────────────────────────────────────────────────────────────────────────────

CHECK = _load_module("check_watch_release_server", ROOT / "scripts" / "ci" / "check_watch_release_server.py")


def test_release_apk_check_passes_a_clean_release():
    dex = [b"\x00https://care.example.invalid\x00care.ira.watch\x00"]
    assert CHECK.release_dex_problems(dex, "https://care.example.invalid") == []
    assert CHECK.manifest_problems('E: application\n  A: android:label="IRA CARE"\n') == []
    assert CHECK.debug_dex_problems([b"care_test_server\x00http://10.0.2.2:8000"]) == []


@pytest.mark.parametrize("dex, url, expect", [
    ([b"https://care.example.invalid http://10.0.2.2:8000"], "https://care.example.invalid", "10.0.2.2"),
    ([b"https://care.example.invalid care_test_server"], "https://care.example.invalid", "care_test_server"),
    ([b"https://other.example"], "https://care.example.invalid", "not in the code"),
    ([b"http://care.example.org"], "http://care.example.org", "not https"),
])
def test_release_apk_check_negative_controls(dex, url, expect):
    assert any(expect in p for p in CHECK.release_dex_problems(dex, url))


def test_release_apk_manifest_and_debug_negative_controls():
    assert CHECK.manifest_problems('A: android:usesCleartextTraffic(0x010104ec)=true') != []
    assert CHECK.manifest_problems('A: android:networkSecurityConfig(0x01010527)=@0x7f120000') != []
    assert CHECK.debug_dex_problems([b"nothing here"]) != []


def test_ci_runs_the_release_unit_tests_and_the_apk_check():
    tests = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    assert "testWearosReleaseUnitTest" in tests and "testAospReleaseUnitTest" in tests
    assert "scripts/ci/check_watch_release_server.py" in tests


# ── the seed's coordinator password ──────────────────────────────────────────────────────────────

def _coordinator_password_fn():
    """seed_demo.coordinator_password, read without importing the server modules the seed needs."""
    src = (ROOT / "scripts" / "demo" / "seed_demo.py").read_text(encoding="utf-8")
    body = src[src.index("COORDINATOR_PASSWORD_ENV ="):src.index("    return given") + len("    return given")]
    ns: dict = {"os": __import__("os"), "fresh_password": lambda: "random-abc-def"}
    exec(body, ns)  # noqa: S102 - our own source, a pure function
    return ns["coordinator_password"]


def test_seed_takes_the_coordinator_password_from_the_environment(monkeypatch):
    fn = _coordinator_password_fn()
    monkeypatch.delenv("IRA_DEMO_COORDINATOR_PASSWORD", raising=False)
    assert fn() == "random-abc-def"
    monkeypatch.setenv("IRA_DEMO_COORDINATOR_PASSWORD", "a-long-enough-pass")
    assert fn() == "a-long-enough-pass"
    monkeypatch.setenv("IRA_DEMO_COORDINATOR_PASSWORD", "short")
    with pytest.raises(SystemExit):
        fn()
    src = (ROOT / "scripts" / "demo" / "seed_demo.py").read_text(encoding="utf-8")
    assert 'coordinator_password() if key == "coordinator" else fresh_password()' in src
