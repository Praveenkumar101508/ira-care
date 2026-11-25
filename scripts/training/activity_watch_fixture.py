"""Write the parity fixture for the watch's Kotlin port of IRA Care Activity.

    python3 scripts/training/activity_watch_fixture.py

Runs the backend's reference implementation (supracloud-jarvis/ira/care/activity_estimate.py)
on fixed synthetic windows and writes the inputs, features, probabilities and answers to
watch/app/src/test/resources/activity_parity.json. The watch's ActivityParityTest must
reproduce them. Synthetic signals only: no recorded data from any person.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "supracloud-jarvis" / "ira"))
from care import activity_estimate as ae  # noqa: E402

OUT = ROOT / "watch/app/src/test/resources/activity_parity.json"
G = 9.80665


def window(kind: str, fs: float, seconds: float, rng) -> tuple[np.ndarray, np.ndarray]:
    t = np.arange(int(round(seconds * fs))) / fs
    n = len(t)
    acc = np.tile([0.3, -2.0, 9.5], (n, 1)) + rng.normal(0, 0.05, (n, 3))
    gyro = rng.normal(0, 0.02, (n, 3))
    if kind == "walk":
        acc += np.stack([2.5 * np.sin(2 * np.pi * 1.8 * t), 1.5 * np.sin(2 * np.pi * 0.9 * t + 0.4),
                         3.0 * np.sin(2 * np.pi * 1.8 * t + 1.1)], 1)
        gyro += np.stack([1.2 * np.sin(2 * np.pi * 0.9 * t), 0.6 * np.sin(2 * np.pi * 1.8 * t), 0.4 * np.cos(2 * np.pi * 0.9 * t)], 1)
    elif kind == "jog":
        acc += np.stack([9.0 * np.sin(2 * np.pi * 2.7 * t), 6.0 * np.sin(2 * np.pi * 1.35 * t), 8.0 * np.sin(2 * np.pi * 2.7 * t + 0.7)], 1)
        gyro += np.stack([3.0 * np.sin(2 * np.pi * 1.35 * t), 2.0 * np.sin(2 * np.pi * 2.7 * t), 1.0 * np.cos(2 * np.pi * 1.35 * t)], 1)
    elif kind == "eat":
        lift = (np.sin(2 * np.pi * 0.25 * t) > 0.6).astype(float)
        acc += np.stack([1.5 * lift, -3.0 * lift, -1.0 * lift], 1) + rng.normal(0, 0.2, (n, 3))
        gyro += np.stack([0.8 * np.gradient(lift) * fs, 0.3 * lift, 0.2 * np.sin(2 * np.pi * 0.5 * t)], 1)
    elif kind == "random":
        acc += rng.normal(0, 1.5, (n, 3))
        gyro += rng.normal(0, 0.8, (n, 3))
    return np.round(acc, 5), np.round(gyro, 5)


def case(name, acc, gyro, fs, gyro_fs=None):
    est = ae.estimate_activity(acc, gyro, fs_hz=fs, gyro_fs_hz=gyro_fs)
    a, g, why = ae.check_window(acc, gyro, fs_hz=fs, gyro_fs_hz=gyro_fs)
    out = {"name": name, "fs_hz": fs, "gyro_fs_hz": gyro_fs if gyro_fs is not None else fs,
           "acc": np.asarray(acc).tolist(), "gyro": np.asarray(gyro).tolist(),
           "activity": est.activity, "reason": est.reason, "candidate": est.candidate,
           "confidence": est.confidence}
    if why is None:
        out["features"] = ae.window_features(a, g).tolist()
        out["probabilities"] = ae.default_model().predict_proba(ae.window_features(a, g))[0].tolist()
    return out


def main() -> None:
    rng = np.random.default_rng(20261125)
    cases = []
    for kind in ("still", "walk", "jog", "eat", "random"):
        acc, gyro = window(kind, 20.0, 10.0, rng)
        cases.append(case(f"{kind}_20hz", acc, gyro, 20.0))
    acc, gyro = window("walk", 50.0, 12.0, rng)            # resampled from 50 Hz, last 10 s kept
    cases.append(case("walk_50hz_12s", acc, gyro, 50.0))
    acc, gyro = window("still", 25.0, 9.0, rng)            # 9 s at 25 Hz, gyro at 20 Hz
    _, gyro20 = window("still", 20.0, 9.0, rng)
    cases.append(case("still_mixed_rates", acc, gyro20, 25.0, 20.0))
    # refusals
    acc, gyro = window("still", 20.0, 10.0, rng)
    cases.append(case("too_short", acc[:100], gyro[:100], 20.0))
    cases.append(case("flat_accelerometer", np.tile([0.0, 0.0, 9.8], (200, 1)), gyro, 20.0))
    cases.append(case("implausible_gravity", acc / G, gyro, 20.0))   # in g, not m/s^2
    sat = acc.copy()
    sat[:40, 0] = 30.0
    cases.append(case("saturated_accelerometer", sat, gyro, 20.0))
    bad = acc.copy()
    bad[5, 1] = np.nan
    cases.append(case("not_finite", bad, gyro, 20.0))
    for c in cases:
        if c["name"] == "not_finite":
            c["acc"] = [[None if (isinstance(v, float) and np.isnan(v)) else v for v in row] for row in c["acc"]]
    OUT.write_text(json.dumps({"source": "care/activity_estimate.py", "model": "care/models/activity_model.json",
                               "feature_names": list(ae.FEATURE_NAMES), "cases": cases}, indent=1) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(cases)} cases")
    for c in cases:
        print(f"  {c['name']:26} {c['activity']!s:16} {c['reason']!s:24} {c['confidence']:.4f}")


if __name__ == "__main__":
    main()
