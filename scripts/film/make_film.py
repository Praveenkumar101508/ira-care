#!/usr/bin/env python3
"""Assemble the IRA CARE film (round 4, phase 3) at 1920 x 1080.

    scripts/film/make_film.sh        # the same, with the defaults below

A title card, the 20-second Blender turntable, the recorded scenes (scripts/film/record_scenes.mjs)
with smooth cross-fades between them, the hardware renders and a closing card. The voice-over is
the table in docs/film/NARRATION.md spoken by Piper (MIT) with the en_GB-cori-high voice. What IRA
says on the watch is IRA's own words from the recording (caught from the watch page's speech call),
spoken by the same open voice a little higher, and never overlapping the voice-over. Every spoken
line is captioned; IRA's lines are marked "IRA". The film must stay under 60 MB.

Inputs: docs/film/NARRATION.md, docs/film/raw/NN-*.webm and NN-*.json, docs/hardware/turntable.mp4,
docs/hardware/renders/*.png. Needs ffmpeg with libass and the piper command (PIPER) with PIPER_VOICE.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
os.chdir(ROOT)
PIPER = os.environ.get("PIPER", "piper")
VOICE = os.environ.get("PIPER_VOICE") or sys.exit("set PIPER_VOICE to the en_GB-cori-high.onnx file")
RAW = Path(os.environ.get("FILM_RAW", "docs/film/raw"))
WORK = Path(os.environ.get("FILM_WORK", str(RAW / "work")))
OUT = Path(os.environ.get("FILM_OUT", "docs/film/ira_care.mp4"))
TURNTABLE = Path(os.environ.get("FILM_TURNTABLE", "docs/hardware/turntable.mp4"))
RENDERS = Path(os.environ.get("FILM_RENDERS", "docs/hardware/renders"))
FONT = os.environ.get("FILM_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
W, H, FPS = 1920, 1080, 30
FADE = 0.6            # cross-fade between scenes, seconds
LEAD = 0.8            # the voice-over starts this long into a scene
GAP = 0.35            # between two spoken sentences
MAX_SCENE = 80.0      # a recorded scene is cut at this length
PAPER, INK, MUTED, CORAL = "0xFAF8F4", "0x1F2A24", "0x5F6B66", "0xE2574C"
MAX_BYTES = 60 * 1024 * 1024
WORK.mkdir(parents=True, exist_ok=True)


def run(*cmd: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", *cmd], check=True)


def dur(path: Path) -> float:
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], text=True).strip())


def speak(text: str, out: Path, ira: bool = False) -> Path:
    """One sentence as a 48 kHz mono WAV; IRA's lines a little higher and quicker than the narrator."""
    if out.exists() and out.stat().st_size > 0:
        return out
    tmp = out.with_suffix(".raw.wav")
    subprocess.run([PIPER, "-m", VOICE, "-f", str(tmp), "--length-scale", "0.98" if ira else "1.05"], input=text, text=True, check=True, capture_output=True)
    shift = "asetrate=22050*1.07,aresample=48000,atempo=0.95" if ira else "aresample=48000"
    run("-i", str(tmp), "-af", shift + ",volume=1.0", "-ac", "1", str(out))
    tmp.unlink()
    return out


