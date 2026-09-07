#!/usr/bin/env python3
"""Synthetic TouchDesigner: a scripted Psytrance track on ``/feat/*`` (SPEC §3.1).

TouchDesigner is not installed on this machine, and even when it is, testing a
section detector against a real track means playing a real track. This module
generates the same 10 Hz feature stream TD would send, from a declarative
script, deterministically.

Two ways to use it:

* **Over UDP** — ``python tools/fake_td.py --to 127.0.0.1:9000`` plays the
  track into a running ``python -m amv.sidecar``. It prints the ground-truth
  section timeline first, so a human can diff it against the sidecar's log.
  ``--speed 20`` compresses a two-minute track into six seconds (give the
  sidecar the same ``--speed`` so its windows stay in track time).
* **In process** — :func:`timeline` and :func:`energy_bass_stream` hand the
  same samples straight to :class:`~amv.sections.SectionDetector`, which is how
  the tests check detection latency without touching a socket.

The default script is the shape of a Psytrance set: a steady groove, a twenty
second build, the drop, a long steady stretch, a breakdown with the kick gone,
and a second drop out of it.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

__all__ = [
    "DEFAULT_SCRIPT",
    "DETECT_LAG_S",
    "FEATURES",
    "SectionSpan",
    "ground_truth",
    "section_at",
    "timeline",
    "energy_bass_stream",
    "format_ground_truth",
    "load_script",
    "duration",
    "main",
]

#: Continuous features, all normalised 0–1 except ``centroid`` (Hz).
FEATURES: tuple[str, ...] = ("bass", "mid", "high", "energy", "centroid")

DEFAULT_SCRIPT: dict[str, Any] = {
    "name": "psytrance_145",
    "bpm": 145,
    "noise": 0.015,
    "centroid_noise": 60.0,
    "seed": 20260908,
    "sections": [
        {
            "name": "steady",
            "duration": 20.0,
            "energy": 0.60,
            "bass": 0.55,
            "mid": 0.50,
            "high": 0.45,
            "centroid": 2200.0,
        },
        {
            # Energy ramps .6 -> .85 and the highs open up; bass deliberately
            # stays under the 0.7 drop threshold so the build cannot fake one.
            "name": "build",
            "duration": 20.0,
            "energy": [0.60, 0.85],
            "bass": [0.50, 0.62],
            "mid": [0.50, 0.60],
            "high": [0.35, 0.80],
            "centroid": [2200.0, 3600.0],
        },
        {
            "name": "drop",
            "duration": 8.0,
            "energy": 0.90,
            "bass": 0.90,
            "mid": 0.60,
            "high": 0.55,
            "centroid": 1800.0,
        },
        {
            "name": "steady",
            "duration": 30.0,
            "energy": 0.60,
            "bass": 0.55,
            "mid": 0.50,
            "high": 0.45,
            "centroid": 2200.0,
        },
        {
            # Kick drops out entirely — that is what a breakdown sounds like.
            "name": "breakdown",
            "duration": 10.0,
            "energy": 0.20,
            "bass": 0.10,
            "mid": 0.25,
            "high": 0.30,
            "centroid": 2600.0,
            "kicks": False,
        },
        {
            "name": "drop",
            "duration": 8.0,
            "energy": 0.90,
            "bass": 0.90,
            "mid": 0.60,
            "high": 0.55,
            "centroid": 1800.0,
        },
        {
            "name": "steady",
            "duration": 20.0,
            "energy": 0.60,
            "bass": 0.55,
            "mid": 0.50,
            "high": 0.45,
            "centroid": 2200.0,
        },
    ],
}

#: How long after a section *starts* :class:`~amv.sections.SectionDetector` can
#: possibly call it, given the SPEC §4 rules and the detector's defaults. These
#: are properties of the rules, not fudge factors:
#:
#: * ``drop`` — a bass jump is visible in the sample it happens in.
#: * ``breakdown`` — SPEC requires energy below 0.35 for *more than* 4 s.
#: * ``build`` — SPEC says "energy rising for 20 s", measured as a running
#:   least-squares slope over a 20 s window. On the default ramp (0.0125 / s
#:   out of a flat 0.6) that window's slope clears the entry threshold, and the
#:   end-to-end rise clears 0.08, about 7.5 s into the ramp.
DETECT_LAG_S: dict[str, float] = {
    "steady": 0.0,
    "drop": 0.0,
    "breakdown": 4.0,
    "build": 7.5,
}


@dataclass(frozen=True)
class SectionSpan:
    """One scripted section and when the detector should notice it."""

    name: str
    start: float
    end: float
    detect_at: float

    @property
    def duration(self) -> float:
        return self.end - self.start


# -- script handling --------------------------------------------------------


def load_script(path: str | Path) -> dict[str, Any]:
    """Load a script JSON file, same shape as :data:`DEFAULT_SCRIPT`."""
    with open(path, encoding="utf-8") as fh:
        script = json.load(fh)
    if not isinstance(script, dict) or not script.get("sections"):
        raise ValueError(f"{path}: expected an object with a non-empty 'sections' list")
    return script


def resolve_script(name: str | None) -> dict[str, Any]:
    """``None`` / ``default`` gives the built-in script; anything else is a path."""
    if name in (None, "", "default"):
        return DEFAULT_SCRIPT
    return load_script(name)


def ground_truth(script: dict[str, Any] | None = None) -> list[SectionSpan]:
    """The scripted timeline as spans, with expected detection times."""
    script = script or DEFAULT_SCRIPT
    spans: list[SectionSpan] = []
    t = 0.0
    for section in script["sections"]:
        length = float(section["duration"])
        name = str(section["name"])
        lag = float(section.get("detect_lag", DETECT_LAG_S.get(name, 0.0)))
        spans.append(SectionSpan(name, t, t + length, t + lag))
        t += length
    return spans


def duration(script: dict[str, Any] | None = None) -> float:
    """Total scripted length in seconds."""
    script = script or DEFAULT_SCRIPT
    return sum(float(s["duration"]) for s in script["sections"])


def section_at(t: float, script: dict[str, Any] | None = None) -> str:
    """Ground-truth section name at time ``t`` (clamped to the last section)."""
    spans = ground_truth(script)
    for span in spans:
        if t < span.end:
            return span.name
    return spans[-1].name


# -- sample generation ------------------------------------------------------


def _value(spec: Any, phase: float) -> float:
    """A scalar, or a ``[start, end]`` ramp evaluated at ``phase`` in 0–1."""
    if isinstance(spec, (list, tuple)):
        start, end = float(spec[0]), float(spec[-1])
        return start + (end - start) * phase
    return float(spec)


def _kick_indices(script: dict[str, Any], rate_hz: int) -> set[int]:
    """Sample indices that carry a kick pulse, on the BPM grid."""
    bpm = float(script.get("bpm", 145))
    if bpm <= 0:
        return set()
    beat = 60.0 / bpm
    total = duration(script)
    silent: list[tuple[float, float]] = []
    t = 0.0
    for section in script["sections"]:
        length = float(section["duration"])
        if not section.get("kicks", True):
            silent.append((t, t + length))
        t += length
    indices: set[int] = set()
    n = 0
    while True:
        beat_t = n * beat
        n += 1
        if beat_t >= total:
            break
        if any(lo <= beat_t < hi for lo, hi in silent):
            continue
        indices.add(int(round(beat_t * rate_hz)))
    return indices


def timeline(
    script: dict[str, Any] | None = None,
    rate_hz: int = 10,
    seed: int | None = None,
) -> list[tuple[float, dict[str, float]]]:
    """Render the whole script as ``(t, {feature: value})`` samples.

    Deterministic: the same script and seed always produce the same numbers,
    so a detection latency measured in a test stays measured.
    """
    script = script or DEFAULT_SCRIPT
    if seed is None:
        seed = int(script.get("seed", 0))
    rng = random.Random(seed)
    noise = float(script.get("noise", 0.0))
    centroid_noise = float(script.get("centroid_noise", 0.0))
    kicks = _kick_indices(script, rate_hz)
    step = 1.0 / rate_hz

    out: list[tuple[float, dict[str, float]]] = []
    index = 0
    t0 = 0.0
    for section in script["sections"]:
        length = float(section["duration"])
        count = int(round(length * rate_hz))
        for i in range(count):
            t = t0 + i * step
            phase = (i / count) if count else 0.0
            sample: dict[str, float] = {}
            for key in FEATURES:
                if key not in section:
                    continue
                value = _value(section[key], phase)
                if key == "centroid":
                    value = max(20.0, value + rng.gauss(0.0, centroid_noise))
                else:
                    value = min(1.0, max(0.0, value + rng.gauss(0.0, noise)))
                sample[key] = value
            sample["kick"] = 1.0 if index in kicks else 0.0
            out.append((round(t, 6), sample))
            index += 1
        t0 += length
    return out


def energy_bass_stream(
    script: dict[str, Any] | None = None,
    rate_hz: int = 10,
    seed: int | None = None,
) -> Iterator[tuple[float, float, float]]:
    """``(t, energy, bass)`` triples — exactly what ``SectionDetector`` eats."""
    for t, sample in timeline(script, rate_hz, seed):
        yield t, sample["energy"], sample["bass"]


# -- presentation -----------------------------------------------------------


def _clock(t: float) -> str:
    return f"{int(t) // 60:02d}:{t - 60 * (int(t) // 60):04.1f}"


def format_ground_truth(script: dict[str, Any] | None = None) -> str:
    """Human-readable ground truth, printed before playback."""
    script = script or DEFAULT_SCRIPT
    spans = ground_truth(script)
    lines = [
        f"ground truth — {script.get('name', 'script')} "
        f"@ {script.get('bpm', '?')} BPM, {duration(script):.1f} s",
        f"  {'start':>8}  {'end':>8}  {'section':<10}  detectable from",
    ]
    for span in spans:
        lines.append(
            f"  {_clock(span.start):>8}  {_clock(span.end):>8}  {span.name:<10}"
            f"  {_clock(span.detect_at)}"
        )
    return "\n".join(lines)


# -- CLI --------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tools/fake_td.py",
        description="Play a scripted /feat/* stream at TouchDesigner's 10 Hz.",
    )
    parser.add_argument(
        "--to", default="127.0.0.1:9000", help="sidecar OSC endpoint (default 127.0.0.1:9000)"
    )
    parser.add_argument("--rate", type=int, default=10, help="samples per second (default 10)")
    parser.add_argument(
        "--script", default="default", help="'default' or a path to a script JSON file"
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="time scale; 20 plays a 2 min track in 6 s (give the sidecar the same --speed)",
    )
    parser.add_argument("--seed", type=int, default=None, help="override the script's noise seed")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the ground truth and exit, send nothing"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    script = resolve_script(args.script)
    print(format_ground_truth(script), flush=True)
    if args.dry_run:
        return 0
    if args.speed <= 0:
        print("--speed must be positive", file=sys.stderr)
        return 2

    # Imported late so --dry-run works without the dependency installed.
    from pythonosc.udp_client import SimpleUDPClient

    from amv.osc_io import parse_endpoint

    host, port = parse_endpoint(args.to)
    client = SimpleUDPClient(host, port)
    samples = timeline(script, args.rate, args.seed)
    print(
        f"sending {len(samples)} samples to {host}:{port} "
        f"at {args.rate} Hz x{args.speed:g}",
        flush=True,
    )

    started = time.monotonic()
    current = ""
    sent = 0
    try:
        for t, sample in samples:
            due = started + t / args.speed
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            for key, value in sample.items():
                if key == "kick":
                    client.send_message("/feat/kick", int(value))
                else:
                    client.send_message(f"/feat/{key}", float(value))
                sent += 1
            name = section_at(t, script)
            if name != current:
                current = name
                print(f"  {_clock(t)}  -> {name}", flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted", flush=True)
        return 130
    print(f"done — {sent} messages in {time.monotonic() - started:.1f} s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
