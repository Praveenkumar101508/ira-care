#!/usr/bin/env bash
# Assemble the IRA CARE film (round 4, phase 3): 1920 x 1080, a title card, the 20-second Blender
# turntable, the recorded scenes with cross-fades, captions and the open voice-over, the hardware
# renders and a closing card. The work is done by scripts/film/make_film.py; see its header.
#
#   PIPER_VOICE=/path/en_GB-cori-high.onnx scripts/film/make_film.sh   # writes docs/film/ira_care.mp4 (< 60 MB)
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec python3 scripts/film/make_film.py "$@"
