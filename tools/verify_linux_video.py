#!/usr/bin/env python3
"""Independently decode every frame/sample of a rendered Linux preview.

This verifies encoded-media integrity and provides real decoded-image evidence.
It does not establish real-time rendering, subjective quality, shader float
finiteness, or causal audio-reactivity. Those require separate source checks.
No EGL, display, Pillow, or network access is required.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from render_shaders import png_bytes


def probe_media(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, check=True, text=True,
    )
    return json.loads(result.stdout)


def read_exact(stream, count):
    """Pipe reads may be short; only EOF can end a complete frame."""
    chunks, remaining = [], count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if data and len(data) != count:
        raise ValueError(f"Truncated decoded frame: got {len(data)} of {count} bytes")
    return data


def iter_decoded_frames(path, width, height):
    """Decode all original-resolution frames without inventing/dropping frames."""
    command = [
        "ffmpeg", "-v", "error", "-xerror", "-err_detect", "explode", "-threads", "1",
        "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn", "-fps_mode", "passthrough",
        "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
    ]
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        try:
            while data := read_exact(process.stdout, width * height * 3):
                yield np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3)
            code = process.wait()
            if code:
                errors.seek(0)
                raise RuntimeError(f"ffmpeg decode failed ({code}): {errors.read().decode(errors='replace')}")
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()


def pixel_metrics(frame, previous=None):
    rgb = np.asarray(frame, dtype=np.float32) / 255.0
    luma = rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722
    return {
        "mean_luma": float(luma.mean()),
        "max_rgb": float(rgb.max()),
        "spatial_std": float(rgb.std(axis=(0, 1)).mean()),
        "near_black_fraction": float((luma < 0.01).mean()),
        "near_white_fraction": float((luma > 0.99).mean()),
        "mean_abs_rgb_delta": None if previous is None else float(
            np.abs(frame.astype(np.int16) - previous.astype(np.int16)).mean() / 255.0
        ),
        "exact_repeat": previous is not None and bool(np.array_equal(frame, previous)),
    }


def read_frame_timestamps(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames",
         "-show_entries", "frame=best_effort_timestamp_time,duration_time,pkt_duration_time,key_frame",
         "-of", "json", str(path)],
        capture_output=True, check=True, text=True,
    )
    return json.loads(result.stdout)["frames"]


def inspect_timestamps(frames, fps):
    timestamps = [float(row["best_effort_timestamp_time"]) for row in frames
                  if "best_effort_timestamp_time" in row]
    steps = np.diff(timestamps)
    tolerance = max(2e-6, (1.0 / fps) * 0.001)
    return {
        "timestamp_count": len(timestamps),
        "all_frames_have_timestamp": len(timestamps) == len(frames),
        "strictly_increasing": bool((steps > 0).all()),
        "constant_frame_spacing": bool((np.abs(steps - 1.0 / fps) <= tolerance).all()),
        "first_timestamp_seconds": timestamps[0] if timestamps else None,
        "last_timestamp_seconds": timestamps[-1] if timestamps else None,
        "min_step_seconds": float(steps.min()) if steps.size else None,
        "max_step_seconds": float(steps.max()) if steps.size else None,
    }


def decode_audio(path, stream):
    rate, channels = int(stream["sample_rate"]), int(stream["channels"])
    command = ["ffmpeg", "-v", "error", "-xerror", "-err_detect", "explode", "-threads", "1",
               "-i", str(path), "-map", "0:a:0", "-vn", "-sn", "-dn",
               "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    windows, sample_count, peak, sum_squares, finite = [], 0, 0.0, 0.0, True
    step = max(1, rate // 10)
    # Stream one 100ms window at a time; hour-long media must not require
    # keeping gigabytes of decoded PCM resident in memory.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        try:
            while True:
                chunks, remaining = [], step * channels * 4
                while remaining:
                    chunk = process.stdout.read(remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
                if not raw:
                    break
                if len(raw) % (4 * channels):
                    raise ValueError("Truncated PCM audio sample")
                window = np.frombuffer(raw, dtype="<f4").reshape(-1, channels)
                square_total = float(np.sum(window.astype(np.float64) ** 2))
                windows.append({"time_seconds": sample_count / rate,
                                "rms": float(np.sqrt(square_total / window.size))})
                sample_count += len(window)
                sum_squares += square_total
                peak = max(peak, float(np.abs(window).max()))
                finite = finite and bool(np.isfinite(window).all())
            code = process.wait()
            if code:
                errors.seek(0)
                raise RuntimeError(f"ffmpeg audio decode failed ({code}): {errors.read().decode(errors='replace')}")
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()
    if not sample_count:
        raise ValueError("Audio stream decoded no samples")
    return {
        "sample_rate": rate, "channels": channels, "decoded_sample_frames": sample_count,
        "decoded_duration_seconds": sample_count / rate,
        "all_samples_finite": finite,
        "peak_absolute": peak,
        "rms": float(np.sqrt(sum_squares / (sample_count * channels))),
        "rms_100ms_windows": windows,
    }


def summarize_frames(rows, fps):
    deltas = [row["mean_abs_rgb_delta"] for row in rows[1:]]
    repeat_runs, current_start = [], None
    for index, row in enumerate(rows + [{"exact_repeat": False}]):
        if row["exact_repeat"] and current_start is None:
            current_start = index - 1
        elif not row["exact_repeat"] and current_start is not None:
            repeat_runs.append({"first_frame": current_start, "last_frame": index - 1,
                                "duration_seconds": (index - current_start) / fps})
            current_start = None
    jumps = sorted(rows[1:], key=lambda row: row["mean_abs_rgb_delta"], reverse=True)[:20]
    return {
        "decoded_frame_count": len(rows),
        "zero_rgb_frames": [row["frame"] for row in rows if row["max_rgb"] == 0],
        "near_black_frame_count": sum(row["mean_luma"] < 0.01 for row in rows),
        "mean_luma_min": min(row["mean_luma"] for row in rows),
        "mean_luma_max": max(row["mean_luma"] for row in rows),
        "mean_motion_delta": float(np.mean(deltas)) if deltas else 0.0,
        "median_motion_delta": float(np.median(deltas)) if deltas else 0.0,
        "max_motion_delta": max(deltas) if deltas else 0.0,
        "exact_repeat_runs": repeat_runs,
        "longest_exact_repeat_seconds": max((row["duration_seconds"] for row in repeat_runs), default=0),
        "largest_frame_changes": jumps,
    }


def thumbnail(frame, max_width=320):
    height, width, _ = frame.shape
    target_width = min(width, max_width)
    target_height = max(1, round(height * target_width / width))
    return frame[np.linspace(0, height - 1, target_height).astype(int)][:,
                 np.linspace(0, width - 1, target_width).astype(int)]


def save_samples(output, samples, fps):
    if not samples:
        return []
    items, tiles = [], []
    for index, frame in sorted(samples.items()):
        filename = f"frame-{index:06d}-{index / fps:08.3f}s.png"
        (output / filename).write_bytes(png_bytes(frame.astype(np.float32) / 255.0))
        tiles.append(thumbnail(frame))
        items.append({"frame": index, "time_seconds": index / fps, "file": filename})
    tile_height, tile_width = tiles[0].shape[:2]
    columns = min(4, len(tiles))
    sheet = np.full((((len(tiles) + columns - 1) // columns) * tile_height,
                     columns * tile_width, 3), 12, dtype=np.uint8)
    for position, frame in enumerate(tiles):
        row, column = divmod(position, columns)
        sheet[row * tile_height:(row + 1) * tile_height,
              column * tile_width:(column + 1) * tile_width] = frame
    (output / "decoded-contact-sheet.png").write_bytes(png_bytes(sheet.astype(np.float32) / 255.0))
    return items


def verify_video(path, output, *, expected_fps=None, expected_duration=None,
                 expected_frames=None, require_audio=False, sample_times=(), sample_count=20):
    path, output = Path(path), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    metadata = probe_media(path)
    videos = [stream for stream in metadata["streams"] if stream["codec_type"] == "video"]
    audios = [stream for stream in metadata["streams"] if stream["codec_type"] == "audio"]
    if not videos:
        raise ValueError("No video stream")
    video = videos[0]
    width, height = int(video["width"]), int(video["height"])
    fps = float(Fraction(video["avg_frame_rate"]))
    if fps <= 0:
        raise ValueError("Positive average video FPS required")
    estimated_frames = expected_frames or int(video.get("nb_frames", 0)) or max(
        1, round(float(video.get("duration", metadata["format"]["duration"])) * fps)
    )
    sample_indices = set(np.linspace(0, estimated_frames - 1, max(2, sample_count)).astype(int))
    sample_indices.update(max(0, round(time * fps)) for time in sample_times)
    samples, rows, previous = {}, [], None
    for index, frame in enumerate(iter_decoded_frames(path, width, height)):
        row = {"frame": index, "time_seconds": index / fps, **pixel_metrics(frame, previous)}
        rows.append(row)
        if index in sample_indices:
            samples[index] = frame.copy()
        previous = frame.copy()
    if not rows:
        raise ValueError("Video stream decoded no frames")
    samples[len(rows) - 1] = previous
    timing = inspect_timestamps(read_frame_timestamps(path), fps)
    audio = decode_audio(path, audios[0]) if audios else None
    duration = len(rows) / fps
    # AAC may expose padding up to one codec packet; this is explicitly reported.
    audio_tolerance = max(1 / fps, 1024 / audio["sample_rate"]) if audio else 0
    audio_start = float(audios[0].get("start_time", 0)) if audios else None
    video_start = float(video.get("start_time", 0))
    declared_audio_duration = float(audios[0]["duration"]) if audios and "duration" in audios[0] else None
    checks = {
        "every_video_frame_decoded": True,
        "decoded_count_matches_timestamp_count": len(rows) == timing["timestamp_count"],
        "timestamps_strictly_increasing": timing["strictly_increasing"],
        "constant_frame_spacing": timing["constant_frame_spacing"],
        "all_frames_have_timestamp": timing["all_frames_have_timestamp"],
        "expected_fps": expected_fps is None or abs(fps - expected_fps) < 1e-6,
        "expected_frame_count": expected_frames is None or len(rows) == expected_frames,
        "expected_duration": expected_duration is None or abs(duration - expected_duration) <= 1 / fps + 1e-6,
        "declared_frame_count": "nb_frames" not in video or int(video["nb_frames"]) == len(rows),
        "required_audio_present": not require_audio or audio is not None,
        "decoded_audio_finite": audio is None or audio["all_samples_finite"],
        "audio_video_start_aligned": audio is None or abs(audio_start - video_start) <= audio_tolerance,
        "audio_video_decoded_duration_aligned": audio is None or abs(audio["decoded_duration_seconds"] - duration) <= audio_tolerance + 1e-6,
        "audio_video_declared_duration_aligned": declared_audio_duration is None or abs(declared_audio_duration - duration) <= audio_tolerance + 1e-6,
    }
    summary = summarize_frames(rows, fps)
    with path.open("rb") as source_file:
        source_sha256 = hashlib.file_digest(source_file, "sha256").hexdigest()
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_file": str(path.resolve()), "source_sha256": source_sha256,
        "video": {"width": width, "height": height, "encoded_fps": fps,
                  "decoded_duration_seconds": duration, "codec": video["codec_name"],
                  "pixel_format": video.get("pix_fmt"), "start_seconds": video_start},
        "checks": checks, "passed_media_integrity": all(checks.values()),
        "frame_summary": summary, "timestamps": timing,
        "audio": audio, "audio_sync_tolerance_seconds": audio_tolerance,
        "samples": save_samples(output, samples, fps),
        "limitations": [
            "FPS is encoded playback cadence, not offline render throughput or real-time performance.",
            "All video pixels are decoded uint8; shader float finiteness needs pre-encoding validation.",
            "Image motion does not independently establish causal audio-reactivity.",
            "Black or repeated frames and large jumps require context and visual review, not automatic aesthetic verdicts.",
            "Audio duration/start alignment verifies stream timing, not perceptual beat synchronization.",
            "Decoded contact sheet reads left-to-right, top-to-bottom in samples order; timestamps are in audit.json.",
        ],
    }
    (output / "audit.json").write_text(json.dumps(report, indent=2) + "\n")
    (output / "per-frame-metrics.json").write_text(json.dumps(rows, indent=2) + "\n")
    (output / "ffprobe.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-fps", type=float)
    parser.add_argument("--expected-duration", type=float)
    parser.add_argument("--expected-frames", type=int)
    parser.add_argument("--require-audio", action="store_true")
    parser.add_argument("--at", type=float, action="append", default=[])
    parser.add_argument("--sample-count", type=int, default=20)
    args = parser.parse_args(argv)
    report = verify_video(args.video, args.output, expected_fps=args.expected_fps,
                          expected_duration=args.expected_duration, expected_frames=args.expected_frames,
                          require_audio=args.require_audio, sample_times=args.at, sample_count=args.sample_count)
    print(json.dumps({"passed_media_integrity": report["passed_media_integrity"],
                      "decoded_frames": report["frame_summary"]["decoded_frame_count"],
                      "video": report["video"], "failed_checks": [key for key, value in report["checks"].items() if not value],
                      "report": str(args.output / "audit.json")}, indent=2))
    return 0 if report["passed_media_integrity"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
