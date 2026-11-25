"""The simulated sensors' signal model, built and run on the host (E3.c, E3.d, E3.e).

os/device/ira/sensors_sim/sim_signals.cpp is the part of IRA CARE OS's simulated Sensors HAL
that needs no Android: heart rate, accelerometer, gyroscope and off-body. Its host test checks
it against docs/hardware/SENSOR_HAL.md (units, rates, NO_CONTACT off the wrist, a fall the rule
detector sees, on-change off-body, determinism). The HAL service around it is unbuilt."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SIM = ROOT / "os/device/ira/sensors_sim"
CXX = shutil.which("g++") or shutil.which("clang++")


def _build_and_run(tmp_path: Path, model: Path) -> subprocess.CompletedProcess:
    if CXX is None:
        pytest.skip("no host C++ compiler")
    exe = tmp_path / "sim_test"
    build = subprocess.run([CXX, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O1", f"-I{SIM}", str(model),
                            str(SIM / "sim_signals_test.cpp"), "-o", str(exe)], capture_output=True, text=True, timeout=180)
    assert build.returncode == 0, build.stderr
    return subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)


def test_the_simulated_signals_meet_the_sensor_contract(tmp_path):
    run = _build_and_run(tmp_path, SIM / "sim_signals.cpp")
    assert run.returncode == 0, run.stdout
    assert "PASSED: 0 failure(s)" in run.stdout
    assert run.stdout.count("ok   ") >= 14


def test_negative_control_acceleration_in_g_instead_of_m_s2_is_caught(tmp_path):
    """A model that reported g rather than m/s^2 would make every fall threshold wrong."""
    bad = tmp_path / "sim_signals.cpp"
    src = (SIM / "sim_signals.cpp").read_text(encoding="utf-8")
    mutated = src.replace("double x = 0.4, y = 0.6, z = kGravity;", "double x = 0.04, y = 0.06, z = 1.0;")
    assert mutated != src
    bad.write_text(mutated, encoding="utf-8")
    run = _build_and_run(tmp_path, bad)
    assert run.returncode != 0 and "FAIL at rest the acceleration magnitude is gravity" in run.stdout


def test_the_hal_service_says_its_readings_are_simulated():
    hal = (SIM / "SimSensorsHal.cpp").read_text(encoding="utf-8")
    assert 'kVendor[] = "IRA simulated"' in hal and "UNBUILT" in hal
    bp = (SIM / "Android.bp").read_text(encoding="utf-8")
    assert "UNBUILT" in bp and '"android.hardware.sensors-service.ira-sim"' in bp
