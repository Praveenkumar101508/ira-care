#!/usr/bin/env bash
# Assemble the IRA CARE film (round 3, phase 14): the Blender turntable as the opening,
# the recorded scenes, a hardware scene from the renders, and the narration spoken by
# Piper (MIT) with the en_GB-cori-high voice (public-domain training data).
#
#   scripts/film/make_film.sh            # writes docs/film/ira_care.mp4 (< 60 MB)
#
# Inputs: docs/film/NARRATION.md (the words), docs/film/raw/*.webm (scripts/film/record_scenes.mjs),
# docs/hardware/turntable.mp4 and docs/hardware/renders/*.png (scripts/hardware/build_watch.py).
# Needs ffmpeg and the piper binary (PIPER, default: piper on PATH) with PIPER_VOICE (the .onnx).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
PIPER="${PIPER:-piper}"; VOICE="${PIPER_VOICE:?set PIPER_VOICE to the en_GB-cori-high.onnx file}"
RAW="${FILM_RAW:-docs/film/raw}"; WORK="${FILM_WORK:-$RAW/work}"; OUT="${FILM_OUT:-docs/film/ira_care.mp4}"
TURNTABLE="${FILM_TURNTABLE:-docs/hardware/turntable.mp4}"; RENDERS="${FILM_RENDERS:-docs/hardware/renders}"
FONT="${FILM_FONT:-/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf}"
mkdir -p "$WORK"
W=1280; H=720; FPS=24
# ffmpeg must never read the loop's stdin (it would swallow the scene list).
ff() { ffmpeg -nostdin "$@"; }

# 1. The narration, one file per scene, from the table in NARRATION.md.
python3 - "$WORK" <<'PY'
import re, sys, pathlib, json
work = pathlib.Path(sys.argv[1])
rows = []
for l in pathlib.Path('docs/film/NARRATION.md').read_text().splitlines():
    m = re.match(r"\| (\d+) \| ([^|]+)\| ([^|]+)\| (.+) \|$", l)
    if m: rows.append({"n": int(m.group(1)), "scene": m.group(2).strip(), "seconds": int(m.group(3)), "words": m.group(4).strip()})
(work / "scenes.json").write_text(json.dumps(rows, indent=1))
for r in rows: (work / f"n{r['n']:02d}.txt").write_text(r["words"])
PY
for t in "$WORK"/n*.txt; do
  n="${t##*/n}"; n="${n%.txt}"
  [ -s "$WORK/n$n.wav" ] || "$PIPER" -m "$VOICE" -f "$WORK/n$n.wav" --length-scale 1.05 < "$t" >/dev/null 2>&1
done

