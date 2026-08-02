#!/usr/bin/env bash
# Generates hello_world.mp4: 5s black background, white Arabic text
# "مرحبا بالعالم" that fades in slowly and fades out.
set -euo pipefail

cd "$(dirname "$0")"

ffmpeg -y -f lavfi -i color=c=black:s=1280x720:d=5:r=30 \
  -vf "ass=hello_world.ass" \
  -c:v libx264 -pix_fmt yuv420p -t 5 hello_world.mp4
