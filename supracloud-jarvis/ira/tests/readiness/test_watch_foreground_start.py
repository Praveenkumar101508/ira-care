"""The watch starts its health foreground service only through the permission-checked helper.

Starting a `health` foreground service without a sensor permission throws a SecurityException
and kills the app (targetSdk 35). The Wear OS emulator journey found exactly that straight after
pairing (run 45760714043). CareForegroundService.start/send/sendChoice now check
ForegroundPermission first; these checks keep every start behind them.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SRC = ROOT / "watch" / "app" / "src" / "main" / "java"
SERVICE = SRC / "care" / "ira" / "watch" / "service" / "CareForegroundService.kt"


def unguarded_starts(files: dict[str, str]) -> list[str]:
    """`startForegroundService(` anywhere but inside the service's guarded startWith helper."""
    found = []
    for name, text in files.items():
        for m in re.finditer(r"startForegroundService\(", text):
            line = text.count("\n", 0, m.start()) + 1
            if name.endswith("CareForegroundService.kt"):
                helper = text.rfind("private fun startWith(", 0, m.start())
                guard = text.find("ForegroundPermission.allowed(context)", helper)
                if helper != -1 and guard != -1 and guard < m.start() and "\n        }" not in text[helper:m.start()]:
                    continue
            found.append(f"{name}:{line}")
    return found


def _sources() -> dict[str, str]:
    return {str(p.relative_to(SRC)): p.read_text(encoding="utf-8") for p in SRC.rglob("*.kt")}


def test_every_foreground_start_is_permission_checked():
    assert unguarded_starts(_sources()) == []


def test_negative_control_a_direct_start_is_caught():
    leaky = dict(_sources())
    leaky["care/ira/watch/service/BootReceiver.kt"] += (
        "\nfun boot(c: Context) { c.startForegroundService(Intent(c, CareForegroundService::class.java)) }\n")
    assert unguarded_starts(leaky) == [f"care/ira/watch/service/BootReceiver.kt:{leaky['care/ira/watch/service/BootReceiver.kt'].count(chr(10))}"]


def test_the_permission_screen_starts_the_service_after_the_grant():
    main = (SRC / "care" / "ira" / "watch" / "ui" / "MainActivity.kt").read_text(encoding="utf-8")
    grant = main.index("RequestMultiplePermissions()")
    assert "CareForegroundService.start(this@MainActivity)" in main[grant:grant + 900]
