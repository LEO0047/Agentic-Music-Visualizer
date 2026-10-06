#!/bin/sh
cd -- "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)" || exit 1
# Ask before applying the saved output; --yes opts in for scripted recovery.
exec './Start Visualizer.command' --restore-audio "$@"