def sentences(words: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", words.strip()) if s.strip()]


def narration_rows() -> list[dict]:
    rows = []
    for line in Path("docs/film/NARRATION.md").read_text().splitlines():
        m = re.match(r"\| (\d+) \| ([^|]+)\| ([^|]+)\| (.+) \|$", line)
        if m:
            rows.append({"n": int(m.group(1)), "scene": m.group(2).strip(), "seconds": float(m.group(3)), "words": m.group(4).strip()})
    return rows


def plan_audio(n: int, words: str, said: list[dict]) -> tuple[list[dict], float]:
    """Place IRA's lines where they happened and the narrator's sentences in the gaps between them."""
    items = []
    busy = []
    # The watch can ask the same question twice in a moment (two polls); it is heard once.
    said = [s for k, s in enumerate(said) if not any(p["text"] == s["text"] and s["t"] - p["t"] < 5 for p in said[:k])]
    for i, s in enumerate(said):
        wav = speak(s["text"], WORK / f"ira{n:02d}_{i:02d}.wav", ira=True)
        start = max(0.0, s["t"])
        if busy and start < busy[-1][1] + 0.2:
            start = busy[-1][1] + 0.2
        end = start + dur(wav)
        busy.append((start, end))
        items.append({"wav": wav, "start": start, "end": end, "text": s["text"], "ira": True})
    t = LEAD
    for i, s in enumerate(sentences(words)):
        wav = speak(s, WORK / f"n{n:02d}_{i:02d}.wav")
        d = dur(wav)
        moved = True
        while moved:
            moved = False
            for b0, b1 in busy:
                if t < b1 + 0.4 and t + d > b0 - 0.4:
                    t = b1 + 0.5
                    moved = True
        items.append({"wav": wav, "start": t, "end": t + d, "text": s, "ira": False})
        busy.append((t, t + d))
        busy.sort()
        t += d + GAP
    end = max((i["end"] for i in items), default=0.0)
    return items, end


def mix(items: list[dict], length: float, out: Path) -> None:
    """All of a scene's speech on one track of exactly `length` seconds."""
    if not items:
        run("-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", f"{length:.3f}", "-c:a", "pcm_s16le", str(out))
        return
    args, parts = [], []
    for k, it in enumerate(items):
        args += ["-i", str(it["wav"])]
        ms = int(it["start"] * 1000)
        parts.append(f"[{k}:a]adelay={ms}|{ms}[a{k}]")
    graph = ";".join(parts) + ";" + "".join(f"[a{k}]" for k in range(len(items))) + f"amix=inputs={len(items)}:normalize=0,apad,atrim=0:{length:.3f}[out]"
    run(*args, "-filter_complex", graph, "-map", "[out]", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(out))


def esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’").replace("%", "\\%")


def card(out: Path, seconds: float, big: str, small: list[str]) -> None:
    vf = [f"drawtext=fontfile={FONT}:text='{esc(big)}':fontcolor={INK}:fontsize=120:x=(w-text_w)/2:y=h/2-190"]
    for i, line in enumerate(small):
        vf.append(f"drawtext=fontfile={FONT}:text='{esc(line)}':fontcolor={MUTED}:fontsize=44:x=(w-text_w)/2:y=h/2+{10 + i * 70}")
    vf.append(f"drawbox=x=(iw-160)/2:y=ih/2-30:w=160:h=8:color={CORAL}:t=fill")
    run("-f", "lavfi", "-i", f"color=c={PAPER}:s={W}x{H}:r={FPS}:d={seconds:.3f}", "-vf", ",".join(vf + ["format=yuv420p"]), "-c:v", "libx264", "-preset", "medium", "-crf", "20", str(out))


def fit(extra: str = "") -> str:
    return f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={PAPER}{extra}"


def video_for(row: dict, length: float) -> Path:
    n = row["n"]
    out = WORK / f"v{n:02d}.mp4"
    enc = ["-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-an", "-t", f"{length:.3f}"]
    rec = sorted(p for p in RAW.glob(f"{n:02d}-*.webm") if not p.stem.endswith("-phone"))
    if n == 1:
        card(out, length, "IRA CARE", ["A watch for someone living alone,", "and a care team around them."])
    elif row["scene"].lower().startswith("the watch, turning"):
        run("-stream_loop", "-1", "-i", str(TURNTABLE), "-vf", fit(",format=yuv420p"), *enc, str(out))
    elif row["scene"].lower().startswith("the hardware"):
        pngs = [RENDERS / f"{k}.png" for k in ("exploded", "hero", "front", "side", "section", "wrist_dock") if (RENDERS / f"{k}.png").exists()]
        per = length / len(pngs)
        args, chains = [], []
        for k, p in enumerate(pngs):
            args += ["-loop", "1", "-t", f"{per:.3f}", "-i", str(p)]
            frames = int(per * FPS)
            chains.append(f"[{k}:v]scale=1440:1080,zoompan=z='1+0.06*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1440x1080:fps={FPS},pad={W}:{H}:(ow-iw)/2:0:color={PAPER},setsar=1[p{k}]")
        graph = ";".join(chains) + ";" + "".join(f"[p{k}]" for k in range(len(pngs))) + f"concat=n={len(pngs)}:v=1:a=0,format=yuv420p[v]"
        run(*args, "-filter_complex", graph, "-map", "[v]", *enc, str(out))
    elif row["scene"].lower().startswith("closing"):
        card(out, length, "IRA CARE", ["Built by Praveen Kamineti and Sravani Perni.", "Open source: github.com/Praveenkumar101508/ira-care"])
    elif rec:
        phone = RAW / f"{rec[0].stem}-phone.webm"
        if phone.exists():
            off = float((RAW / f"{rec[0].stem}-phone.offset").read_text())
            graph = (f"[0:v]scale=-2:{H - 120},tpad=stop_mode=clone:stop_duration=120[w];"
                     f"[1:v]scale=-2:{H - 120},tpad=start_mode=clone:start_duration={off:.2f}:stop_mode=clone:stop_duration=120[p];"
                     f"color=c={PAPER}:s={W}x{H}:r={FPS}[bg];[bg][w]overlay=x=150:y=60:shortest=0[a];[a][p]overlay=x=W-w-190:y=60,format=yuv420p[v]")
            run("-i", str(rec[0]), "-i", str(phone), "-filter_complex", graph, "-map", "[v]", *enc, str(out))
        else:
            run("-ss", f"{row.get('start', 0.0):.2f}", "-i", str(rec[0]), "-vf", fit(",tpad=stop_mode=clone:stop_duration=120,format=yuv420p"), *enc, str(out))
    else:
        card(out, length, row["scene"], ["recording missing"])
    return out


def ass_time(t: float) -> str:
    cs = round(t * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def main() -> int:
    rows = narration_rows()
    scenes = []
    for row in rows:
        n = row["n"]
        rec = sorted(p for p in RAW.glob(f"{n:02d}-*.webm") if not p.stem.endswith("-phone"))
        meta = json.loads(rec[0].with_suffix(".json").read_text()) if rec and rec[0].with_suffix(".json").exists() else {}
        # The film's cut starts at the scene's 'start' mark, if it has one.
        start = next((m["t"] for m in meta.get("marks", []) if m["what"] == "start"), 0.0)
        said = [{**s, "t": s["t"] - start} for s in meta.get("said", []) if s["t"] >= start]
        row["start"] = start
        items, speech_end = plan_audio(n, row["words"], said)
        length = max(row["seconds"], speech_end + 1.2)
        if rec:
            length = max(length, min(MAX_SCENE, dur(rec[0]) - start))
        if row["scene"].lower().startswith("the watch, turning"):
            length = max(length, dur(TURNTABLE))
        length = round(length, 2)
        video = video_for(row, length)
        audio = WORK / f"a{n:02d}.wav"
        mix(items, length, audio)
        scenes.append({"n": n, "scene": row["scene"], "length": length, "video": video, "audio": audio, "items": items})
        print(f"scene {n:2d} {row['scene'][:40]:40s} {length:6.1f} s  {sum(1 for i in items if i['ira'])} IRA lines")

    # One pass: cross-fade every scene into the next, video and sound together, and burn in the captions.
    starts, t = [], 0.0
    for k, s in enumerate(scenes):
        starts.append(t)
        t += s["length"] - (FADE if k < len(scenes) - 1 else 0)
    total = t
    ass = WORK / "captions.ass"
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
             "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
             "Style: Narr,DejaVu Sans,40,&H00FFFFFF,&H00FFFFFF,&H00242A1F,&H40242A1F,0,0,0,0,100,100,0,0,3,14,0,2,220,220,40,1",
             "Style: Ira,DejaVu Sans,40,&H00FFFFFF,&H00FFFFFF,&H004C57E2,&H404C57E2,1,0,0,0,100,100,0,0,3,14,0,8,220,220,40,1",
             "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for k, s in enumerate(scenes):
        for it in s["items"]:
            a, b = starts[k] + it["start"], starts[k] + it["end"] + 0.25
            text = ("IRA: " if it["ira"] else "") + it["text"].replace("\n", " ")
            lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},{'Ira' if it['ira'] else 'Narr'},,0,0,0,,{text}")
    ass.write_text("\n".join(lines) + "\n")

    args = []
    for s in scenes:
        args += ["-i", str(s["video"]), "-i", str(s["audio"])]
    parts, v, a = [], "[0:v]", "[1:a]"
    for k in range(1, len(scenes)):
        off = starts[k]
        parts.append(f"{v}[{2 * k}:v]xfade=transition=fade:duration={FADE}:offset={off:.3f}[v{k}]")
        parts.append(f"{a}[{2 * k + 1}:a]acrossfade=d={FADE}:c1=tri:c2=tri[a{k}]")
        v, a = f"[v{k}]", f"[a{k}]"
    parts.append(f"{v}ass={ass}:fontsdir=/usr/share/fonts/truetype/dejavu,format=yuv420p[vout]")
    parts.append(f"{a}loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[aout]")
    run(*args, "-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "slow", "-crf", "24",
        "-r", str(FPS), "-c:a", "aac", "-b:a", "96k", "-ac", "1", "-movflags", "+faststart", str(OUT))
    size = OUT.stat().st_size
    print(f"{OUT}: {size / 1048576:.1f} MB, {dur(OUT):.1f} s (planned {total:.1f} s)")
    (WORK / "timeline.json").write_text(json.dumps([{"n": s["n"], "scene": s["scene"], "start": round(starts[k], 2), "length": s["length"],
        "ira_lines": [i["text"] for i in s["items"] if i["ira"]]} for k, s in enumerate(scenes)], indent=1))
    if size >= MAX_BYTES:
        print("over 60 MB")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
