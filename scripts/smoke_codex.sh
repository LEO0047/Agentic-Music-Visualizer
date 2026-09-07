#!/usr/bin/env bash
# Phase 0 acceptance: one real `codex exec` decision end to end.
# Spends real Codex quota (~25k tokens per call) — run it deliberately.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

exec uv run python -m amv.smoke "$@" < /dev/null
