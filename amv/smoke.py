"""Phase 0 acceptance check: one real ``codex exec`` decision, end to end.

    uv run python -m amv.smoke     (or: scripts/smoke_codex.sh)

Prints the wall-clock seconds and the decision JSON, exits 0 on success and 1
with the error message on failure. This spends real Codex quota — roughly 25k
tokens per call — so it is a check, not something to loop.
"""

from __future__ import annotations

import json
import sys
import time

from .codex_client import CodexClient, CodexError

SAMPLE_PROMPT = """You are the visual director for a live techno set. Return one decision as JSON matching the provided schema.

Current audio features:
- BPM: 145
- bass: 0.82
- mid: 0.55
- high: 0.71
- energy: 0.83, and rising
- section: build

Recent visual history (most recent last):
1. scene=tunnel, palette=violet_cyan, held 40 s
2. scene=kaleido_mesh, palette=acid_lime, held 55 s
3. scene=tunnel, palette=amber_dusk, held 30 s

Rules:
- Do not repeat a scene+palette combination used in the last 60 seconds.
- We are in a build: escalate tension so the drop lands harder.
- intent must be at most 120 characters.
- on_drop must be filled in — it is what the reflex layer fires the frame the drop hits.

Reply with the JSON object only."""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        client = CodexClient()
    except CodexError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(f"codex binary : {client.binary}")
    print(f"model        : {client.model} (reasoning effort {client.effort})")
    print(f"schema       : {client.schema_path}")
    print(f"sandbox cwd  : {client.cwd}")
    print(f"timeout      : {client.timeout:g}s")
    print("running one decision...", flush=True)

    started = time.perf_counter()
    try:
        decision = client.decide(SAMPLE_PROMPT)
    except CodexError as exc:
        elapsed = time.perf_counter() - started
        print(f"FAIL after {elapsed:.1f}s: {exc}", file=sys.stderr)
        return 1
    elapsed = time.perf_counter() - started

    print(f"OK in {elapsed:.1f}s")
    print(json.dumps(decision, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
