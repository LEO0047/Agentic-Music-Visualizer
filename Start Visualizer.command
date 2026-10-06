#!/bin/sh
# A stdlib-only bootstrap. Never install dependencies just by opening this file.
cd -- "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if [ -x .venv/bin/python ]; then
  PYTHON=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
else
  printf '%s\n' 'Python is missing. Install Python 3.11+ and uv: https://docs.astral.sh/uv/' >&2
  exit 1
fi
"$PYTHON" -B -m amv.launcher "$@"
status=$?
if [ "$status" -ne 0 ] && [ -t 0 ] && [ -t 1 ]; then
  printf '\nPress Return to close. '
  IFS= read -r answer
fi
exit "$status"
