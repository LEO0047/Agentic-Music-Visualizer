#!/usr/bin/env python3
"""A stand-in for the ``codex`` binary, so failover can be tested for free.

Every real decision costs ~25k tokens of the ChatGPT subscription's Codex quota
(SPEC §2), which makes the one thing Phase 4 most needs to prove — that the
rule director takes over cleanly when the GPT path dies — the most expensive
thing to test. This script speaks the exact argv shape
:meth:`amv.codex_client.CodexClient.build_argv` produces and fails on demand::

    AMV_CODEX_BIN=$PWD/tools/fake_codex.py \\
    AMV_FAKE_CODEX_MODE=fail \\
    uv run python -m amv.sidecar --director gpt ...

``CodexClient.find_codex`` honours ``$AMV_CODEX_BIN`` before ``PATH``, so
nothing else has to change to swap the real binary out.

Modes (``$AMV_FAKE_CODEX_MODE``):

===========  =================================================================
``ok``       Default. Writes a schema-valid decision derived from a hash of the
             prompt — different prompts get different looks, the same prompt
             always gets the same one — after ``$AMV_FAKE_CODEX_DELAY`` seconds
             (default 0.5, standing in for the measured 13.2 s).
``fail``     Exits 1 with a message on stderr, like a quota or auth failure.
``hang``     Sleeps 120 s so the client's own timeout is what ends the call.
``garbage``  Writes something that is not JSON.
===========  =================================================================

The enum values come from the schema handed to ``--output-schema``, so this
fake cannot drift out of sync with ``director_schema.json``; the hard-coded
lists below are only used when that file cannot be read.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Sequence

__all__ = ["MODES", "VERSION", "decision_for", "main"]

#: What ``--version`` prints. Matches the shape of the real codex-cli output.
VERSION = "codex-cli 0.153.4 (amv fake)"

MODES: tuple[str, ...] = ("ok", "fail", "hang", "garbage")

#: Used only if ``--output-schema`` cannot be read.
FALLBACK_ENUMS: dict[str, tuple[str, ...]] = {
    "scene": ("fractal_temple", "tunnel", "particle_field", "kaleido_mesh", "projectm_blend"),
    "palette": ("violet_cyan", "acid_lime", "amber_dusk", "mono_white", "infrared"),
    "particle_mode": ("spiral", "burst", "rain", "orbit", "none"),
    "transition_mode": ("cut", "glide", "on_next_kick"),
}

HANG_SECONDS = 120.0
DEFAULT_DELAY = 0.5


def _enums(schema_path: str | None) -> dict[str, tuple[str, ...]]:
    """Enum values from the schema we were handed, falling back to the constants."""
    if not schema_path:
        return dict(FALLBACK_ENUMS)
    try:
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        props = schema["properties"]
        return {
            "scene": tuple(props["scene"]["enum"]),
            "palette": tuple(props["palette"]["enum"]),
            "particle_mode": tuple(props["particle_mode"]["enum"]),
            "transition_mode": tuple(props["transition"]["properties"]["mode"]["enum"]),
        }
    except Exception:  # noqa: BLE001 - a fake must never be the thing that breaks
        return dict(FALLBACK_ENUMS)


def decision_for(prompt: str, schema_path: str | None = None) -> dict[str, Any]:
    """A schema-valid decision that varies with (and only with) the prompt."""
    enums = _enums(schema_path)
    digest = hashlib.sha256(prompt.encode("utf-8")).digest()

    def pick(index: int, values: Sequence[str]) -> str:
        return values[digest[index] % len(values)]

    def unit(index: int) -> float:
        return digest[index] / 255.0

    return {
        "scene": pick(0, enums["scene"]),
        "palette": pick(1, enums["palette"]),
        "feedback": round(unit(2) * 0.98, 3),
        "symmetry": 1 + digest[3] % 16,
        "camera_speed": round(unit(4), 3),
        "particle_mode": pick(5, enums["particle_mode"]),
        "projectm_mix": round(unit(6), 3),
        "transition": {
            "mode": pick(7, enums["transition_mode"]),
            "beats": 1 + digest[8] % 16,
        },
        "on_drop": {
            "scene": pick(9, enums["scene"]),
            "palette": pick(10, enums["palette"]),
            "particle_mode": pick(11, enums["particle_mode"]),
        },
        "intent": f"fake codex #{digest[12]:02x}：依 prompt hash 產生的假決策，用來測 failover 與節奏",
    }


#: Flags that take a value, mapped to the field they fill.
_VALUE_FLAGS: dict[str, str] = {
    "-m": "model",
    "--model": "model",
    "-c": "config",
    "--config": "config",
    "-s": "sandbox",
    "--sandbox": "sandbox",
    "-C": "cd",
    "--cd": "cd",
    "--output-schema": "output_schema",
    "-o": "output_last_message",
    "--output-last-message": "output_last_message",
}

#: Flags that take no value.
_BARE_FLAGS: frozenset[str] = frozenset({"--skip-git-repo-check", "--json", "--full-auto"})


class Args:
    """Parsed argv. Hand-rolled because argparse cannot place a trailing
    positional (the prompt) after interspersed options once a first positional
    (``exec``) has been consumed — it reports the prompt as unrecognised."""

    def __init__(self) -> None:
        self.command: str = ""
        self.model: str | None = None
        self.config: list[str] = []
        self.sandbox: str | None = None
        self.cd: str | None = None
        self.output_schema: str | None = None
        self.output_last_message: str | None = None
        self.prompt: str = ""


def _parse_args(argv: Sequence[str]) -> Args:
    args = Args()
    positionals: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in _VALUE_FLAGS:
            field = _VALUE_FLAGS[token]
            value = argv[i + 1] if i + 1 < len(argv) else ""
            if field == "config":
                args.config.append(value)
            else:
                setattr(args, field, value)
            i += 2
            continue
        if token in _BARE_FLAGS:
            i += 1
            continue
        if token.startswith("--") and "=" in token:
            flag, _, value = token.partition("=")
            if flag in _VALUE_FLAGS:
                field = _VALUE_FLAGS[flag]
                if field == "config":
                    args.config.append(value)
                else:
                    setattr(args, field, value)
                i += 1
                continue
        positionals.append(token)
        i += 1
    if positionals:
        args.command = positionals[0]
    if len(positionals) > 1:
        args.prompt = positionals[-1]
    return args


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv or "-V" in argv:
        print(VERSION)
        return 0
    if not argv:
        print("fake_codex: nothing to do", file=sys.stderr)
        return 2

    args = _parse_args(argv)
    if args.command != "exec":
        print(f"fake_codex: unsupported command {args.command!r}", file=sys.stderr)
        return 2

    mode = os.environ.get("AMV_FAKE_CODEX_MODE", "ok").strip().lower() or "ok"
    if mode not in MODES:
        print(f"fake_codex: unknown AMV_FAKE_CODEX_MODE {mode!r}", file=sys.stderr)
        return 2

    if mode == "hang":
        # Never returns in practice: the client's 30 s timeout is the point.
        time.sleep(HANG_SECONDS)
        return 0

    try:
        delay = float(os.environ.get("AMV_FAKE_CODEX_DELAY", DEFAULT_DELAY))
    except ValueError:
        delay = DEFAULT_DELAY
    if delay > 0:
        time.sleep(delay)

    if mode == "fail":
        print(
            "fake_codex: stream error: exceeded retry limit "
            "(simulated Codex failure, AMV_FAKE_CODEX_MODE=fail)",
            file=sys.stderr,
        )
        return 1

    out = args.output_last_message
    if not out:
        print("fake_codex: no -o output path given", file=sys.stderr)
        return 2
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if mode == "garbage":
        out_path.write_text(
            "I looked at the track and I think we should go with the tunnel scene.\n",
            encoding="utf-8",
        )
        return 0

    payload = decision_for(args.prompt, args.output_schema)
    out_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
