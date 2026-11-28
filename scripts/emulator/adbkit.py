"""Pure helpers for the phone emulator journey (phone_smoke.py): finding nodes in a
`uiautomator dump`, escaping text for `adb shell input text`, reading JWT claims, the labels the
app draws for readings, pixels of a screenshot, and the checks each journey step makes.

Nothing here talks to adb or the network, so every function has a unit test
(supracloud-jarvis/ira/tests/readiness/test_phone_journey_helpers.py). watch_smoke.py keeps its own
helpers; this module is used by phone_smoke.py only.
"""
from __future__ import annotations

import base64
import json
import struct
import zlib

#: The words HomeTab shows in place of "No open alerts." while the server cannot be reached (G2.i).
STALE_WORDS = "Cannot reach the care server. Alerts may be out of date."
ALL_WELL_WORDS = "No open alerts."
#: Home-screen test ids that only the family experience draws (mobile/src/model/experience.ts).
FAMILY_ONLY_IDS = ("card-person", "card-updates", "card-watch", "home-care")
#: Only the wearer is offered Talk to IRA (C-7.b): never the family member or the carer.
WEARER_ONLY_IDS = ("home-talk",)
#: What the carer's home screen must show.
CARER_IDS = ("card-visits", "card-people", "card-handover")


# ── uiautomator nodes ───────────────────────────────────────────────────────

def find_id(nodes: list[dict], rid: str) -> dict | None:
    """The node whose resource-id is this React Native testID (bare or package-qualified)."""
    for n in nodes:
        r = n.get("resource-id", "")
        if r == rid or r.endswith(":id/" + rid) or r.endswith("/" + rid):
            return n
    return None


def has_text(nodes: list[dict], text: str) -> bool:
    """True when some node's text or description is, starts with or contains the text."""
    return any(text in n.get("text", "") or text in n.get("content-desc", "") for n in nodes)


def texts_starting(nodes: list[dict], prefix: str) -> list[str]:
    return [n["text"] for n in nodes if n.get("text", "").startswith(prefix)]


def is_selected(node: dict | None) -> bool:
    return node is not None and node.get("selected") == "true"


# ── adb input ───────────────────────────────────────────────────────────────

_SHELL_SPECIAL = set("\\\"'`$&|;<>()[]{}*?~#!")


def adb_text(s: str) -> str:
    """Escape text for `adb shell input text`: spaces become %s and shell specials are
    backslash-escaped, since the argument passes through the device's shell."""
    out = []
    for ch in s:
        if ch == " ":
            out.append("%s")
        elif ch in _SHELL_SPECIAL:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


# ── tokens ──────────────────────────────────────────────────────────────────

def jwt_claims(token: str) -> dict:
    """The claims of a JWT, read without verifying it (the smoke only needs its own token's jti)."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("not a JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def app_sessions(sessions: list[dict], own_agent: str) -> list[dict]:
    """The sessions that are not the smoke's own: the phone app's, told apart by user agent."""
    return [s for s in sessions if (s.get("user_agent") or "") != own_agent]


# ── what the app draws ──────────────────────────────────────────────────────

def reading_label(reading: dict) -> str:
    """The label PersonScreen draws for one reading (mobile/src/model/format.ts readingLabel)."""
    kind, value = reading.get("kind"), float(reading.get("value", 0))
    if kind == "heart_rate":
        return f"Heart rate {round(value)} bpm"
    if kind == "steps":
        return f"{round(value)} steps"
    if kind == "movement":
        return f"Movement {round(value)}"
    if kind == "on_wrist":
        return "Watch on the wrist" if value >= 0.5 else "Watch off the wrist"
    if kind == "battery":
        return f"Battery {round(value)} %"
    return f"{kind} {reading.get('value')}"


def medication_line(m: dict) -> str:
    """One medicine as PersonScreen's Plan tab draws it (medicationLine)."""
    times = ", ".join(str(t)[:5] for t in m.get("times") or [])
    return f"{m['name']}{' · ' + m['dose'] if m.get('dose') else ''}{' · ' + times if times else ''}"


# ── step checks ─────────────────────────────────────────────────────────────

def person_record_problems(nodes: list[dict], name: str, status: dict) -> list[str]:
    """The person record must be titled with the person's name and list every latest reading
    the API reports, with the label the app draws."""
    problems = []
    if not any(n.get("text") == name for n in nodes):
        problems.append(f"title {name!r} not shown")
    readings = status.get("latest_readings") or []
    if not readings:
        problems.append("the API reports no latest readings to compare with")
    for r in readings:
        label = reading_label(r)
        if not texts_starting(nodes, label):
            problems.append(f"reading {label!r} not shown")
    return problems


