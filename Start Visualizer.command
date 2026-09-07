#!/bin/zsh
set -e
cd -- "${0:A:h}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v uv >/dev/null; then
  print 'Install uv first: https://docs.astral.sh/uv/'
  read '?Press Return to close.'
  exit 1
fi
if [[ ! -d /Applications/TouchDesigner.app ]]; then
  print 'Install TouchDesigner and activate its free Non-Commercial license first.'
  read '?Press Return to close.'
  exit 1
fi
uv sync --extra audio
mkdir -p artifacts
swift tools/audio_route.swift enable
function restore_audio() { swift tools/audio_route.swift restore || true; }
trap restore_audio EXIT
trap 'exit 130' INT TERM HUP
open -a TouchDesigner Agentic-Music-Visualizer.toe
print 'Apple Music: select this Mac as output. Use its own volume slider.'
print 'Director: GPT, with automatic rule fallback. Ctrl-C stops it and restores audio.'
uv run python -m amv.sidecar --director gpt --decisions-log artifacts/director-decisions.jsonl
