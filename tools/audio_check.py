#!/usr/bin/env python3
"""Phase 1 audio-routing checks — BlackHole in, numbers out.

    uv run python tools/audio_check.py devices
    uv run python tools/audio_check.py loopback [--seconds 1.5] [--freq 1000] [--json]
    uv run python tools/audio_check.py meter    [--seconds 10] [--json]

``devices`` lists what CoreAudio sees and names the BlackHole index.

``loopback`` needs **no user setup**: it plays a −12 dBFS sine into the
BlackHole *output* while recording the BlackHole *input* on the same device.
BlackHole is a loopback driver, so whatever goes out comes straight back. This
isolates "is the driver working at 48 kHz" from "is the system output routed
into it", which is the one question the GUI step in
``docs/phase1-audio-routing.md`` answers.

``meter`` is the second half: it reads the BlackHole input in 100 ms blocks and
prints live RMS, bass/mid/high bars and kick pulses. It only shows signal once a
Multi-Output device (speakers + BlackHole) is the system output *and* music is
playing — everything it needs a human for.

Feature maths lives in ``amv.audio_features``; this file is device plumbing and
formatting only. ``sounddevice`` is imported lazily so ``--help`` and the import
of this module work without the ``audio`` extra.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

if __package__ in (None, ""):  # running as a script from a checkout
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))

from amv.audio_features import (  # noqa: E402  (after the sys.path shim)
    BLOCK_SECONDS,
    KickDetector,
    Normalizer,
    band_energies,
    find_blackhole,
    to_mono,
)

SAMPLE_RATE = 48000
TONE_DBFS = -12.0
LOOPBACK_MIN_RMS = 0.05
LOOPBACK_MAX_FREQ_ERROR_HZ = 2.0
METER_MIN_RMS = 0.01
BASS_SMOOTHING = 0.5  # one-pole on normalised bass ≈ the Lag 0.05 s TD applies

ROUTING_HINT = "系統輸出還是喇叭？請照 docs/phase1-audio-routing.md 建立多重輸出裝置"
MISSING_SOUNDDEVICE = (
    "sounddevice is not available. Install the audio extra:\n"
    "    uv sync --group dev --extra audio\n"
    "(and make sure BlackHole 2ch is installed: brew install blackhole-2ch)"
)


class CheckError(RuntimeError):
    """A check could not run — bad device, missing driver, refused stream."""


# -- plumbing ---------------------------------------------------------------


def load_sounddevice() -> Any:
    """Import ``sounddevice`` with a message a human can act on."""
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise CheckError(f"{MISSING_SOUNDDEVICE}\n({exc})") from exc
    except OSError as exc:  # PortAudio present as a wheel but unloadable
        raise CheckError(f"sounddevice failed to load PortAudio: {exc}") from exc
    return sd


def device_rows(sd: Any) -> list[dict[str, Any]]:
    return [dict(entry) for entry in sd.query_devices()]


def resolve_device(sd: Any, spec: str | None) -> tuple[int, dict[str, Any]]:
    """Resolve ``--device`` (index, name fragment, or ``None`` → BlackHole)."""
    rows = device_rows(sd)
    if spec is None:
        index = find_blackhole(rows)
        if index is None:
            raise CheckError(
                "no input-capable BlackHole device found. Install it with "
                "`brew install blackhole-2ch`, or pass --device."
            )
    elif spec.lstrip("+-").isdigit():
        index = int(spec)
    else:
        index = find_blackhole(rows, match=spec)
        if index is None:
            raise CheckError(f"no input-capable device whose name contains {spec!r}")

    for row in rows:
        if int(row.get("index", -1)) == index:
            return index, row
    raise CheckError(f"device index {index} is not in the device list")


def dominant_frequency(mono: np.ndarray, sr: int) -> float:
    """Peak frequency in Hz, parabolically interpolated between rfft bins."""
    n = mono.size
    if n < 4:
        return 0.0
    spectrum = np.abs(np.fft.rfft(mono * np.hanning(n)))
    if spectrum.size < 3 or not np.any(spectrum > 0.0):
        return 0.0
    k = int(np.argmax(spectrum))
    offset = 0.0
    if 0 < k < spectrum.size - 1:
        left, peak, right = (float(spectrum[k - 1]), float(spectrum[k]), float(spectrum[k + 1]))
        denominator = left - 2.0 * peak + right
        if denominator != 0.0:
            offset = float(np.clip(0.5 * (left - right) / denominator, -0.5, 0.5))
    return (k + offset) * (float(sr) / n)


def rms(mono: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(mono)))) if mono.size else 0.0


def bar(value: float, width: int = 12) -> str:
    filled = int(round(min(max(value, 0.0), 1.0) * width))
    return "█" * filled + "░" * (width - filled)


def emit(payload: dict[str, Any], as_json: bool, lines: list[str]) -> None:
    """Print either the machine-readable summary or the human one."""
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for line in lines:
            print(line)


# -- commands ---------------------------------------------------------------


def cmd_devices(args: argparse.Namespace) -> int:
    sd = load_sounddevice()
    rows = device_rows(sd)
    blackhole = find_blackhole(rows)
    try:
        default_in, default_out = sd.default.device
    except (TypeError, ValueError):  # pragma: no cover - platform dependent
        default_in, default_out = (None, None)

    if args.json:
        print(
            json.dumps(
                {
                    "blackhole_index": blackhole,
                    "default_input": default_in,
                    "default_output": default_out,
                    "devices": [
                        {
                            "index": row.get("index"),
                            "name": row.get("name"),
                            "max_input_channels": row.get("max_input_channels"),
                            "max_output_channels": row.get("max_output_channels"),
                            "default_samplerate": row.get("default_samplerate"),
                        }
                        for row in rows
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print("CoreAudio devices")
    print("-" * 72)
    for row in rows:
        index = row.get("index")
        marks = []
        if index == default_in:
            marks.append("default in")
        if index == default_out:
            marks.append("default out")
        if index == blackhole:
            marks.append("BlackHole")
        suffix = f"  <- {', '.join(marks)}" if marks else ""
        print(
            f"{index:>3}  {row.get('name')}"
            f"  ({row.get('max_input_channels')} in, {row.get('max_output_channels')} out,"
            f" {float(row.get('default_samplerate', 0)):.0f} Hz){suffix}"
        )
    print("-" * 72)
    if blackhole is None:
        print("BlackHole: not found — brew install blackhole-2ch")
    else:
        print(f"BlackHole: index {blackhole}")
    return 0


def cmd_loopback(args: argparse.Namespace) -> int:
    sd = load_sounddevice()
    index, row = resolve_device(sd, args.device)
    if int(row.get("max_input_channels", 0)) < 1 or int(row.get("max_output_channels", 0)) < 1:
        raise CheckError(
            f"device {index} ({row.get('name')}) is not duplex; loopback needs input and output"
        )

    seconds = float(args.seconds)
    freq = float(args.freq)
    amplitude = 10.0 ** (TONE_DBFS / 20.0)
    n = int(round(seconds * SAMPLE_RATE))
    t = np.arange(n, dtype=np.float64) / SAMPLE_RATE
    tone = amplitude * np.sin(2.0 * np.pi * freq * t)

    fade = min(int(0.005 * SAMPLE_RATE), n // 2)  # 5 ms, so the edges do not click
    if fade > 0:
        ramp = 0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, fade)))
        tone[:fade] *= ramp
        tone[-fade:] *= ramp[::-1]
    stereo = np.repeat(tone[:, None], 2, axis=1).astype(np.float32)

    try:
        recorded = sd.playrec(
            stereo,
            samplerate=SAMPLE_RATE,
            channels=2,
            device=(index, index),
            blocking=True,
        )
    except Exception as exc:  # sd.PortAudioError and friends
        raise CheckError(f"playrec on device {index} ({row.get('name')}) failed: {exc}") from exc

    mono = to_mono(recorded)
    # Skip the head (stream start-up latency records silence) and the tail fade.
    start, stop = int(0.25 * mono.size), int(0.95 * mono.size)
    analysed = mono[start:stop] if stop > start else mono

    measured_rms = rms(analysed)
    peak_hz = dominant_frequency(analysed, SAMPLE_RATE)
    error_hz = abs(peak_hz - freq)
    ok = measured_rms > LOOPBACK_MIN_RMS and error_hz <= LOOPBACK_MAX_FREQ_ERROR_HZ

    payload = {
        "check": "loopback",
        "ok": ok,
        "device_index": index,
        "device_name": row.get("name"),
        "samplerate": SAMPLE_RATE,
        "seconds": seconds,
        "tone_dbfs": TONE_DBFS,
        "requested_hz": freq,
        "measured_hz": round(peak_hz, 3),
        "freq_error_hz": round(error_hz, 3),
        "recorded_rms": round(measured_rms, 6),
        "min_rms": LOOPBACK_MIN_RMS,
        "max_freq_error_hz": LOOPBACK_MAX_FREQ_ERROR_HZ,
    }
    lines = [
        f"loopback via [{index}] {row.get('name')} @ {SAMPLE_RATE} Hz, "
        f"{seconds:g} s, {freq:g} Hz @ {TONE_DBFS:g} dBFS",
        f"  recorded RMS   {measured_rms:.4f}  (need > {LOOPBACK_MIN_RMS})",
        f"  dominant freq  {peak_hz:.2f} Hz  (error {error_hz:.2f} Hz, "
        f"need <= {LOOPBACK_MAX_FREQ_ERROR_HZ:g})",
        f"{'PASS' if ok else 'FAIL'}: BlackHole loopback "
        f"{'plays back what it is given' if ok else 'did not return the test tone'}",
    ]
    if not ok and measured_rms <= LOOPBACK_MIN_RMS:
        payload["hint"] = "沒有回讀到訊號：確認 BlackHole 2ch 驅動已安裝、終端機有麥克風/錄音權限"
        lines.append(f"  hint: {payload['hint']}")
    emit(payload, args.json, lines)
    return 0 if ok else 1


def cmd_meter(args: argparse.Namespace) -> int:
    sd = load_sounddevice()
    index, row = resolve_device(sd, args.device)
    if int(row.get("max_input_channels", 0)) < 1:
        raise CheckError(f"device {index} ({row.get('name')}) has no input channels")

    seconds = float(args.seconds)
    blocksize = int(round(BLOCK_SECONDS * SAMPLE_RATE))
    channels = min(2, int(row.get("max_input_channels", 1)))
    total_blocks = max(1, int(round(seconds / BLOCK_SECONDS)))

    normalizer = Normalizer()
    kicks = KickDetector()
    smoothed_bass = 0.0
    rms_values: list[float] = []
    kick_times: list[float] = []
    overflows = 0
    stream_out = sys.stderr if args.json else sys.stdout

    if not args.json:
        print(
            f"meter on [{index}] {row.get('name')} @ {SAMPLE_RATE} Hz, "
            f"{BLOCK_SECONDS * 1000:.0f} ms blocks, {seconds:g} s"
        )

    try:
        stream = sd.InputStream(
            device=index,
            channels=channels,
            samplerate=SAMPLE_RATE,
            blocksize=blocksize,
            dtype="float32",
        )
    except Exception as exc:
        raise CheckError(f"could not open input on device {index}: {exc}") from exc

    started = time.monotonic()
    try:
        with stream:
            for _ in range(total_blocks):
                block, overflowed = stream.read(blocksize)
                overflows += int(bool(overflowed))
                elapsed = time.monotonic() - started
                features = band_energies(block, SAMPLE_RATE)
                rms_values.append(features["energy"])
                unit = normalizer.update(
                    {key: features[key] for key in ("bass", "mid", "high", "energy")}
                )
                smoothed_bass += BASS_SMOOTHING * (unit["bass"] - smoothed_bass)
                kicked = kicks.update(smoothed_bass, dt=BLOCK_SECONDS)
                if kicked:
                    kick_times.append(elapsed)
                print(
                    f"{elapsed:6.2f}s  rms {features['energy']:.4f}"
                    f" | B {bar(unit['bass'])} {unit['bass']:.2f}"
                    f" | M {bar(unit['mid'])} {unit['mid']:.2f}"
                    f" | H {bar(unit['high'])} {unit['high']:.2f}"
                    f" | {features['centroid']:6.0f} Hz"
                    f"{'  KICK' if kicked else ''}",
                    file=stream_out,
                    flush=True,
                )
    except KeyboardInterrupt:
        print("", file=stream_out)
    except Exception as exc:
        raise CheckError(f"reading device {index} failed: {exc}") from exc

    duration = max(time.monotonic() - started, 1e-6)
    mean_rms = float(np.mean(rms_values)) if rms_values else 0.0
    min_rms = float(np.min(rms_values)) if rms_values else 0.0
    max_rms = float(np.max(rms_values)) if rms_values else 0.0
    silent_blocks = sum(1 for value in rms_values if value <= METER_MIN_RMS)
    ok = bool(rms_values) and silent_blocks == 0
    kicks_per_min = len(kick_times) / duration * 60.0

    payload = {
        "check": "meter",
        "ok": ok,
        "device_index": index,
        "device_name": row.get("name"),
        "samplerate": SAMPLE_RATE,
        "block_seconds": BLOCK_SECONDS,
        "seconds": round(duration, 3),
        "blocks": len(rms_values),
        "silent_blocks": silent_blocks,
        "mean_rms": round(mean_rms, 6),
        "min_rms": round(min_rms, 6),
        "max_rms": round(max_rms, 6),
        "min_rms_threshold": METER_MIN_RMS,
        "kicks": len(kick_times),
        "kicks_per_min": round(kicks_per_min, 1),
        "overflows": overflows,
    }
    lines = [
        f"  blocks {len(rms_values)}  mean RMS {mean_rms:.4f}"
        f"  min {min_rms:.4f}  max {max_rms:.4f}",
        f"  kicks {len(kick_times)}  ≈ {kicks_per_min:.1f} kicks/min",
    ]
    if ok:
        lines.append(f"PASS: every block above RMS {METER_MIN_RMS}")
    else:
        payload["hint"] = ROUTING_HINT
        lines.append(
            f"FAIL: {silent_blocks}/{len(rms_values)} blocks at or below RMS {METER_MIN_RMS}"
            if rms_values
            else "FAIL: no audio blocks were captured"
        )
        lines.append(f"  hint: {ROUTING_HINT}")
    emit(payload, args.json, lines)
    return 0 if ok else 1


# -- cli --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audio_check.py",
        description="Phase 1 audio-routing checks for the BlackHole → sidecar path.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    devices = subparsers.add_parser("devices", help="list CoreAudio devices and the BlackHole index")
    devices.add_argument("--json", action="store_true", help="machine-readable output")
    devices.set_defaults(func=cmd_devices)

    loopback = subparsers.add_parser(
        "loopback", help="play a test tone into BlackHole and read it back (no setup needed)"
    )
    loopback.add_argument("--seconds", type=float, default=1.5, help="tone duration (default 1.5)")
    loopback.add_argument("--freq", type=float, default=1000.0, help="tone frequency (default 1000)")
    loopback.add_argument("--device", help="device index or name fragment (default: BlackHole)")
    loopback.add_argument("--json", action="store_true", help="machine-readable summary")
    loopback.set_defaults(func=cmd_loopback)

    meter = subparsers.add_parser(
        "meter", help="live RMS / bass / mid / high / kick meter from the BlackHole input"
    )
    meter.add_argument("--seconds", type=float, default=10.0, help="run duration (default 10)")
    meter.add_argument("--device", help="device index or name fragment (default: BlackHole)")
    meter.add_argument(
        "--json", action="store_true", help="summary as JSON on stdout, meter lines on stderr"
    )
    meter.set_defaults(func=cmd_meter)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except CheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