def plan_problems(nodes: list[dict], medications: list[dict], plan: dict | None) -> list[str]:
    problems = []
    active = [m for m in medications if m.get("active", True)]
    if not active:
        problems.append("the API reports no active medication to compare with")
    for m in active:
        if not has_text(nodes, medication_line(m)):
            problems.append(f"medication {medication_line(m)!r} not shown")
    if plan is None:
        problems.append("the API reports no care plan to compare with")
    elif find_id(nodes, "care-plan-card") is None:
        problems.append("no care plan card")
    elif plan.get("goals") and not has_text(nodes, f"Goals: {plan['goals']}"):
        problems.append("the care plan's goals are not shown")
    return problems


def offline_problems(nodes: list[dict]) -> list[str]:
    """Offline, the home screen must say it may be out of date and never say all is well."""
    problems = []
    if not has_text(nodes, STALE_WORDS):
        problems.append("no out-of-date notice")
    if has_text(nodes, ALL_WELL_WORDS):
        problems.append(f"{ALL_WELL_WORDS!r} shown while offline")
    return problems


def recovered_problems(nodes: list[dict]) -> list[str]:
    problems = []
    if has_text(nodes, STALE_WORDS):
        problems.append("still says out of date")
    if not has_text(nodes, ALL_WELL_WORDS):
        problems.append(f"{ALL_WELL_WORDS!r} not back")
    return problems


def carer_home_problems(nodes: list[dict], family_words: tuple[str, ...] = ()) -> list[str]:
    """The carer's home: Today, the carer's cards, nothing of the family experience and none of
    the words that belonged to the previous account."""
    problems = []
    if not any(n.get("text") == "Today" for n in nodes):
        problems.append("no 'Today' title")
    for rid in CARER_IDS:
        if find_id(nodes, rid) is None:
            problems.append(f"no {rid}")
    for rid in FAMILY_ONLY_IDS:
        if find_id(nodes, rid) is not None:
            problems.append(f"family-only {rid} shown to the carer")
    for rid in WEARER_ONLY_IDS:
        if find_id(nodes, rid) is not None:
            problems.append(f"wearer-only {rid} shown to the carer")
    for w in family_words:
        if has_text(nodes, w):
            problems.append(f"previous account's words shown: {w!r}")
    return problems


def nothing_revealed_problems(nodes: list[dict], private_words: tuple[str, ...]) -> list[str]:
    """After sign-out and Back: nothing of the account is on the screen."""
    problems = [f"private words shown: {w!r}" for w in private_words if has_text(nodes, w)]
    for rid in ("tab-home", "tab-care", "tab-alerts", "card-alerts", "card-person", "card-help"):
        if find_id(nodes, rid) is not None:
            problems.append(f"signed-in element {rid} shown")
    return problems


def revocation_problems(before: list[dict], after: list[dict]) -> list[str]:
    """Every session the app held before sign-out must be revoked after it, and there must have
    been one; a session that was already revoked before proves nothing."""
    problems = []
    live_before = [s for s in before if not s.get("revoked_at")]
    if not live_before:
        problems.append("no live app session before sign-out")
    ids = {s["id"] for s in live_before}
    for s in after:
        if s["id"] in ids and not s.get("revoked_at"):
            problems.append(f"session {s['id']} still live after sign-out")
    return problems


# ── screenshots ─────────────────────────────────────────────────────────────

def png_pixel(data: bytes, x: int, y: int) -> tuple[int, int, int]:
    """The RGB of one pixel of an 8-bit RGB or RGBA, non-interlaced PNG (what screencap writes)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, width, height, channels = 8, [], 0, 0, 0
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, colour, _c, _f, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or colour not in (2, 6) or interlace != 0:
                raise ValueError(f"unsupported PNG: depth {depth}, colour type {colour}, interlace {interlace}")
            channels = 3 if colour == 2 else 4
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + length
    if not (0 <= x < width and 0 <= y < height):
        raise ValueError("pixel outside the image")
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    prev = bytearray(stride)
    line = bytearray(stride)
    for row in range(y + 1):
        start = row * (stride + 1)
        ftype = raw[start]
        cur = bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride):
            a = cur[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if ftype == 1:
                cur[i] = (cur[i] + a) & 255
            elif ftype == 2:
                cur[i] = (cur[i] + b) & 255
            elif ftype == 3:
                cur[i] = (cur[i] + ((a + b) >> 1)) & 255
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                cur[i] = (cur[i] + pred) & 255
        line, prev = cur, cur
    i = x * channels
    return (line[i], line[i + 1], line[i + 2])


def is_dark(rgb: tuple[int, int, int]) -> bool:
    """Dark by relative luminance (below 0.25): tells a dark wallpaper from the default cream."""
    def lin(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b) < 0.25
