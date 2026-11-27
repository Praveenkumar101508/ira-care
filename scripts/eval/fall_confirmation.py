"""The fall confirmation stage, in Python, line for line with the watch's `FallConfirmation.kt`.

After a detector (the rule's still window or the v3 model) raises a suspected fall, the watch
looks at the seconds around the impact before it starts asking the person: a fall changes the
direction gravity reads on the wrist and leaves the arm quiet or stirring; a hand impact (a clap,
a knock, a slap on the table) leaves the wrist pointing the same way and the hand carrying on.
The stage never suppresses a candidate whose wrist is still afterwards, whatever the orientation
did: a person lying still is always asked.

Inputs: the samples from PRE_FROM_S before the impact to POST_TO_S after it (50 Hz, g). The stage
is ready once a sample at or past POST_TO_S after the impact has arrived.

Decision (the watch and this module must agree exactly; `tests/readiness/test_fall_confirmation.py`
pins the constants and the decisions on the synthetic scenarios):

  post_motion  = standard deviation of |a| over the post window
  orientation  = angle between the mean gravity vector before and after the impact
  impacts      = local maxima of |a| at or above IMPACT_G within ±REPEAT_WINDOW_S of the impact,
                 at least REPEAT_GAP_S apart
  clipped      = any axis at or beyond CLIP_G anywhere in the window (flag only)

  if post_motion < STILL_SD_G                                     -> CONFIRM  "still afterwards"
  elif impacts >= REPEAT_MIN and orientation < REPEAT_TURN_DEG    -> SUPPRESS "repeated impacts, same orientation, moving"
  elif orientation < SAME_TURN_DEG and post_motion > BUSY_SD_G    -> SUPPRESS "same orientation, hand carried on"
  else                                                            -> CONFIRM  "orientation changed or arm not busy"
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

HZ = 50
PRE_FROM_S, PRE_TO_S = 2.0, 0.5          # the "before" window: 2.0 s to 0.5 s before the impact
POST_FROM_S, POST_TO_S = 1.5, 4.0        # the "after" window: 1.5 s to 4.0 s after the impact
STILL_SD_G = 0.15                        # below this the wrist is still afterwards: always ask
BUSY_SD_G = 0.35                         # above this the hand carried on moving
SAME_TURN_DEG = 20.0                     # the wrist kept its orientation
REPEAT_TURN_DEG = 30.0                   # same, for a burst of impacts
REPEAT_MIN = 3                           # impacts in the burst window that make it repetitive
REPEAT_WINDOW_S = 3.0
REPEAT_GAP_S = 0.15
IMPACT_G = 2.0                           # what counts as an impact in the burst count (the model's candidate)
CLIP_G = 15.5                            # at or beyond this an axis is saturated (range ±16 g)


@dataclass(frozen=True)
class Decision:
    confirm: bool
    reason: str
    orientation_change_deg: float
    post_motion_sd_g: float
    impacts_in_window: int
    clipped: bool


def _window(t_s: np.ndarray, xyz: np.ndarray, start: float, end: float) -> np.ndarray:
    m = (t_s >= start) & (t_s <= end)
    return xyz[m]


def orientation_change_deg(pre: np.ndarray, post: np.ndarray) -> float:
    if len(pre) == 0 or len(post) == 0:
        return 0.0
    a, b = pre.mean(0), post.mean(0)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-6 or nb < 1e-6:
        return 0.0
    return math.degrees(math.acos(float(np.clip(a @ b / (na * nb), -1.0, 1.0))))


def count_impacts(t_s: np.ndarray, mag: np.ndarray, impact_s: float) -> int:
    m = (t_s >= impact_s - REPEAT_WINDOW_S) & (t_s <= impact_s + REPEAT_WINDOW_S)
    t, g = t_s[m], mag[m]
    count, last = 0, -math.inf
    for i in range(len(g)):
        if g[i] < IMPACT_G:
            continue
        left = g[i - 1] if i > 0 else -math.inf
        right = g[i + 1] if i + 1 < len(g) else -math.inf
        if g[i] >= left and g[i] > right and t[i] - last >= REPEAT_GAP_S:
            count += 1
            last = t[i]
    return count


def decide(t_s: np.ndarray, xyz: np.ndarray, impact_s: float) -> Decision:
    """`t_s`: sample times in seconds; `xyz`: (n, 3) in g; `impact_s`: the impact's time."""
    mag = np.linalg.norm(xyz, axis=1)
    pre = _window(t_s, xyz, impact_s - PRE_FROM_S, impact_s - PRE_TO_S)
    post = _window(t_s, xyz, impact_s + POST_FROM_S, impact_s + POST_TO_S)
    post_sd = float(np.std(np.linalg.norm(post, axis=1))) if len(post) else 0.0
    turn = orientation_change_deg(pre, post)
    impacts = count_impacts(t_s, mag, impact_s)
    clipped = bool(np.any(np.abs(_window(t_s, xyz, impact_s - PRE_FROM_S, impact_s + POST_TO_S)) >= CLIP_G))
    if post_sd < STILL_SD_G:
        return Decision(True, "still afterwards", turn, post_sd, impacts, clipped)
    if impacts >= REPEAT_MIN and turn < REPEAT_TURN_DEG:
        return Decision(False, "repeated impacts, same orientation, moving", turn, post_sd, impacts, clipped)
    if turn < SAME_TURN_DEG and post_sd > BUSY_SD_G:
        return Decision(False, "same orientation, hand carried on", turn, post_sd, impacts, clipped)
    return Decision(True, "orientation changed or arm not busy", turn, post_sd, impacts, clipped)


def ready(latest_s: float, impact_s: float) -> bool:
    """Whether enough has been seen after the impact to decide."""
    return latest_s >= impact_s + POST_TO_S
