#!/usr/bin/env python3
"""Original synthetic wrist-IMU scenarios for the fall pipeline (requirement F4.h, round 9).

    python3 scripts/eval/fall_synthetic_scenarios.py --out build/fall_synthetic --seed 7 --per-kind 20

Each scenario is a 50 Hz recording of x, y, z in g (gravity included, device frame: a still wrist
reads |a| = 1) with a label (`fall` or `not_fall`), a kind and the physical parameters that made
it. Nothing here is recorded from a person; every shape follows the reasoning in
`docs/ai/fall/SYNTHETIC_SCENARIOS.md`. The generator is deterministic for a seed and varies every
parameter within a range, so the set is a regression corpus, not a benchmark of real accuracy.

The kinds (falls, then the activities that provoke impact detectors):

  falls       forward, backward, sideways, from_sitting, slow_collapse, fall_then_moves,
              fall_wrist_under_body, stairs_tumble
  not_fall    clapping, knocking, hammering, table_slap, drop_into_chair, brushing_teeth,
              watch_put_on_table (off-wrist), wrist_shake, washing_hands, stairs_down, clipping_hit

Every recording starts with two seconds of a still, worn wrist, so a detector's buffers are full
before anything happens.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

HZ = 50
G_CLIP = 16.0          # the watch's accelerometer range, ±16 g; samples beyond it saturate

FALL_KINDS = ("forward", "backward", "sideways", "from_sitting", "slow_collapse", "fall_then_moves",
              "fall_wrist_under_body", "stairs_tumble")
NOT_FALL_KINDS = ("clapping", "knocking", "hammering", "table_slap", "drop_into_chair", "brushing_teeth",
                  "watch_put_on_table", "wrist_shake", "washing_hands", "stairs_down", "clipping_hit")


def _unit(v):
    v = np.asarray(v, float)
    return v / (np.linalg.norm(v) or 1.0)


def _rotate_towards(a, b, frac):
    """A unit vector `frac` of the way from unit `a` to unit `b` along the great circle."""
    a, b = _unit(a), _unit(b)
    dot = float(np.clip(a @ b, -1.0, 1.0))
    ang = math.acos(dot)
    if ang < 1e-6:
        return a
    return (math.sin((1 - frac) * ang) * a + math.sin(frac * ang) * b) / math.sin(ang)


def _random_direction(rng):
    v = rng.normal(size=3)
    return _unit(v)


def _still(rng, seconds, gravity, noise=0.02):
    n = int(seconds * HZ)
    return gravity[None, :] + rng.normal(0, noise, (n, 3))


def _motion(rng, seconds, gravity, amplitude, period_s, noise=0.03):
    """Rhythmic arm movement about `gravity`: a sinusoid of `amplitude` g along a wandering axis."""
    n = int(seconds * HZ)
    t = np.arange(n) / HZ
    axis = _random_direction(rng)
    swing = amplitude * np.sin(2 * math.pi * t / period_s)[:, None] * axis[None, :]
    wobble = rng.normal(0, noise, (n, 3))
    return gravity[None, :] + swing + wobble


def _impact(rng, gravity, peak_g, direction, width_samples=3, bounce=0.5):
    """A short spike of `peak_g` along `direction`, with one smaller bounce, over about 0.3 s."""
    n = int(0.3 * HZ)
    out = gravity[None, :].repeat(n, 0) + rng.normal(0, 0.05, (n, 3))
    d = _unit(direction)
    c = n // 3
    for i in range(width_samples):
        k = c + i
        if k < n:
            out[k] += d * peak_g * (1.0 if i == 0 else 0.55)
    kb = c + width_samples + 2
    if kb < n:
        out[kb] += d * peak_g * bounce * 0.4
    return out


def _clip(xyz):
    return np.clip(xyz, -G_CLIP, G_CLIP)


# ── falls ─────────────────────────────────────────────────────────────────────

def fall(rng, kind: str) -> tuple[np.ndarray, dict]:
    """A fall: the wrist leaves its standing orientation, a free-fall dip, an impact of 2.5 to 7 g,
    a settling half second, then lying. The gravity vector after the fall points another way
    (the body changed orientation); `fall_then_moves` has the person stirring afterwards and
    `slow_collapse` a weak impact. Returns the recording and its parameters."""
    g0 = _random_direction(rng)
    peak = float(rng.uniform(2.6, 7.0))
    turn_deg = float(rng.uniform(55, 150))
    if kind == "from_sitting":
        peak = float(rng.uniform(2.3, 4.5)); turn_deg = float(rng.uniform(45, 110))
    if kind == "slow_collapse":
        peak = float(rng.uniform(2.1, 3.2)); turn_deg = float(rng.uniform(50, 120))
    target = _rotate_towards(g0, _random_direction(rng), 1.0)
    # force the turn to the chosen angle
    axis = _unit(np.cross(g0, target)) if np.linalg.norm(np.cross(g0, target)) > 1e-6 else _unit([1, 0, 0])
    rad = math.radians(turn_deg)
    g1 = _unit(g0 * math.cos(rad) + np.cross(axis, g0) * math.sin(rad))
    parts = [_still(rng, 2.0, g0)]
    pre_move = _motion(rng, rng.uniform(0.6, 1.5), g0, rng.uniform(0.2, 0.5), 0.8)
    parts.append(pre_move)
    # free fall: gravity reading shrinks for 0.2 to 0.45 s
    n_ff = int(rng.uniform(0.2, 0.45) * HZ)
    ff = (g0[None, :] * np.linspace(1.0, 0.25, n_ff)[:, None]) + rng.normal(0, 0.05, (n_ff, 3))
    parts.append(ff)
    impact_dir = _unit(g1 + rng.normal(0, 0.3, 3))
    parts.append(_impact(rng, g1, peak, impact_dir, width_samples=int(rng.integers(2, 5)), bounce=rng.uniform(0.3, 0.7)))
    if kind == "stairs_tumble":
        for _ in range(int(rng.integers(2, 4))):
            parts.append(_motion(rng, rng.uniform(0.4, 0.9), _rotate_towards(g0, g1, rng.uniform(0.3, 0.9)), 0.8, 0.5))
            parts.append(_impact(rng, g1, rng.uniform(2.2, 4.5), _random_direction(rng)))
    settle = _motion(rng, 0.5, g1, 0.5, 0.25, noise=0.08)
    parts.append(settle)
    if kind == "fall_then_moves":
        parts.append(_motion(rng, 6.0, g1, rng.uniform(0.25, 0.6), rng.uniform(0.7, 1.6)))
        parts.append(_still(rng, 20.0, g1, noise=0.03))
    elif kind == "fall_wrist_under_body":
        # the wrist is pinned: gravity reads along one axis, almost no noise, slightly over 1 g
        parts.append(_still(rng, 25.0, g1 * 1.03, noise=0.01))
    else:
        parts.append(_still(rng, 25.0, g1, noise=rng.uniform(0.015, 0.05)))
    xyz = _clip(np.vstack(parts))
    return xyz, {"peak_g": round(peak, 2), "orientation_change_deg": round(turn_deg, 1), "kind": kind}


# ── activities that provoke impact detectors ─────────────────────────────────

def not_fall(rng, kind: str) -> tuple[np.ndarray, dict]:
    g0 = _random_direction(rng)
    parts = [_still(rng, 2.0, g0)]
    info = {"kind": kind}
    if kind in ("clapping", "knocking", "hammering"):
        # bursts of sharp impacts, 2 to 6 per second, the wrist returning to the same orientation
        rate = {"clapping": rng.uniform(2.5, 4.5), "knocking": rng.uniform(2.0, 4.0), "hammering": rng.uniform(1.5, 3.0)}[kind]
        peak = {"clapping": rng.uniform(2.2, 4.0), "knocking": rng.uniform(2.5, 5.0), "hammering": rng.uniform(3.0, 8.0)}[kind]
        seconds = rng.uniform(3.0, 8.0)
        n_hits = max(3, int(seconds * rate))
        d = _random_direction(rng)
        for _ in range(n_hits):
            gap = _motion(rng, max(0.05, 1.0 / rate - 0.3), g0, rng.uniform(0.3, 0.8), 0.4)
            parts.append(gap)
            parts.append(_impact(rng, g0, peak * rng.uniform(0.75, 1.1), d + rng.normal(0, 0.15, 3)))
        parts.append(_motion(rng, 4.0, g0, 0.3, 1.2))
        parts.append(_still(rng, 10.0, g0))
        info.update(peak_g=round(peak, 2), hits=n_hits, rate_hz=round(rate, 2))
    elif kind == "table_slap":
        parts.append(_motion(rng, 0.5, g0, 0.6, 0.5))
        parts.append(_impact(rng, g0, rng.uniform(3.0, 6.0), _random_direction(rng)))
        parts.append(_motion(rng, 5.0, g0, rng.uniform(0.3, 0.6), 1.0))    # the hand keeps going
        parts.append(_still(rng, 10.0, g0))
    elif kind == "drop_into_chair":
        # body drops, wrist orientation changes a little (arm on the armrest), then seated fidgeting
        g1 = _rotate_towards(g0, _random_direction(rng), rng.uniform(0.05, 0.25))
        n_ff = int(0.15 * HZ)
        parts.append(g0[None, :] * np.linspace(1.0, 0.6, n_ff)[:, None] + rng.normal(0, 0.05, (n_ff, 3)))
        parts.append(_impact(rng, g1, rng.uniform(2.2, 3.5), g1))
        parts.append(_motion(rng, 8.0, g1, rng.uniform(0.2, 0.45), rng.uniform(1.0, 2.5)))
        parts.append(_still(rng, 10.0, g1, noise=0.04))
    elif kind == "brushing_teeth":
        parts.append(_motion(rng, 20.0, g0, rng.uniform(1.2, 2.4), rng.uniform(0.15, 0.3), noise=0.1))
        parts.append(_still(rng, 5.0, g0))
    elif kind == "watch_put_on_table":
        # taken off (orientation changes), a sharp tap as it lands, then perfectly still: off-wrist
        g1 = _random_direction(rng)
        parts.append(_motion(rng, 1.0, _rotate_towards(g0, g1, 0.5), 0.5, 0.8))
        parts.append(_impact(rng, g1, rng.uniform(2.2, 4.0), g1))
        parts.append(_still(rng, 25.0, g1, noise=0.004))
        info.update(off_wrist_after_s=3.3)
    elif kind == "wrist_shake":
        parts.append(_motion(rng, rng.uniform(2.0, 5.0), g0, rng.uniform(1.6, 3.2), rng.uniform(0.12, 0.25), noise=0.15))
        parts.append(_motion(rng, 4.0, g0, 0.3, 1.0))
        parts.append(_still(rng, 10.0, g0))
    elif kind == "washing_hands":
        for _ in range(int(rng.integers(3, 7))):
            parts.append(_motion(rng, rng.uniform(1.0, 2.5), g0, rng.uniform(0.6, 1.4), rng.uniform(0.3, 0.6), noise=0.08))
            parts.append(_impact(rng, g0, rng.uniform(2.0, 2.8), _random_direction(rng), width_samples=2, bounce=0.2))
        parts.append(_still(rng, 10.0, g0))
    elif kind == "stairs_down":
        steps = int(rng.integers(8, 16))
        for _ in range(steps):
            parts.append(_motion(rng, rng.uniform(0.45, 0.7), g0, rng.uniform(0.5, 0.9), 0.5, noise=0.08))
            parts.append(_impact(rng, g0, rng.uniform(1.8, 2.6), g0, width_samples=2, bounce=0.3))
        parts.append(_motion(rng, 3.0, g0, 0.4, 1.0))
        parts.append(_still(rng, 10.0, g0))
    elif kind == "clipping_hit":
        # a very hard knock that saturates the sensor, then the hand carries on
        parts.append(_motion(rng, 0.5, g0, 0.7, 0.5))
        parts.append(_impact(rng, g0, rng.uniform(17.0, 30.0), _random_direction(rng), width_samples=3))
        parts.append(_motion(rng, 5.0, g0, rng.uniform(0.3, 0.7), 0.9))
        parts.append(_still(rng, 10.0, g0))
    xyz = _clip(np.vstack(parts))
    return xyz, info


def generate(seed: int, per_kind: int) -> list[dict]:
    rng = np.random.default_rng(seed)
    out = []
    for kind in FALL_KINDS:
        for i in range(per_kind):
            xyz, info = fall(rng, kind)
            out.append({"id": f"fall_{kind}_{i:03d}", "label": "fall", "kind": kind, "params": info, "xyz": xyz})
    for kind in NOT_FALL_KINDS:
        for i in range(per_kind):
            xyz, info = not_fall(rng, kind)
            out.append({"id": f"notfall_{kind}_{i:03d}", "label": "not_fall", "kind": kind, "params": info, "xyz": xyz})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--per-kind", type=int, default=20)
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    scenarios = generate(a.seed, a.per_kind)
    index = []
    for s in scenarios:
        np.save(a.out / f"{s['id']}.npy", s["xyz"].astype(np.float32))
        index.append({k: v for k, v in s.items() if k != "xyz"} | {"samples": int(len(s["xyz"])), "seconds": round(len(s["xyz"]) / HZ, 1)})
    (a.out / "index.json").write_text(json.dumps({"seed": a.seed, "per_kind": a.per_kind, "hz": HZ, "scenarios": index}, indent=1) + "\n")
    print(f"{len(scenarios)} scenarios ({len(FALL_KINDS)} fall kinds, {len(NOT_FALL_KINDS)} other kinds) in {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
