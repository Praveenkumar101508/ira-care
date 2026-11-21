"""ONNX Runtime's usage reporting to Microsoft is off in every IRA process.

The official ONNX Runtime wheels for Linux (1.29 onwards) connect to
Microsoft's 1DS collector, mobile.events.data.microsoft.com, from native code
the moment the library is imported, unless ORT_DISABLE_TELEMETRY is already in
the process environment. A genuine clean-Linux install on 31 October 2026
(pull request #190) saw the API try it at start-up: `voice/wakeword.py`
imports onnxruntime, and faster-whisper's voice-activity filter loads it on
the first transcription. Calling onnxruntime.disable_telemetry_events() after
the import is too late. Evidence:
supracloud-jarvis/docs/testing/evidence/onnxruntime-telemetry-off-2026-11-01/.

So the switch must be set before the first `import onnxruntime` in the process.
These tests start each entry point in a fresh interpreter with the variable
removed and check:

* the API (`main`) and the worker (`worker.main`) set it before they import any
  other IRA module, in the C environment native code reads as well as in
  `os.environ`;
* the two modules that lead to an onnxruntime import set it on their own, for
  any other process that loads them. A stand-in for onnxruntime records the
  environment at the moment it is imported; the real library never loads, so
  the suite itself reports nothing;
* an owner's explicit value is kept and a blank one is not;
* the template and both start scripts carry it.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from core.runtime.vendor_telemetry import SWITCHES, switch_off_vendor_telemetry

IRA_DIR = Path(__file__).resolve().parents[1]
ROOT = IRA_DIR.parent
SWITCH = "ORT_DISABLE_TELEMETRY"

# The values ONNX Runtime reads as "off": trimmed and lower-cased, one of these
# (onnxruntime/core/platform/telemetry_environment.h,
# IsTelemetryDisabledByEnvironment). Anything else, "" included, leaves it on.
_ORT_OFF_VALUES = {"1", "true", "yes", "on", "y"}


def _ort_reads_as_off(value: str | None) -> bool:
    return (value or "").strip().lower() in _ORT_OFF_VALUES


# Every top-level module and package of the backend except `core`, which holds
# the switch itself and imports only the standard library.
_IRA_TOP_LEVEL = sorted(
    ({p.stem for p in IRA_DIR.glob("*.py")}
     | {p.name for p in IRA_DIR.iterdir() if (p / "__init__.py").is_file()})
    - {"core", "tests", "conftest"}
)

# Runs in a fresh interpreter, from ira/, with the switch removed.
#   first_ira_import: stop at the first IRA module the entry point imports and
#                     record the switch there, as Python and as C getenv see it.
#   import / wake_available: import the entry point, then onnxruntime (directly,
#                     or through wakeword.is_available()); a stand-in records
#                     the switch when onnxruntime is imported.
_PROBE = textwrap.dedent(
    r"""
    import ctypes, importlib, importlib.abc, importlib.machinery, json, os, sys, types

    entry, mode, ira_top = sys.argv[1], sys.argv[2], set(json.loads(sys.argv[3]))
    seen = {"first_ira_import": None, "onnxruntime": []}

    def c_getenv(name):
        if sys.platform == "win32":  # no POSIX libc to ask; CI runs this on Linux
            return os.environ.get(name)
        libc = ctypes.CDLL(None)
        libc.getenv.restype = ctypes.c_char_p
        value = libc.getenv(name.encode())
        return None if value is None else value.decode()

    def both(name):
        return [os.environ.get(name), c_getenv(name)]

    class Stop(Exception):
        pass

    class Spy(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, name, path, target=None):
            if (mode == "first_ira_import" and seen["first_ira_import"] is None
                    and name.partition(".")[0] in ira_top
                    and name != entry and not entry.startswith(name + ".")):
                seen["first_ira_import"] = [name] + both("ORT_DISABLE_TELEMETRY")
                raise Stop
            if name == "onnxruntime":
                return importlib.machinery.ModuleSpec(name, self)
            return None

        def create_module(self, spec):
            seen["onnxruntime"].append(both("ORT_DISABLE_TELEMETRY"))
            return types.ModuleType(spec.name)

        def exec_module(self, module):
            pass

    sys.meta_path.insert(0, Spy())
    try:
        module = importlib.import_module(entry)
    except Stop:
        module = None
    if mode == "import":
        import onnxruntime  # noqa: F401  (faster-whisper's VAD does this lazily)
    elif mode == "wake_available":
        module.is_available()
    print("PROBE " + json.dumps(seen))
    """
)


def _probe(entry: str, mode: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k != SWITCH}
    # The CI placeholders Settings needs; never real credentials.
    for key, value in (("IRA_SECRET_KEY", "ci-test-secret-key"),
                       ("IRA_ADMIN_PASSWORD", "ci-test-password"),
                       ("POSTGRES_PASSWORD", "ci-test"), ("REDIS_PASSWORD", "ci-test"),
                       ("VLLM_API_KEY", "ci-test")):
        env.setdefault(key, value)
    env["PYTHONPATH"] = str(IRA_DIR)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, entry, mode, json.dumps(_IRA_TOP_LEVEL)],
        cwd=IRA_DIR, env=env, capture_output=True, text=True, timeout=300,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("PROBE ")]
    assert proc.returncode == 0 and lines, (
        f"probe of {entry} failed:\n{(proc.stdout + proc.stderr)[-4000:]}")
    return json.loads(lines[-1][len("PROBE "):])


# ── The entry points set it before anything else ────────────────────────────

@pytest.mark.parametrize("entry", ["main", "worker.main"])
def test_entry_point_sets_the_switch_before_importing_any_other_ira_module(entry):
    first = _probe(entry, "first_ira_import")["first_ira_import"]

    assert first is not None, f"{entry} imported no other IRA module"
    name, in_python, in_c = first
    assert in_python == "1" and in_c == "1", (
        f"{entry} imported {name} with {SWITCH} = {in_python!r} (os.environ), "
        f"{in_c!r} (C environment). It must already be '1': any later import could "
        f"load ONNX Runtime, whose native code reads the C environment and reports to "
        f"Microsoft at import.")


@pytest.mark.parametrize("path", ["main.py", "worker/main.py"])
def test_entry_point_calls_the_switch_before_any_non_stdlib_import(path):
    """The same order, read from the source: an independent second check."""
    tree = ast.parse((IRA_DIR / path).read_text(encoding="utf-8"))
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) \
                and getattr(node.value.func, "id", None) == "switch_off_vendor_telemetry":
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [node.module or ""] if isinstance(node, ast.ImportFrom) \
                else [alias.name for alias in node.names]
            for name in names:
                assert name.partition(".")[0] in stdlib \
                    or name == "core.runtime.vendor_telemetry", (
                        f"{path} imports {name} before calling switch_off_vendor_telemetry()")
    pytest.fail(f"{path} never calls switch_off_vendor_telemetry() at module level")


# ── The modules that import onnxruntime cover any other process ─────────────

@pytest.mark.parametrize("entry, mode", [
    ("voice.wakeword", "wake_available"),   # is_available() imports onnxruntime
    ("voice.stt", "import"),                # faster-whisper's VAD imports it lazily
])
def test_modules_that_lead_to_onnxruntime_set_the_switch_themselves(entry, mode):
    seen = _probe(entry, mode)["onnxruntime"]

    assert seen == [["1", "1"]], (
        f"a process that loads {entry} without main.py imported onnxruntime with "
        f"{SWITCH} = (os.environ, C environment) {seen}")


# ── The helper ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("before, after, changed", [
    (None, "1", [SWITCH]),
    ("", "1", [SWITCH]),        # start-ira.sh exports a blank .env line as ""
    ("   ", "1", [SWITCH]),
    ("0", "0", []),             # the owner's explicit choice is kept
    ("true", "true", []),
])
def test_helper_fills_absent_or_blank_and_keeps_explicit(before, after, changed):
    env = {} if before is None else {SWITCH: before}

    assert switch_off_vendor_telemetry(env) == changed
    assert env[SWITCH] == after


def test_the_value_the_helper_sets_is_one_onnxruntime_reads_as_off():
    assert _ort_reads_as_off(SWITCHES[SWITCH])


# ── The template and the start scripts ───────────────────────────────────────

def _env_example_value(key: str) -> str | None:
    for raw in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", raw)
        if match and match.group(1) == key:
            return re.sub(r"\s+#.*$", "", match.group(2)).strip()
    return None


def test_env_example_switches_onnxruntime_reporting_off():
    value = _env_example_value(SWITCH)

    assert value is not None, f".env.example does not set {SWITCH}"
    assert _ort_reads_as_off(value), (
        f"{SWITCH}={value!r} in .env.example leaves ONNX Runtime reporting to Microsoft")


_SH_EXPORT = 'export ORT_DISABLE_TELEMETRY="${ORT_DISABLE_TELEMETRY:-1}"'


def test_start_sh_exports_the_switch_after_env_and_before_the_processes():
    text = (ROOT / "start-ira.sh").read_text(encoding="utf-8")
    export_at = text.find(_SH_EXPORT)

    assert export_at != -1, f"start-ira.sh does not contain: {_SH_EXPORT}"
    assert text.find('done < "$ROOT/.env"') < export_at, "exported before .env is read"
    for start in ("-m uvicorn main:app", "-m worker.main"):
        assert export_at < text.find(start), f"exported after `{start}` starts"


@pytest.mark.parametrize("inherited, expected", [
    (None, "1"), ("", "1"), ("0", "0"), ("1", "1"),
])
def test_start_sh_export_fills_absent_or_blank(inherited, expected):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if inherited is not None:
        env[SWITCH] = inherited
    proc = subprocess.run(
        ["bash", "-c", _SH_EXPORT + '; printf %s "$ORT_DISABLE_TELEMETRY"'],
        env=env, capture_output=True, text=True, timeout=30, check=True,
    )
    assert proc.stdout == expected


def test_start_ps1_sets_the_switch_after_env_and_before_the_processes():
    text = (ROOT / "start-ira.ps1").read_text(encoding="utf-8")
    line = ('if (-not (Get-EnvOr "ORT_DISABLE_TELEMETRY" "")) '
            '{ [Environment]::SetEnvironmentVariable("ORT_DISABLE_TELEMETRY", "1", "Process") }')
    set_at = text.find(line)

    assert set_at != -1, f"start-ira.ps1 does not contain: {line}"
    assert text.find('Write-Host "Loaded .env"') < set_at, "set before .env is read"
    assert text.find("function Get-EnvOr") < set_at, "used before Get-EnvOr is defined"
    assert set_at < text.find('"-m","uvicorn","main:app"'), "set after the backend starts"
