#!/usr/bin/env bash
# Record the demo and compress the video (H.264, under 15 MB with the screenshots).
set -euo pipefail
cd "$(dirname "$0")/../.."
node scripts/demo/record_demo.mjs
ffmpeg -y -loglevel error -i docs/demo/raw/demo.webm -vf "fps=12,scale=1024:-2" -c:v libx264 -preset slow -crf 30 -pix_fmt yuv420p -an docs/demo/demo.mp4
rm -rf docs/demo/raw
du -sh docs/demo