# 2. Each scene as a 1280x720 clip whose length is max(planned seconds, narration + 1 s).
title() { # out, seconds, big text, small text
  ff -y -v error -f lavfi -i "color=c=0x14181B:s=${W}x${H}:r=$FPS:d=$2" -vf "drawtext=fontfile=$FONT:text='$3':fontcolor=white:fontsize=64:x=(w-text_w)/2:y=(h-text_h)/2-40,drawtext=fontfile=$FONT:text='$4':fontcolor=0xA6B3AC:fontsize=28:x=(w-text_w)/2:y=(h-text_h)/2+50" -c:v libx264 -pix_fmt yuv420p "$1"
}
dur() { ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$1"; }
clips=()
while IFS= read -r line; do
  n=$(echo "$line" | cut -d' ' -f1); planned=$(echo "$line" | cut -d' ' -f2)
  nar="$WORK/n$(printf %02d "$n").wav"; nlen=$(dur "$nar"); need=$(python3 -c "print(max(int($planned), int(float($nlen))+2))")
  clip="$WORK/c$(printf %02d "$n").mp4"
  # A recorded scene keeps what happened in it, up to a minute, never less than planned.
  rec=$(ls "$RAW"/$(printf %02d "$n")-*.webm 2>/dev/null | head -1 || true)
  if [ -n "$rec" ]; then rlen=$(dur "$rec"); need=$(python3 -c "print(int(min(60, max($need, float($rlen)))))"); fi
  case "$n" in
    1) # the turntable, looped to the scene's length, with the title over it
       ff -y -v error -stream_loop -1 -i "$TURNTABLE" -t "$need" -vf "scale=-2:$H,pad=$W:$H:ow-iw:0:color=0x14181B,drawtext=fontfile=$FONT:text='IRA CARE':fontcolor=white:fontsize=64:x=56:y=200,drawtext=fontfile=$FONT:text='A watch for someone':fontcolor=0xA6B3AC:fontsize=28:x=56:y=300,drawtext=fontfile=$FONT:text='living alone, and a':fontcolor=0xA6B3AC:fontsize=28:x=56:y=340,drawtext=fontfile=$FONT:text='care team around them.':fontcolor=0xA6B3AC:fontsize=28:x=56:y=380,format=yuv420p" -r $FPS -c:v libx264 -preset medium -crf 26 -an "$clip" ;;
    9) # the hardware: the renders, a few seconds each
       list="$WORK/renders.txt"; : > "$list"; per=$(python3 -c "import glob;print(max(3, $need/len(glob.glob('$RENDERS/*.png'))))")
       for png in "$RENDERS"/hero.png "$RENDERS"/front.png "$RENDERS"/side.png "$RENDERS"/back.png "$RENDERS"/exploded.png "$RENDERS"/section.png "$RENDERS"/wrist_dock.png; do [ -f "$png" ] && printf "file '%s'\nduration %s\n" "$(realpath "$png")" "$per" >> "$list"; done
       ff -y -v error -f concat -safe 0 -i "$list" -t "$need" -vf "scale=$W:$H:force_original_aspect_ratio=decrease,pad=$W:$H:(ow-iw)/2:(oh-ih)/2:color=0xFAF8F4,format=yuv420p" -r $FPS -c:v libx264 -preset medium -crf 26 -an "$clip" ;;
    *) src=$(ls "$RAW"/$(printf %02d "$n")-*.webm 2>/dev/null | head -1 || true)
       if [ -n "$src" ]; then
         ff -y -v error -i "$src" -t "$need" -vf "scale=$W:$H:force_original_aspect_ratio=decrease,pad=$W:$H:(ow-iw)/2:(oh-ih)/2:color=0xFAF8F4,tpad=stop_mode=clone:stop_duration=$need,format=yuv420p" -r $FPS -c:v libx264 -preset medium -crf 26 -an "$clip"
       else title "$clip" "$need" "$(python3 -c "import json;print([r['scene'] for r in json.load(open('$WORK/scenes.json')) if r['n']==$n][0])")" "recording missing"; fi ;;
  esac
  # narration starts one second in, silence after
  ff -y -v error -i "$clip" -i "$nar" -filter_complex "[1:a]adelay=1000|1000,apad[a]" -map 0:v -map "[a]" -t "$need" -c:v copy -c:a aac -b:a 64k -ac 1 "$WORK/s$(printf %02d "$n").mp4"
  clips+=("$WORK/s$(printf %02d "$n").mp4")
done < <(python3 -c "import json;[print(r['n'], r['seconds']) for r in json.load(open('$WORK/scenes.json'))]")

# 3. Concatenate and keep it under 60 MB.
: > "$WORK/concat.txt"; for c in "${clips[@]}"; do printf "file '%s'\n" "$(realpath "$c")" >> "$WORK/concat.txt"; done
ff -y -v error -f concat -safe 0 -i "$WORK/concat.txt" -c:v libx264 -preset slow -crf 27 -pix_fmt yuv420p -r $FPS -c:a aac -b:a 64k -ac 1 -movflags +faststart "$OUT"
size=$(stat -c %s "$OUT"); echo "$OUT: $(( size / 1048576 )) MB, $(dur "$OUT") s"
[ "$size" -lt $((60 * 1048576)) ] || { echo "over 60 MB"; exit 1; }
