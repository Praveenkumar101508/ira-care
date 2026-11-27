#!/usr/bin/env python3
"""Produce IRA CARE OS's bootanimation.zip from its specification (requirement E2.f).

The specification is os/device/ira/ira_watch_cf/bootanimation/: desc.txt (the control file,
copied verbatim) and README.md (what the person sees). This renders it deterministically:

  part0/  30 frames, one second: the mark fades in on black, ease-out.
  part1/  60 frames, two seconds: the mark pulses, scale 1.00 to 1.04 and back, seamless.

The mark is a placeholder until the real artwork exists: a coral ring (#E2574C, the Help
button's colour, D-072) with a coral dot, centred inside a 320-pixel circle so it stays within a
round panel's safe area. No text. Frames are 8-bit RGB PNG named 00000.png onward, and every zip
entry is stored, not deflated, as the platform reads them in place.

Run on the build host by os/scripts/build_cuttlefish.sh into the AOSP checkout; never committed
(no binary assets in the repository). Same input, same bytes: the zip's timestamps are fixed.

    python3 os/scripts/make_bootanimation.py OUTPUT.zip [--desc PATH]

Needs Pillow. Nothing produced here has been shown at boot: that needs a built image.
"""
from __future__ import annotations

import argparse
import io
import math
import re
import sys
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

SPEC = Path(__file__).resolve().parents[1] / "device/ira/ira_watch_cf/bootanimation/desc.txt"
CORAL = (0xE2, 0x57, 0x4C)
FIXED_TIME = (2026, 1, 1, 0, 0, 0)
SUPERSAMPLE = 4


def parse_desc(text: str) -> tuple[int, int, int, list[tuple[str, int, int, str]]]:
    """desc.txt as frameworks/base/cmds/bootanimation/FORMAT.md defines it (the parts this
    animation uses: `WIDTH HEIGHT FPS`, then `TYPE COUNT PAUSE PATH` with TYPE c, p or f)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines or not re.fullmatch(r"\d+ \d+ \d+( \d+)?", lines[0]):
        raise SystemExit(f"desc.txt line 1 must be 'WIDTH HEIGHT FPS': {lines[:1]}")
    width, height, fps = (int(v) for v in lines[0].split()[:3])
    if fps <= 0 or width <= 0 or height <= 0:
        raise SystemExit("desc.txt: width, height and fps must be positive")
    parts = []
    for ln in lines[1:]:
        m = re.fullmatch(r"([cpf]) (\d+) (\d+) (part\d+)", ln)
        if not m:
            raise SystemExit(f"desc.txt part line must be 'TYPE COUNT PAUSE PATH' with TYPE c, p or f: {ln!r}")
        parts.append((m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)))
    if not parts:
        raise SystemExit("desc.txt names no parts")
    return width, height, fps, parts


def _mark(size: int, scale: float, alpha: float) -> Image.Image:
    """The mark on black at one scale and opacity, drawn large and reduced for smooth edges."""
    big = size * SUPERSAMPLE
    img = Image.new("RGB", (big, big), (0, 0, 0))
    d = ImageDraw.Draw(img)
    colour = tuple(round(c * alpha) for c in CORAL)
    c = big / 2
    ring_r = 120 * SUPERSAMPLE * scale            # inside the 160-pixel radius safe circle
    ring_w = max(1, round(22 * SUPERSAMPLE * scale))
    dot_r = 34 * SUPERSAMPLE * scale
    d.ellipse([c - ring_r, c - ring_r, c + ring_r, c + ring_r], outline=colour, width=ring_w)
    d.ellipse([c - dot_r, c - dot_r, c + dot_r, c + dot_r], fill=colour)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def frames(width: int, fps: int) -> dict[str, list[Image.Image]]:
    fade = [_mark(width, 1.0, 1 - (1 - i / (fps - 1)) ** 3) for i in range(fps)]          # ease-out
    n = 2 * fps
    pulse = [_mark(width, 1.0 + 0.04 * (1 - math.cos(2 * math.pi * i / n)) / 2, 1.0) for i in range(n)]
    return {"part0": fade, "part1": pulse}


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False, compress_level=6)
    return buf.getvalue()


def build(out: Path, desc_path: Path = SPEC) -> Path:
    desc = desc_path.read_text(encoding="utf-8")
    width, height, fps, parts = parse_desc(desc)
    if width != height:
        raise SystemExit("the watch panel is round: frames must be square")
    rendered = frames(width, fps)
    names = [p[3] for p in parts]
    if sorted(names) != sorted(rendered):
        raise SystemExit(f"desc.txt names parts {names}; this script renders {sorted(rendered)}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as z:
        info = zipfile.ZipInfo("desc.txt", FIXED_TIME)
        z.writestr(info, desc.encode("utf-8"), compress_type=zipfile.ZIP_STORED)
        for part in names:
            for i, img in enumerate(rendered[part]):
                info = zipfile.ZipInfo(f"{part}/{i:05d}.png", FIXED_TIME)
                z.writestr(info, _png(img), compress_type=zipfile.ZIP_STORED)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("output", type=Path)
    ap.add_argument("--desc", type=Path, default=SPEC)
    args = ap.parse_args(argv)
    path = build(args.output, args.desc)
    print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
