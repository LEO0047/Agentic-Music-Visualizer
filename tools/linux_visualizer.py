#!/usr/bin/env python3
"""Linux music-reactive GLSL player and exact-clock FFmpeg recorder.

Real surfaceless EGL/OpenGL renders the unchanged project fragment shader.
Pure-Python Linux controls are an independent software adapter, not TD. Audio
is original procedural PCM (default) or your PCM WAV. No account, paid API,
external music, device access, TouchDesigner, projectM or image generation.

    .venv/bin/python tools/linux_visualizer.py --seconds 120 --output artifacts/linux-visual/demo
    .venv/bin/python tools/linux_visualizer.py --realtime --seconds 3600 --width 320 --height 180 --output artifacts/linux-visual/live
    .venv/bin/python tools/linux_visualizer.py --benchmark --output artifacts/linux-visual/benchmark

Offline exports every requested frame and matching PCM sample. Realtime uses
a monotonic wall clock, skips missed render slots instead of slowing the show,
and writes status, snapshots and telemetry; it does not play audio to a device.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import select
import shutil
import subprocess
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT, ROOT / "tools"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import numpy as np
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer
from pythonosc.udp_client import SimpleUDPClient

from amv.audio_features import KickDetector, Normalizer, band_energies
from amv.director import History, RuleDirector, strip_private
from amv.features import FeatureBuffer
from amv.osc_io import FeatureReceiver, TDClient
from amv.schema import validate_and_clamp
from amv.sections import SectionDetector
from render_shaders import (PALETTES, PARTICLE_MODES, SCENES, ShaderRenderer,
                            assert_valid_frame, png_bytes)


def current_rss_bytes() -> int:
    try:
        return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)



class TimingStats:
    """Constant-memory cumulative timing histogram (p95 upper bound, 1ms bins)."""
    def __init__(self, budget_s=1 / 30):
        self.count = 0
        self.total = self.maximum = 0.
        self.above_budget = 0
        self.budget_s = budget_s
        self.bins = np.zeros(5002, dtype=np.int64)

    def append(self, seconds):
        self.count += 1
        self.total += seconds
        self.maximum = max(self.maximum, seconds)
        self.above_budget += seconds > self.budget_s
        self.bins[min(5001, max(0, math.ceil(seconds * 1000)))] += 1

    def __len__(self):
        return self.count

    @property
    def p95_upper_ms(self):
        if not self.count:
            return 0.
        index = int(np.searchsorted(np.cumsum(self.bins), math.ceil(self.count * .95)))
        return self.maximum * 1000 if index == 5001 else float(index)


def acceptance_checks(report: dict) -> dict[str, bool]:
    """Separate completing execution from meeting the declared visual target."""
    live = report["mode"] == "realtime-headless"
    frames = report.get("rendered_frames", 0)
    fps = report["target_fps"]
    checks = {
        "execution_completed": report.get("execution_completed", False),
        "source_unchanged_during_run": report.get("source_unchanged_during_run", False),
        "no_errors": not report.get("errors"),
        "all_raw_float_draws_validated": report.get("raw_shader_draws", 0) > 0 and report.get("raw_shader_draws") == report.get("raw_float_frames_validated"),
        "no_black_frames": report.get("black_frames", -1) == 0,
        "feature_udp_complete": report.get("feature_packets_sent", 0) > 0 and report.get("feature_packets_sent") == report.get("feature_packets_received"),
        "control_udp_complete": report.get("control_packets_sent") == report.get("internal_control_packets_received") and report.get("control_packets_rejected", -1) == 0,
        "no_osc_send_errors": report.get("osc_send_errors", -1) == 0,
        "no_one_second_exact_freeze": report.get("longest_exact_repeat_duration_s", 1.) < 1.,
        "resources_released": report.get("resources_released", False),
        "exact_audio_analysis_samples": report.get("exact_audio_sample_count", False),
    }
    if report["director"] != "external":
        checks["director_heartbeat_continuous"] = report.get("heartbeat_stale_frames", -1) == 0
    if report["director"] == "showcase" and report["duration_s"] >= 30:
        checks["all_five_scenes_rendered"] = report.get("all_five_scenes_rendered", False)
        checks["all_five_palettes_rendered"] = set(report.get("palette_render_draws", {})) == set(PALETTES)
    if report["director"] == "showcase" and report["duration_s"] >= 180:
        checks["empty_particle_fallback_exercised"] = report.get("empty_particle_fallback_draws", 0) > 0
    if live:
        checks["full_requested_wall_duration"] = report.get("wall_elapsed_s", 0) >= report["duration_s"]
        checks["at_least_98_percent_render_slots"] = frames / report["expected_frames"] >= .98
        checks["no_half_second_frame_gap"] = report.get("frame_interval_max_ms", 1000) < 500
    else:
        checks["exact_video_frames"] = report.get("exact_frame_count", False)
        checks["exact_exported_pcm_samples"] = report.get("audio", {}).get("samples_per_channel") == report["expected_audio_samples_per_channel"]
    return checks


def exact_counts(seconds: float, fps: int, sample_rate: int) -> tuple[int, int]:
    """Whole-frame duration; audio count rounded to the nearest PCM sample."""
    if not math.isfinite(seconds) or seconds <= 0 or fps <= 0 or sample_rate <= 0:
        raise ValueError("seconds, fps and sample_rate must be finite and positive")
    frames = max(1, round(seconds * fps))
    return frames, round(frames * sample_rate / fps)


class SyntheticPsytrance:
    """Original deterministic, chunk-independent, stereo 145 BPM test music.

    No recordings or artist imitation. Kick, rolling bass, acid-like sequence,
    hats and pads are generated directly from oscillator/envelope equations.
    A 96-second arrangement includes build, breakdown and bass return.
    """
    sample_rate = 48000
    channels = 2

    def __init__(self, bpm: float = 145.0):
        if not math.isfinite(bpm) or bpm <= 0:
            raise ValueError("bpm must be positive")
        self.bpm = bpm

    def read(self, start: int, count: int) -> np.ndarray:
        t = (start + np.arange(count, dtype=np.float64)) / self.sample_rate
        beat = 60 / self.bpm
        age = np.remainder(t, beat)
        step = np.floor(t / (beat / 4)).astype(np.int64)
        step_age = np.remainder(t, beat / 4)
        phase = 2 * np.pi * (46 * age + (120 - 46) * .025 * (1 - np.exp(-age / .025)))
        kick = .62 * np.sin(phase) * np.exp(-age * 24)
        bass_freq = np.array([55, 55, 65.4064, 48.9994])[((step // 16) % 4)]
        bass_phase = 2 * np.pi * bass_freq * t
        bass = (.20 * np.sin(bass_phase) + .045 * np.sin(2 * bass_phase))
        bass *= np.exp(-step_age * 14) * np.minimum(step_age * 1000, 1) * (step % 4 != 0)
        note = np.array([0, 12, 7, 15, 0, 7, 10, 19, 3, 12, 7, 22, 0, 10, 15, 7])[step % 16]
        lead_phase = 2 * np.pi * (220 * np.power(2.0, note / 12)) * t
        lead = .028 * (np.sin(lead_phase) + .4 * np.sin(2 * lead_phase))
        lead *= np.minimum(step_age * 800, 1) * np.exp(-step_age * 28)
        sample = start + np.arange(count, dtype=np.float64)
        noise = 2 * np.mod(np.sin(sample * 12.9898 + 78.233) * 43758.5453, 1) - 1
        hats = .030 * noise * np.exp(-step_age * 90) * (step % 2 == 1)
        pad = .016 * (np.sin(2 * np.pi * 110 * t) + .5 * np.sin(2 * np.pi * 164.81 * t))
        within = np.remainder(t, 96.0)
        build = np.clip((within - 16) / 20, 0, 1)
        level = np.where((within >= 16) & (within < 36), .45 + .55 * build, 1.0)
        quiet = ((within >= 40) & (within < 48)) | ((within >= 72) & (within < 80))
        rhythm = (kick + bass + lead + hats) * level * np.where(quiet, .025, 1.0)
        stereo = np.column_stack((rhythm + pad, rhythm + pad * .85 + lead * .12 * np.sin(t * .3)))
        fade = np.minimum(t / .04, 1.0)
        return np.clip(stereo * fade[:, None], -.98, .98).astype(np.float32)

    def close(self):
        pass


class WavSource:
    """Standard PCM WAV; 8/16/24/32-bit integer, mono or stereo (no resample)."""
    def __init__(self, path: Path):
        self.file = wave.open(str(path), "rb")
        self.sample_rate = self.file.getframerate()
        self.channels = self.file.getnchannels()
        self.sample_width = self.file.getsampwidth()
        self.total_samples = self.file.getnframes()
        if self.channels not in (1, 2) or self.sample_width not in (1, 2, 3, 4):
            self.close()
            raise ValueError("WAV must contain mono/stereo 8/16/24/32-bit integer PCM")

    def read(self, start: int, count: int) -> np.ndarray:
        self.file.setpos(min(start, self.total_samples))
        raw = self.file.readframes(count)
        if self.sample_width == 1:
            data = (np.frombuffer(raw, np.uint8).astype(np.float64) - 128) / 128
        elif self.sample_width == 3:
            b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
            signed = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            signed = (signed ^ 0x800000) - 0x800000
            data = signed.astype(np.float64) / 8388608
        else:
            dtype = "<i2" if self.sample_width == 2 else "<i4"
            data = np.frombuffer(raw, dtype).astype(np.float64) / (2 ** (8 * self.sample_width - 1))
        data = data.reshape(-1, self.channels)
        if len(data) < count:
            data = np.pad(data, ((0, count - len(data)), (0, 0)))
        return data.astype(np.float32)

    def close(self):
        self.file.close()


def write_audio(source, path: Path, samples: int) -> dict:
    peak = 0.0
    with wave.open(str(path), "wb") as output:
        output.setnchannels(source.channels)
        output.setsampwidth(2)
        output.setframerate(source.sample_rate)
        for start in range(0, samples, source.sample_rate):
            block = source.read(start, min(source.sample_rate, samples - start))
            if not np.isfinite(block).all():
                raise ValueError("Non-finite source audio")
            peak = max(peak, float(np.abs(block).max(initial=0)))
            output.writeframes(np.rint(np.clip(block, -1, 1) * 32767).astype("<i2").tobytes())
    return {"samples_per_channel": samples, "channels": source.channels,
            "sample_rate": source.sample_rate, "pcm_peak": peak,
            "duration_s": samples / source.sample_rate, "file": path.name}


class AudioAnalysis:
    """Real production FFT/normalizer/kick/section modules, 100 ms PCM blocks."""
    def __init__(self, source):
        self.source = source
        self.block_size = max(1, round(source.sample_rate / 10))
        self.normalizer = Normalizer()
        self.detector = KickDetector()
        self.sections = SectionDetector()
        self.blocks = self.samples = self.kicks = 0
        self.last = dict(bass=0., mid=0., high=0., energy=0., centroid=0., kick=0.)
        self.section = "steady"
        # Calibrate from a declared two-second reference of this same source,
        # not a made-up feature timeline. Analysis itself is causal thereafter.
        for i in range(20):
            raw = band_energies(source.read(i * self.block_size, self.block_size), source.sample_rate)
            self.normalizer.update({k: raw[k] for k in ("bass", "mid", "high", "energy")})

    def step(self, count: int | None = None) -> tuple[dict, str]:
        count = self.block_size if count is None else count
        raw = band_energies(self.source.read(self.samples, count), self.source.sample_rate)
        self.samples += count
        now = self.samples / self.source.sample_rate
        feat = self.normalizer.update({k: raw[k] for k in ("bass", "mid", "high", "energy")})
        feat["centroid"] = raw["centroid"]
        feat["kick"] = self.detector.update(feat["bass"], t=now)
        self.kicks += feat["kick"]
        self.last = feat
        self.section = self.sections.update(now, feat["energy"], feat["bass"])
        self.blocks += 1
        return feat, self.section


class ControlTransport:
    """Real loopback UDP, ordered callbacks on the GL/render thread."""
    def __init__(self, controls, port=0):
        self.controls = controls
        self.now = 0.0
        self.received = self.rejected = 0
        self.internal_received = self.external_received = 0
        dispatcher = Dispatcher()
        dispatcher.set_default_handler(self._receive, needs_reply_address=True)
        self.server = BlockingOSCUDPServer(("127.0.0.1", port), dispatcher)
        self.server.timeout = .05
        self.port = self.server.server_address[1]
        self.client = TDClient(port=self.port)

    def _receive(self, peer, address, *args):
        self.received += 1
        own_port = self.client._client._sock.getsockname()[1]
        if peer[0] == "127.0.0.1" and peer[1] == own_port:
            self.internal_received += 1
        else:
            self.external_received += 1
        if not args or not self.controls.receive(address, args[0], self.now):
            self.rejected += 1

    def pump(self, now: float, expected: int = 0):
        self.now = now
        target = self.internal_received + expected
        deadline = time.monotonic() + 1
        while True:
            wait = .05 if self.internal_received < target else 0
            ready, _, _ = select.select([self.server.socket], [], [], wait)
            if ready:
                self.server.handle_request()
            elif self.internal_received >= target:
                return
            if time.monotonic() > deadline:
                raise RuntimeError(f"Internal OSC control delivery timeout: {self.internal_received}/{target}")

    def decision(self, decision: dict, now: float):
        before = self.client.sent
        if self.client.send_director(validate_and_clamp(decision)) is None:
            raise RuntimeError(f"OSC send failed: {self.client.last_error}")
        self.pump(now, self.client.sent - before)

    def close(self):
        self.client.close()
        self.server.server_close()


def update_render_controls(controls, now, kick, local_section, director):
    """One declared section authority: external OSC or local PCM analysis."""
    return controls.update(now, kick=kick, section=None if director == "external" else local_section)


def composition_layers(state) -> list[tuple[dict, float]]:
    """Couple discrete crossfade marginals without a 2**4 render explosion.

    The union of cumulative-weight breakpoints gives exact marginal weights.
    A normal simultaneous transition costs two shader draws, not sixteen.
    Interrupted glides preserve each endpoint's weight and remain continuous.
    """
    tables = {}
    edges = {0., 1.}
    for key in ("scene", "palette", "particle_mode", "symmetry"):
        weights = getattr(state, key + "_weights")
        total = sum(float(weight) for _, weight in weights)
        if total <= 0:
            raise ValueError(f"Empty {key} composition weights")
        running = 0.
        table = []
        for value, weight in weights:
            running += float(weight) / total
            table.append((value, running))
            edges.add(min(1., max(0., running)))
        tables[key] = table
    edges = sorted(edges)
    result = []
    for left, right in zip(edges, edges[1:]):
        if right - left < 1e-7:
            continue
        midpoint = (left + right) / 2
        layer = {key: next(value for value, edge in table if edge >= midpoint)
                 for key, table in tables.items()}
        result.append((layer, right - left))
    return result


EMPTY_PARTICLE_FALLBACK_REASON = "particle_field_none_has_no_animated_geometry"


def render_scene_for(requested_scene: str, particle_mode: str) -> str:
    """Keep particles disabled while avoiding the otherwise empty static scene.

    The existing kaleido_mesh branch contains animated non-particle geometry;
    its particle contribution is still disabled by particle_mode=none. This is
    a Linux rendering fallback only. Director decisions, controls and the
    original GLSL stay unchanged, and reports retain both scene identities.
    """
    return "kaleido_mesh" if requested_scene == "particle_field" and particle_mode == "none" else requested_scene


def showcase_particle_mode(segment_number: int) -> str:
    """Alternate populated/particles-off fields in successive five-scene cycles."""
    index = segment_number % len(SCENES)
    if index == 2 and (segment_number // len(SCENES)) % 2:
        return "none"
    return ("spiral", "orbit", "rain", "burst", "spiral")[index]


class VisualComposer:
    """Unchanged real GLSL plus CPU crossfade/feedback; bounded float output."""
    def __init__(self, renderer):
        self.renderer = renderer
        self.previous = None
        self.audio = np.zeros(4, dtype=np.float32)
        self.clock = 0.
        self.draws = 0
        self.validation_count = 0
        self.max_layers = 0
        self.scene_frames = Counter()
        self.requested_scene_frames = Counter()
        self.palette_frames = Counter()
        self.empty_particle_fallback_draws = 0
        self.empty_particle_fallback_output_frames = 0
        self.last_render_layers = []
        self.last_effective_scene_weights = ()
        self.last_fallback_applied = False

    def render(self, state, features: dict, dt: float, kick_pulse: float):
        # 80 ms attack/180 ms release over the real FFT-derived signal.
        target = np.array([features[k] for k in ("bass", "mid", "high", "energy")], np.float32)
        tau = np.where(target > self.audio, .08, .18)
        self.audio += (target - self.audio) * (1 - np.exp(-max(dt, 0) / tau))
        audio = self.audio.copy()
        # uModes.kick is unused by the existing shader. A bounded Linux
        # reflex maps a detected kick onto its existing bass/energy uniforms.
        audio[0] = min(1., audio[0] + .12 * kick_pulse)
        audio[3] = min(1., audio[3] + .06 * kick_pulse)
        self.clock += dt * (.35 + .9 * state.camera_speed)
        layers = composition_layers(state)
        self.max_layers = max(self.max_layers, len(layers))
        composite = None
        self.last_render_layers = []
        self.last_fallback_applied = False
        effective_weights = Counter()
        for layer, weight in layers:
            requested_scene = layer["scene"]
            effective_scene = render_scene_for(requested_scene, layer["particle_mode"])
            fallback_applied = effective_scene != requested_scene
            self.last_fallback_applied |= fallback_applied
            self.empty_particle_fallback_draws += int(fallback_applied)
            self.last_render_layers.append({"requested_scene": requested_scene,
                "effective_scene": effective_scene, "particle_mode": layer["particle_mode"],
                "palette": layer["palette"], "symmetry": layer["symmetry"], "weight": weight,
                "fallback_reason": EMPTY_PARTICLE_FALLBACK_REASON if fallback_applied else None})
            effective_weights[effective_scene] += weight
            frame = self.renderer.render(SCENES.index(effective_scene), time=self.clock,
                audio=tuple(audio), speed=state.camera_speed, symmetry=layer["symmetry"],
                particle_mode=PARTICLE_MODES.index(layer["particle_mode"]),
                palette=PALETTES.index(layer["palette"]), kick=kick_pulse)
            # Every raw float32 draw is checked before quantization.
            assert_valid_frame(frame, patterned=False)
            self.validation_count += 1
            self.draws += 1
            self.scene_frames[effective_scene] += 1
            self.requested_scene_frames[requested_scene] += 1
            self.palette_frames[layer["palette"]] += 1
            if composite is None:
                composite = frame * weight
            else:
                composite += frame * weight
        self.last_effective_scene_weights = tuple(effective_weights.items())
        self.empty_particle_fallback_output_frames += int(self.last_fallback_applied)
        # Feedback is an EMA of successive real rendered frames, not a TD
        # Feedback TOP. 60 Hz reference retention makes it frame-rate aware.
        retain = min(.90, max(0., float(state.feedback) * .85)) ** (dt * 60)
        if self.previous is not None:
            composite = composite * (1 - retain) + self.previous * retain
        composite[..., 3] = 1.
        if not np.isfinite(composite).all():
            raise RuntimeError("Non-finite composed frame")
        self.previous = composite
        return composite


class FFmpegWriter:
    def __init__(self, output: Path, audio: Path, width: int, height: int, fps: int, frames: int):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg is required for video export")
        self.log = output.with_suffix(".ffmpeg.log").open("wb")
        self.command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", f"{width}x{height}",
            "-framerate", str(fps), "-i", "pipe:0", "-i", str(audio),
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "19", "-pix_fmt", "yuv420p", "-threads", "2", "-c:a", "aac",
            "-b:a", "192k", "-t", str(frames / fps), "-movflags", "+faststart", str(output)]
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.log)
        self.frames = 0

    def write(self, frame):
        pixels = np.rint(np.clip(frame[..., :3], 0, 1) * 255).astype(np.uint8)
        self.process.stdin.write(pixels.tobytes())
        self.frames += 1

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        code = self.process.wait(timeout=120)
        self.log.close()
        if code:
            raise RuntimeError(f"FFmpeg failed with exit {code}; see .ffmpeg.log")

    def abort(self):
        self.process.kill()
        self.process.wait(timeout=10)
        self.log.close()



def srt_time(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    second, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{second:02d},{ms:03d}"


def annotate_video(output: Path, captions: list[dict], duration: float) -> Path:
    """Native FFmpeg subtitles, preserving the untouched clean-render master."""
    subtitle_path = output / "captions.srt"
    blocks = []
    events_path = output / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()] if events_path.exists() else []
    for index, row in enumerate(captions):
        end = captions[index + 1]["t"] if index + 1 < len(captions) else duration
        recent = [event for event in events if 0 <= row["t"] - event["t"] < 3]
        cue = ""
        if recent:
            event = recent[-1]
            if event["event"] == "director_decision":
                cue = event["decision"]["transition"]["mode"]
            elif event["event"] == "next_kick_cue":
                cue = "on_next_kick"
            elif event["event"] == "scripted_manual_touch":
                cue = "manual hold 30s"
            elif event["event"] == "audio_section":
                cue = "audio " + event["to"]
        text = row["text"] + (" | cue: " + cue if cue else "")
        blocks.append(f"{index + 1}\n{srt_time(row['t'])} --> {srt_time(end)}\n{text}\n")
    subtitle_path.write_text("\n".join(blocks), encoding="utf-8")
    annotated = output / "visualizer-labeled.mp4"
    # Local generated path, escaped for libavfilter rather than a shell.
    escaped = str(subtitle_path.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    filter_text = "subtitles=filename='" + escaped + "':force_style='FontName=DejaVu Sans,FontSize=11,Outline=1,BorderStyle=3,BackColour=&H90000000,Alignment=2,MarginV=8'"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", str(output / "visualizer.mp4"),
        "-vf", filter_text, "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-threads", "2",
        "-c:a", "copy", "-movflags", "+faststart", str(annotated)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=max(120., duration * 3))
    (output / "annotations.ffmpeg.log").write_text(result.stderr)
    if result.returncode:
        raise RuntimeError("FFmpeg annotation failed; see annotations.ffmpeg.log")
    return annotated


def benchmark(output: Path, frames=30) -> dict:
    rows = []
    for width, height in ((320, 180), (640, 360), (1280, 720)):
        with ShaderRenderer(width, height) as renderer:
            info = renderer.info
            for scene, name in enumerate(SCENES):
                for warmup in range(3):
                    renderer.render(scene, time=warmup / 30)
                times = []
                for i in range(frames):
                    started = time.perf_counter()
                    frame = renderer.render(scene, time=i / 30)
                    assert_valid_frame(frame)
                    times.append(time.perf_counter() - started)
                row = {"width": width, "height": height, "scene": name, "frames": frames,
                       "fps": frames / sum(times), "p95_ms": float(np.percentile(times, 95) * 1000),
                       "float_frames_validated": frames}
                rows.append(row)
                print(json.dumps(row), flush=True)
    report = {"renderer": info, "rows": rows, "measure": "actual draw, readback and float validation",
              "recommended_offline": "640x360 30fps", "recommended_software_realtime": "320x180 30fps"}
    atomic_json(output / "benchmark.json", report)
    return report


def run(args) -> dict:
    from amv.linux_controls import LinuxControlState
    total_started = time.monotonic()
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    source = WavSource(args.wav) if args.wav else SyntheticPsytrance(args.bpm)
    seconds = args.seconds
    if seconds is None:
        seconds = source.total_samples / source.sample_rate if args.wav else 120.
    frames, samples = exact_counts(seconds, args.fps, source.sample_rate)
    duration = frames / args.fps
    if args.wav and samples > source.total_samples + round(source.sample_rate / args.fps):
        source.close()
        raise ValueError("Requested duration exceeds WAV duration; trim --seconds or use a longer file")
    audio_report = None
    writer = renderer = transport = receiver = feature_client = None
    success = False
    report = {"passed": False, "error": None, "started_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "realtime-headless" if args.realtime else "offline-exact-clock", "requested_duration_s": seconds,
        "duration_s": duration, "target_fps": args.fps, "width": args.width, "height": args.height,
        "expected_frames": frames, "expected_audio_samples_per_channel": samples,
        "audio_source": "user PCM WAV" if args.wav else "original procedural Psytrance-like stereo PCM",
        "feature_calibration": "Declared two-second lookahead reference from the same source calibrates normalization; per-frame PCM analysis is causal after that reference, not strict streaming from sample zero",
        "section_authority": "external OSC /feat/section" if args.director == "external" else "local PCM SectionDetector",
        "software_versions": {"python": sys.version, "numpy": np.__version__},
        "ai_calls": 0, "audio_device_playback": False, "touchdesigner_runtime": False,
        "controls": "independent LinuxControlState adapter over actual loopback OSC",
        "projectm": "No external projectM; scene projectm_blend is the shader's native analytic fallback; projectm_mix is tracked but has no external source to mix",
        "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [ROOT / "tools/linux_visualizer.py", ROOT / "amv/linux_controls.py",
                      ROOT / "tools/render_shaders.py", ROOT / "td/shaders/visualizer.frag",
                      ROOT / "amv/audio_features.py", ROOT / "amv/features.py",
                      ROOT / "amv/director.py", ROOT / "amv/sections.py", ROOT / "amv/osc_io.py",
                      ROOT / "amv/schema.py", ROOT / "director_schema.json"]},
        "empty_particle_fallback": "Linux compositor maps particle_field + particle_mode none to the existing animated kaleido_mesh branch with particle_mode still none. Decisions, logical controls and original GLSL are unchanged; counters distinguish requested and effective scenes.",
        "showcase_particle_schedule": "Successive five-scene cycles alternate particle_field rain and particle_field none; all other scene presets are unchanged",
        "director": args.director, "errors": [], "rendered_frames": 0, "skipped_render_slots": 0,
        "feature_packets_sent": 0, "feature_packets_received": 0}
    try:
        if not args.realtime:
            audio_report = write_audio(source, output / "audio.wav", samples)
            report["audio"] = audio_report
        analysis = AudioAnalysis(source)
        controls = LinuxControlState(bpm=args.bpm)
        transport = ControlTransport(controls, args.osc_port)
        media_clock = [0.]
        buffer = FeatureBuffer(clock=lambda: media_clock[0])
        receiver = FeatureReceiver(port=0, buffer=buffer).start()
        feature_client = SimpleUDPClient(*receiver.address)
        renderer = ShaderRenderer(args.width, args.height)
        report["renderer"] = renderer.info
        report["shader_sha256"] = hashlib.sha256(renderer.shader_bytes).hexdigest()
        report["shader_source_modified"] = False
        report["osc_control_endpoint"] = f"127.0.0.1:{transport.port}"
        report["osc_feature_endpoint"] = f"127.0.0.1:{receiver.port}"
        # Mesa compiles branch-specialized kernels lazily on first draw.
        # Warm every scene before starting the realtime measurement clock.
        for scene in range(len(SCENES)):
            assert_valid_frame(renderer.render(scene), patterned=False)
        composer = VisualComposer(renderer)
        if not args.realtime:
            writer = FFmpegWriter(output / "visualizer.mp4", output / "audio.wav", args.width, args.height, args.fps, frames)
        history, director = History(), RuleDirector()
        next_decision = 0.
        next_snapshot = 0.
        next_status = 0.
        section = "steady"
        last_section = section
        render_times, frame_times = TimingStats(1 / args.fps), TimingStats(1 / args.fps)
        image_hashes = set()
        black_frames = 0
        stale_frames = 0
        scenes = Counter()
        requested_scenes = Counter()
        snapshots = []
        kick_pulse = 0.
        manual_done = False
        kick_cue_done = False
        captions = []
        next_caption = 0.
        motion_count = 0
        motion_total = motion_max = 0.
        previous_sample = None
        repeated_run = longest_repeat_run = 0
        repeat_started = None
        longest_repeat_duration = 0.
        previous_digest = None
        previous_media = 0.
        # Start the timed window only after shader compilation and setup.
        report["rss_start_bytes"] = current_rss_bytes()
        start = time.monotonic()
        last_frame_wall = start
        target_index = 0
        last_media = 0.
        with (output / "telemetry.jsonl").open("w") as telemetry, (output / "events.jsonl").open("w") as events:
            print(json.dumps({"event": "started", "mode": report["mode"], "duration_s": duration,
                "control_endpoint": report["osc_control_endpoint"], "renderer": renderer.info}), flush=True)
            while target_index < frames:
                scheduled = target_index / args.fps
                if args.realtime:
                    elapsed = time.monotonic() - start
                    if elapsed >= duration:
                        report["skipped_render_slots"] += frames - target_index
                        break
                    if elapsed < scheduled:
                        time.sleep(scheduled - elapsed)
                    elapsed = time.monotonic() - start
                    overdue = max(0, int((elapsed - scheduled) * args.fps))
                    if overdue:
                        skip = min(overdue, frames - 1 - target_index)
                        target_index += skip
                        report["skipped_render_slots"] += skip
                    scheduled = target_index / args.fps
                now = scheduled
                dt = (now - last_media) if report["rendered_frames"] else 1 / args.fps
                last_media = now
                media_clock[0] = now
                kick = False
                # Causal: only fully elapsed 100ms blocks affect this frame.
                while analysis.samples + analysis.block_size <= min(samples, round(now * source.sample_rate)):
                    feat, section = analysis.step()
                    for key, value in feat.items():
                        feature_client.send_message("/feat/" + key, value)
                        report["feature_packets_sent"] += 1
                    deadline = time.monotonic() + 1
                    while receiver.received < report["feature_packets_sent"]:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Loopback audio feature delivery timed out")
                        time.sleep(.0002)
                    kick = kick or bool(feat["kick"])
                report["feature_packets_received"] = receiver.received
                features = {key: buffer.latest(key, 0.) for key in ("bass", "mid", "high", "energy", "centroid", "kick")}
                if section != last_section:
                    events.write(json.dumps({"t": now, "event": "audio_section", "from": last_section, "to": section}) + "\n")
                    last_section = section
                if args.director != "external" and now + 1e-9 >= next_decision:
                    decision = strip_private(director.decide(buffer.summary(), section, history, now, args.bpm))
                    if args.director == "showcase":
                        segment = min(duration, 120.) / 5
                        segment_number = int(now / max(segment, .001))
                        index = segment_number % len(SCENES)
                        decision.update(scene=SCENES[index], palette=PALETTES[index],
                            particle_mode=showcase_particle_mode(segment_number))
                        decision["transition"] = {"mode": "cut" if now == 0 else "glide", "beats": 4}
                        # Drop demonstrates the prepared same-scene look, so
                        # the five-scene showcase remains inspectable.
                        decision["on_drop"] = {"scene": SCENES[index], "palette": PALETTES[index], "particle_mode": "burst"}
                        next_decision = (segment_number + 1) * segment
                    else:
                        next_decision = now + 18.
                    transport.decision(decision, now)
                    history.append(decision, now)
                    events.write(json.dumps({"t": now, "event": "director_decision", "decision": decision}, ensure_ascii=False) + "\n")
                if args.exercise_controls and not manual_done and now >= duration * .55:
                    before = transport.client.sent
                    transport.client.send("/manual/camera_speed", .12)
                    transport.pump(now, transport.client.sent - before)
                    events.write(json.dumps({"t": now, "event": "scripted_manual_touch", "field": "camera_speed", "value": .12, "freeze_s": 30}) + "\n")
                    manual_done = True
                if args.exercise_controls and not kick_cue_done and now >= duration * .75:
                    before_state = controls.update(now, section=section)
                    cue = strip_private(director.decide(buffer.summary(), section, history, now, args.bpm))
                    cue.update(scene=before_state.scene, palette=before_state.palette,
                               particle_mode=before_state.particle_mode,
                               symmetry=12 if before_state.symmetry != 12 else 6,
                               camera_speed=.85)
                    cue["transition"] = {"mode": "on_next_kick", "beats": 2}
                    transport.decision(cue, now)
                    events.write(json.dumps({"t": now, "event": "next_kick_cue", "decision": cue}) + "\n")
                    kick_cue_done = True
                transport.pump(now)
                state = update_render_controls(controls, now, kick, section, args.director)
                stale_frames += bool(state.heartbeat_stale)
                kick_pulse = 1. if kick else kick_pulse * math.exp(-dt / .10)
                render_start = time.perf_counter()
                frame = composer.render(state, features, dt, kick_pulse)
                render_times.append(time.perf_counter() - render_start)
                if writer:
                    writer.write(frame)
                pixels = np.rint(frame[..., :3] * 255).astype(np.uint8)
                # Hash sparse downsample; full raw float checks occur per draw.
                sampled = pixels[::8, ::8]
                digest = hashlib.sha256(pixels.tobytes()).hexdigest()
                if len(image_hashes) < 2048:
                    image_hashes.add(digest)
                if digest == previous_digest:
                    repeated_run += 1
                    if repeat_started is None:
                        repeat_started = previous_media
                    longest_repeat_duration = max(longest_repeat_duration, now - repeat_started)
                else:
                    repeated_run = 0
                    repeat_started = None
                longest_repeat_run = max(longest_repeat_run, repeated_run)
                previous_digest, previous_media = digest, now
                if previous_sample is not None:
                    delta = float(np.abs(sampled.astype(np.float32) - previous_sample).mean() / 255)
                    motion_count += 1
                    motion_total += delta
                    motion_max = max(motion_max, delta)
                previous_sample = sampled.astype(np.float32)
                black_frames += int(float(frame[..., :3].max()) < .02)
                effective_scene = max(composer.last_effective_scene_weights, key=lambda item: item[1])[0]
                scenes[effective_scene] += 1
                requested_scenes[state.scene_to] += 1
                frame_wall = time.monotonic()
                frame_times.append(frame_wall - last_frame_wall)
                last_frame_wall = frame_wall
                report["rendered_frames"] += 1
                if not args.realtime and now + 1e-9 >= next_caption:
                    frozen = ",".join(state.frozen_fields) or "none"
                    scene_label = ">".join(str(value) for value, weight in state.scene_weights if weight > .01)
                    if composer.last_fallback_applied:
                        effective_label = ">".join(str(value) for value, weight in composer.last_effective_scene_weights if weight > .01)
                        scene_label += " => " + effective_label + " (particles off fallback)"
                    palette_label = ">".join(str(value) for value, weight in state.palette_weights if weight > .01)
                    captions.append({"t": now, "text":
                        f"LINUX actual GLSL | synthetic test audio | {args.director} director (no AI)\n"
                        f"{now:06.1f}s | {scene_label} | {palette_label}\n"
                        f"{state.section} | {state.mode} | manual hold: {frozen}"})
                    next_caption = math.floor(now) + 1
                if now + 1e-9 >= next_snapshot:
                    name = f"frame-{target_index:06d}-{effective_scene}.png"
                    (output / name).write_bytes(png_bytes(frame))
                    snapshots.append({"t": now, "frame_index": target_index, "file": name, "state": asdict(state), "render_layers": composer.last_render_layers})
                    next_snapshot = now + args.snapshot_seconds
                if now + 1e-9 >= next_status:
                    wall = time.monotonic() - start
                    row = {"t": now, "wall_elapsed_s": wall, "frame_index": target_index,
                        "rendered_frames": report["rendered_frames"], "skipped_render_slots": report["skipped_render_slots"],
                        "cumulative_render_fps": len(render_times) / render_times.total,
                        "cumulative_wall_fps": report["rendered_frames"] / max(wall, 1e-9),
                        "features": features, "state": asdict(state), "render_layers": composer.last_render_layers,
                        "empty_particle_fallback_output_frames": composer.empty_particle_fallback_output_frames, "rss_peak_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024, "rss_current_bytes": current_rss_bytes()}
                    telemetry.write(json.dumps(row) + "\n")
                    telemetry.flush()
                    atomic_json(output / "status.json", row)
                    print(json.dumps({k: row[k] for k in ("t", "wall_elapsed_s", "rendered_frames", "skipped_render_slots", "cumulative_render_fps")}), flush=True)
                    next_status = now + args.status_seconds
                target_index += 1
            if args.realtime:
                remaining = start + duration - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
            render_wall = time.monotonic() - start
            # Analyze final tail for exact source sample accounting; no future
            # feature is allowed to influence an earlier rendered frame.
            while analysis.samples < samples:
                analysis.step(min(analysis.block_size, samples - analysis.samples))
            if writer:
                writer.close()
                writer = None
                if not args.no_labels:
                    annotated = annotate_video(output, captions, duration)
                    report["annotated_video"] = annotated.name
            atomic_json(output / "captions.json", {"captions": captions})
            report.update(wall_elapsed_s=render_wall,
                render_fps=report["rendered_frames"] / max(render_times.total, 1e-9),
                wall_fps=report["rendered_frames"] / max(render_wall, 1e-9),
                render_time_p95_ms=render_times.p95_upper_ms,
                render_time_max_ms=render_times.maximum * 1000,
                frame_interval_p95_ms=frame_times.p95_upper_ms,
                frame_interval_max_ms=frame_times.maximum * 1000,
                timing_percentiles="cumulative histogram upper bounds, 1ms resolution",
                raw_shader_draws=composer.draws, raw_float_frames_validated=composer.validation_count,
                max_composition_layers=composer.max_layers, black_frames=black_frames,
                distinct_sampled_frames=len(image_hashes), distinct_sample_capacity=2048, scene_render_draws=dict(composer.scene_frames),
                palette_render_draws=dict(composer.palette_frames), scene_output_frames=dict(scenes),
                requested_scene_render_draws=dict(composer.requested_scene_frames),
                requested_scene_output_frames=dict(requested_scenes),
                empty_particle_fallback_draws=composer.empty_particle_fallback_draws,
                empty_particle_fallback_output_frames=composer.empty_particle_fallback_output_frames,
                scene_accounting="scene_render_draws counts actual shader branches; scene_output_frames counts the dominant effective branch. requested_scene_* preserves director intent; telemetry/snapshots include every weighted requested/effective layer.",
                motion_delta_scope="RGB24 pixels sampled every eighth row and column; exact freeze uses FULL RGB24 frame SHA256 and media timestamps",
                motion_mean_delta=motion_total / max(1, motion_count),
                motion_max_delta=motion_max, longest_exact_repeat_run=longest_repeat_run,
                longest_exact_repeat_duration_s=longest_repeat_duration,
                render_budget_exceeded_frames=render_times.above_budget,
                drop_executions=state.drop_count, controls_accepted_batches=controls.accepted_batches,
                controls_applied_batches=controls.applied_batches,
                shader_clock_s=composer.clock, audio_analysis_blocks=analysis.blocks,
                audio_analysis_samples=analysis.samples, detected_kicks=analysis.kicks,
                section_events=analysis.sections.events, control_packets_sent=transport.client.sent,
                control_packets_received=transport.received, control_packets_rejected=transport.rejected,
                internal_control_packets_received=transport.internal_received,
                external_control_packets_received=transport.external_received,
                external_control_accounting="Received only; external sender's sent count is not observable. Strict sent/received equality applies to this player's own loopback sender.",
                osc_send_errors=transport.client.send_errors, heartbeat_stale_frames=stale_frames,
                rss_peak_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                rss_end_bytes=current_rss_bytes())
            report["all_five_scenes_rendered"] = set(composer.scene_frames) == set(SCENES)
            report["exact_frame_count"] = report["rendered_frames"] == frames
            report["exact_audio_sample_count"] = analysis.samples == samples
            atomic_json(output / "snapshots.json", {"snapshots": snapshots})
        report["artifact_sha256"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in output.iterdir() if path.suffix in (".mp4", ".wav", ".png", ".srt")}
        success = True
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["errors"].append(report["error"])
        raise
    finally:
        cleanup_errors = []
        for name, object_, close in (
            ("encoder", writer, lambda: writer.abort()),
            ("renderer", renderer, lambda: renderer.close()),
            ("controls", transport, lambda: transport.close()),
            ("features", receiver, lambda: receiver.stop()),
            ("feature sender", feature_client, lambda: feature_client._sock.close()),
            ("source", source, lambda: source.close()),
        ):
            if object_ is not None:
                try:
                    close()
                except Exception as exc:
                    cleanup_errors.append(f"{name}: {type(exc).__name__}: {exc}")
        report["errors"].extend(cleanup_errors)
        report["execution_completed"] = success
        report["resources_released"] = not cleanup_errors and (
            renderer is None or renderer.display is None) and (
            transport is None or transport.server.socket.fileno() == -1) and (
            receiver is None or receiver._thread is None and receiver._server.socket.fileno() == -1) and (
            feature_client is None or feature_client._sock.fileno() == -1)
        report["source_unchanged_during_run"] = all(
            path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest
            for name, digest in report["source_sha256"].items() for path in [ROOT / name])
        report["checks"] = acceptance_checks(report)
        report["passed"] = all(report["checks"].values())
        report["qualification"] = ("target_passed" if report["passed"] else
            "realtime_target_failed" if args.realtime and success else "validation_failed")
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        report["total_wall_elapsed_s"] = time.monotonic() - total_started
        atomic_json(output / "report.json", report)
    print(json.dumps({"event": "complete", "report": str(output / "report.json"), "passed": report["passed"],
                      "rendered_frames": report["rendered_frames"], "render_fps": report["render_fps"]}), flush=True)
    return report


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", type=Path, default=ROOT / "artifacts/linux-visual/demo")
    p.add_argument("--seconds", type=float)
    p.add_argument("--width", type=int, default=640)
    p.add_argument("--height", type=int, default=360)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--bpm", type=float, default=145.)
    p.add_argument("--wav", type=Path)
    p.add_argument("--realtime", action="store_true")
    p.add_argument("--benchmark", action="store_true")
    p.add_argument("--director", choices=("showcase", "rule", "external"), default="showcase")
    p.add_argument("--osc-port", type=int, default=0)
    p.add_argument("--snapshot-seconds", type=float, default=12.)
    p.add_argument("--status-seconds", type=float, default=5.)
    p.add_argument("--exercise-controls", action="store_true")
    p.add_argument("--no-labels", action="store_true", help="Skip annotated copy; keep clean GLSL master")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.width <= 0 or args.height <= 0 or args.width % 2 or args.height % 2:
        p.error("width and height must be positive even integers for H.264 yuv420p")
    if args.fps <= 0 or args.snapshot_seconds <= 0 or args.status_seconds <= 0:
        p.error("fps, snapshot-seconds and status-seconds must be positive")
    if not math.isfinite(args.bpm) or args.bpm <= 0:
        p.error("bpm must be finite and positive")
    args.output.mkdir(parents=True, exist_ok=True)
    if args.benchmark:
        benchmark(args.output)
    else:
        report = run(args)
        return 0 if report["passed"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
