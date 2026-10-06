#!/usr/bin/env python3
"""Bounded fake-AI → actual Linux OpenGL pixel integration, never an hour claim.

    .venv/bin/python tools/linux_ai_integration.py --output artifacts/linux-ai-seam

The 220-second production-timing probe uses actual 48 kHz synthetic PCM,
production FFT/normalization, real feature UDP, Sidecar, DirectorLoop,
GPTDirector and CodexClient. Only the AI response is simulated: the subprocess
is hard-wired to checked-in fake_codex.py, regardless of PATH or credentials.
Actual director UDP reaches LinuxControlState on the MAIN render thread, then
VisualComposer and the unchanged GLSL produce float32 pixels on real EGL/GL.

Faults: exit 1, malformed output, 30-second timeout, 25-second freshness
watchdog, same-gpt-mode healthy recovery, manual takeover while a 13-second
call is in flight, and explicit rule mode. Every frame has control provenance,
full RGB24 SHA-256, sampled motion, and float validation. PNGs are actual
rendered snapshots, not generated illustrations. No audio device, real model,
paid request, TouchDesigner, native projectM, desktop access or upload occurs.

There is no accelerated pass: production control glides and 30-second manual
field freezes retain their real durations. EGL is created only by run().
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import threading
import time
import traceback
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "tools"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import numpy as np
from pythonosc.udp_client import SimpleUDPClient

from amv.director import DirectorLoop, GPTDirector, RuleDirector
from amv.linux_controls import LinuxControlState
from amv.osc_io import DIRECTOR_ADDRESSES, FeatureReceiver
from amv.sections import SectionDetector
from amv.sidecar import Sidecar
from cloud_endurance import (FakeSubprocessClient, ObservedBuffer, ObservedTDClient,
                             Phase, Profile, code_manifest)
from linux_visualizer import (AudioAnalysis, ControlTransport, TimingStats,
                              VisualComposer, atomic_json)
from render_shaders import SCENES, ShaderRenderer, assert_valid_frame, png_bytes

PHASES = (
    Phase("gpt_success", 0, 32, "gpt", "ok"),
    Phase("process_failure", 32, 54, "gpt", "fail"),
    Phase("malformed_output", 54, 76, "gpt", "garbage"),
    Phase("hung_subprocess", 76, 118, "gpt", "hang"),
    Phase("gpt_same_mode_recovery", 118, 174, "gpt", "ok"),
    Phase("manual_takeover", 174, 194, "manual", "ok"),
    Phase("explicit_rule", 194, 220, "rule", "ok"),
)
PROFILES = {
    "production": Profile(),
}
# Intentionally exercise the valid, formerly empty particle_field/none pair.
# The logical request stays frozen while the declared Linux safety scene moves.
MANUAL_VALUES = dict(scene="particle_field", palette="infrared", feedback=.18,
                     symmetry=9, camera_speed=.7, particle_mode="none", projectm_mix=.0)
EMPTY_PARTICLE_REASON = "particle_field_none_has_no_animated_geometry"


def phase_at(elapsed: float, profile: Profile) -> Phase:
    """A single bounded cycle. Shutdown remains in explicit rule mode."""
    position = max(0., elapsed) / profile.phase_scale
    return next((phase for phase in PHASES if position < phase.end_s), PHASES[-1])


class SteadyPCM:
    """Chunk-independent 48 kHz stereo fixture with actual bass transients.

    Keeping the arrangement steady deliberately isolates AI recovery from
    obsolete-section rejection. The two-second calibration in AudioAnalysis
    is drawn from this SAME waveform. Features/sections are never fabricated.
    This is a diagnostic tone fixture, not the music showcase soundtrack.
    """
    sample_rate = 48000
    channels = 2

    def __init__(self):
        self.peak = 0.

    def read(self, start: int, count: int) -> np.ndarray:
        sample = start + np.arange(count, dtype=np.int64)
        t = sample / self.sample_rate
        pulse = ((sample // 4800) % 4 == 0).astype(np.float64)
        mono = .45 * ((.32 + .28 * pulse) * np.sin(2 * np.pi * 60 * t)
                      + .45 * np.sin(2 * np.pi * 700 * t)
                      + .20 * np.sin(2 * np.pi * 6000 * t))
        stereo = np.column_stack((mono, mono)).astype(np.float32)
        if not np.isfinite(stereo).all():
            raise ValueError("non-finite fixture PCM")
        self.peak = max(self.peak, float(np.abs(stereo).max(initial=0)))
        return stereo

    def close(self):
        pass


class WireControls(ControlTransport):
    """Observe the real datagrams without replacing the production adapter."""
    def __init__(self, controls):
        self.owner_thread = threading.get_ident()
        self.observation_clock = lambda: self.now
        self.wire_batches: list[dict] = []
        self.pending: dict[str, Any] = {}
        self.address_counts: Counter = Counter()
        super().__init__(controls)

    def _receive(self, peer, address, *args):
        if threading.get_ident() != self.owner_thread:
            raise RuntimeError("Linux controls must only run on the GL owner thread")
        accepted_before = self.controls.accepted_batches
        rejected_before = self.rejected
        super()._receive(peer, address, *args)
        self.address_counts[address] += 1
        if self.rejected != rejected_before:
            self.pending.clear()
            return
        if address == DIRECTOR_ADDRESSES[0]:
            self.pending.clear()
        if address in DIRECTOR_ADDRESSES[:-1]:
            self.pending[address.rsplit("/", 1)[-1]] = args[0]
        elif address == DIRECTOR_ADDRESSES[-1]:
            if self.controls.accepted_batches > accepted_before:
                self.wire_batches.append({"elapsed_s": self.observation_clock(),
                    "control_timestamp_s": self.now,
                    "heartbeat": int(args[0]), "mode": self.controls.mode,
                    "fields": dict(self.pending), "render_thread": self.owner_thread})
            self.pending.clear()


def control_values(state) -> dict:
    """Only appearance controls; heartbeat/section are intentionally excluded."""
    return {**{key + "_weights": getattr(state, key + "_weights")
               for key in ("scene", "palette", "particle_mode", "symmetry")},
            **{key: getattr(state, key) for key in ("feedback", "camera_speed", "projectm_mix")}}


def matches_decision(state: dict, decision: dict) -> bool:
    """Whether real rendered state reached the decision's complete endpoint."""
    for key in ("scene", "palette", "particle_mode", "symmetry"):
        weights = state[key + "_weights"]
        if len(weights) != 1 or weights[0][0] != decision[key] or abs(weights[0][1] - 1.) > 1e-7:
            return False
    return all(abs(state[key] - decision[key]) < 1e-6
               for key in ("feedback", "camera_speed", "projectm_mix"))


def apply_operator_mode(loop, transport, mode: str, now: float) -> float:
    """Atomically hand control ownership across the real UDP boundary.

    The same production publication lock covers mode selection, draining old
    batches, and applying the adapter mode. In particular, an immediate rule
    takeover cannot arrive while the adapter still believes it is manual.
    """
    with loop._lock:
        loop.set_mode(mode)
        mode_returned = loop.clock()
        transport.pump(now)
        before = transport.client.sent
        transport.client.send("/manual/mode", mode)
        if mode == "manual":
            for key, value in MANUAL_VALUES.items():
                transport.client.send("/manual/" + key, value)
        transport.pump(now, transport.client.sent - before)
        return mode_returned


def valid_render_layers(layers: list[dict], state) -> bool:
    """Keep requested control labels distinct from the actual shader branch."""
    if not layers:
        return False
    weights: dict[str, dict] = {key: {} for key in ("scene", "particle_mode", "palette", "symmetry")}
    try:
        for layer in layers:
            requested = layer["requested_scene"]
            fallback = requested == "particle_field" and layer["particle_mode"] == "none"
            expected = "kaleido_mesh" if fallback else requested
            if requested not in SCENES or layer["effective_scene"] != expected:
                return False
            if layer["fallback_reason"] != (EMPTY_PARTICLE_REASON if fallback else None):
                return False
            weight = float(layer["weight"])
            if not math.isfinite(weight) or weight <= 0:
                return False
            for field, totals in weights.items():
                value = requested if field == "scene" else layer[field]
                totals[value] = totals.get(value, 0.) + weight
        for field, totals in weights.items():
            expected_weights = dict(getattr(state, field + "_weights"))
            # composition_layers intentionally omits sub-1e-7 intervals.
            if any(abs(totals.get(key, 0.) - expected_weights.get(key, 0.)) >= 1e-6
                   for key in set(totals) | set(expected_weights)):
                return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


class FrameEvidence:
    """Bounded aggregates plus independently auditable per-frame JSONL."""
    def __init__(self):
        self.frames = 0
        self.black = 0
        self.previous_pixels = None
        self.previous_hash = None
        self.repeat_started = None
        self.longest_repeat_s = 0.
        self.previous_time = None
        self.max_gap_s = 0.
        self.first_by_heartbeat: dict[int, dict] = {}
        self.endpoint_by_heartbeat: dict[int, dict] = {}
        self.changed_by_heartbeat: dict[int, dict] = {}
        self.baseline_by_heartbeat: dict[int, str | None] = {}
        self.previous_control_signature = None
        self.groups: dict[str, dict] = {}
        self.manual_signatures: set[str] = set()
        self.manual_values_match = True
        self.render_layers_validated = 0
        self.requested_scene_draws: Counter = Counter()
        self.effective_scene_draws: Counter = Counter()
        self.fallback_draws = 0
        self.fallback_frames = 0

    def record(self, frame, state, elapsed, phase, publication, inflight_modes, render_layers=None) -> dict:
        if not np.isfinite(frame).all():
            raise ValueError("non-finite composed pixels")
        pixels = np.rint(np.clip(frame[..., :3], 0, 1) * 255).astype(np.uint8)
        digest = hashlib.sha256(pixels.tobytes()).hexdigest()
        sample = pixels[::8, ::8].astype(np.float32)
        delta = float(np.mean(np.abs(sample - self.previous_pixels))) if self.previous_pixels is not None else 0.
        if self.previous_time is not None:
            self.max_gap_s = max(self.max_gap_s, elapsed - self.previous_time)
        if digest == self.previous_hash:
            self.longest_repeat_s = max(self.longest_repeat_s, elapsed - self.repeat_started)
        else:
            self.repeat_started = elapsed
        self.previous_pixels, self.previous_hash, self.previous_time = sample, digest, elapsed
        self.frames += 1
        black = float(frame[..., :3].max()) < .02
        self.black += black
        values = control_values(state)
        signature = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        source = ("manual" if state.mode == "manual" else
                  publication["source"] if publication else "initial defaults")
        layers = [dict(layer) for layer in (render_layers or [])]
        self.render_layers_validated += valid_render_layers(layers, state)
        fallback_applied = any(layer.get("fallback_reason") for layer in layers)
        self.fallback_frames += fallback_applied
        for layer in layers:
            self.requested_scene_draws[layer["requested_scene"]] += 1
            self.effective_scene_draws[layer["effective_scene"]] += 1
            self.fallback_draws += bool(layer.get("fallback_reason"))
        row = {"index": self.frames - 1, "elapsed_s": elapsed, "phase": phase.name,
               "mode": state.mode, "source": source, "heartbeat": state.heartbeat,
               "state": asdict(state), "control_sha256": signature, "rgb24_sha256": digest,
               "motion_mean_abs_rgb24": delta, "rgb_mean": float(frame[..., :3].mean()),
               "rgb_max": float(frame[..., :3].max()), "black": black,
               "inflight_fake_modes": list(inflight_modes),
               "effective_render_layers": layers, "safety_fallback_applied": fallback_applied}
        if publication and state.mode != "manual":
            decision_heartbeat = publication["heartbeat"]
            evidence = {"elapsed_s": elapsed, "frame_index": row["index"],
                        "rgb24_sha256": digest, "source": source,
                        "effective_render_layers": layers,
                        "safety_fallback_applied": fallback_applied}
            self.baseline_by_heartbeat.setdefault(decision_heartbeat, self.previous_control_signature)
            self.first_by_heartbeat.setdefault(decision_heartbeat, evidence)
            if signature != self.baseline_by_heartbeat[decision_heartbeat]:
                self.changed_by_heartbeat.setdefault(decision_heartbeat, evidence)
            if matches_decision(values, publication["decision"]):
                self.endpoint_by_heartbeat.setdefault(decision_heartbeat, evidence)
        self.previous_control_signature = signature
        groups = [phase.name]
        if "hang" in inflight_modes:
            groups.append("actual_hang_inflight")
        if state.mode == "manual":
            groups.append("actual_manual_mode")
            self.manual_signatures.add(signature)
            self.manual_values_match &= matches_decision(values, MANUAL_VALUES)
        if fallback_applied:
            groups.append("empty_particle_safety_fallback")
        for name in groups:
            group = self.groups.setdefault(name, {"frames": 0, "changed_frames": 0,
                "motion_total_rgb24": 0., "first_elapsed_s": elapsed,
                "last_elapsed_s": elapsed, "first_sha256": digest, "last_sha256": digest})
            group["frames"] += 1
            group["changed_frames"] += delta > 0
            group["motion_total_rgb24"] += delta
            group["last_elapsed_s"], group["last_sha256"] = elapsed, digest
        return row


def summarize_provenance(decisions, wire_batches, evidence: FrameEvidence) -> list[dict]:
    wire = {row["heartbeat"]: row for row in wire_batches}
    result = []
    previous = 0.
    for decision in decisions:
        heartbeat = decision["heartbeat"]
        received = wire.get(heartbeat)
        first = evidence.first_by_heartbeat.get(heartbeat)
        endpoint = evidence.endpoint_by_heartbeat.get(heartbeat)
        result.append({"heartbeat": heartbeat, "source": decision["source"],
            "error": decision.get("error"),
            "published_elapsed_s": decision["t"], "decision_latency_s": decision["latency_s"],
            "since_previous_publication_s": decision["t"] - previous,
            "wire_received_elapsed_s": received["elapsed_s"] if received else None,
            "first_frame": first, "endpoint_frame": endpoint,
            "first_changed_frame": evidence.changed_by_heartbeat.get(heartbeat),
            "publication_to_first_frame_s": first["elapsed_s"] - decision["t"] if first else None,
            "decision": decision["decision"]})
        previous = decision["t"]
    return result


def summarize_faults(calls: list[dict], decisions: list[dict]) -> list[dict]:
    """Measure from the actual injected call, not the phase's calendar edge."""
    result = []
    for call in calls:
        if call["fake_mode"] not in ("fail", "garbage", "hang"):
            continue
        start = call["start_elapsed_s"]
        fallback = next((d for d in decisions if d["source"].startswith("rule")
                         and d["published_elapsed_s"] >= start), None)
        row = {"fake_mode": call["fake_mode"], "call_start_elapsed_s": start,
               "call_end_elapsed_s": call.get("end_elapsed_s"),
               "outcome": call.get("outcome"), "error": call.get("error"),
               "fallback_heartbeat": fallback["heartbeat"] if fallback else None}
        if fallback:
            row.update(fallback_source=fallback["source"],
                fresh_age_at_fallback_s=fallback["since_previous_publication_s"],
                call_to_rule_publication_s=fallback["published_elapsed_s"] - start,
                call_to_rule_wire_s=(fallback["wire_received_elapsed_s"] - start
                                    if fallback["wire_received_elapsed_s"] is not None else None),
                call_to_rule_first_frame_s=(fallback["first_frame"]["elapsed_s"] - start
                                           if fallback["first_frame"] else None),
                call_to_rule_endpoint_frame_s=(fallback["endpoint_frame"]["elapsed_s"] - start
                                              if fallback["endpoint_frame"] else None))
        result.append(row)
    return result


def evaluate(report: dict) -> dict[str, bool]:
    """No failed or missing scenario can be relabeled as a complete seam pass."""
    profile = report["profile"]
    calls = report.get("calls", [])
    decisions = report.get("decision_provenance", [])
    mode_events = report.get("phase_events", [])
    manual = next((e for e in mode_events if e["phase"] == "manual_takeover"), None)
    rule = next((e for e in mode_events if e["phase"] == "explicit_rule"), None)
    manual_start = manual["set_mode_returned_s"] if manual else math.inf
    manual_end = rule["set_mode_returned_s"] if rule else -math.inf
    late_calls = [c for c in calls if c["start_elapsed_s"] < manual_start < c.get("end_elapsed_s", -math.inf)
                  < manual_end and c.get("outcome") == "ok"]
    recovery_start = PHASES[4].start_s * profile["phase_scale"]
    recovery_end = PHASES[4].end_s * profile["phase_scale"]
    recovered = [d for d in decisions if recovery_start <= d["published_elapsed_s"] < recovery_end
                 and d["source"] == "gpt" and d["endpoint_frame"] and d["first_changed_frame"]]
    watchdogs = [d for d in decisions if d["source"] == "rule (AI freshness watchdog)"]
    hung = [c for c in calls if c["fake_mode"] == "hang" and c.get("outcome") == "expected_injected_error"]
    groups = report.get("frame_groups", {})
    # The production threshold is exactly 25 s. The 10 Hz watchdog hook and
    # frame-sampled UDP consumer add quantization; declare it, do not hide it.
    watchdog_budget = profile["watchdog_s"] + .1 + 2 / report["target_fps"] + .05

    def fault_reaches_pixels(mode: str, error: str) -> bool:
        return any(c["fake_mode"] == mode and c.get("outcome") == "expected_injected_error"
            and d["source"].startswith("rule") and error in (d.get("error") or "")
            and -.002 <= d["published_elapsed_s"] - c["end_elapsed_s"] <= .25
            and d["first_changed_frame"] is not None and d["endpoint_frame"] is not None
            for c in calls for d in decisions)

    checks = {
        "execution_completed": report.get("execution_completed", False),
        "requested_wall_duration": report.get("active_wall_seconds", 0) >= report["duration_s"],
        "never_claims_full_hour": report.get("full_hour") is False,
        "source_bytes_unchanged": report.get("source_unchanged_during_run", False),
        "no_errors": not report.get("errors"),
        "no_real_model_or_audio_device": report.get("real_model_calls") == 0 and report.get("audio_device_playback") is False,
        "real_egl_renderer": bool(report.get("renderer", {}).get("gl_renderer")),
        "all_raw_float_draws_validated": report.get("raw_draws", 0) > 0 and report.get("raw_draws") == report.get("raw_validated"),
        "every_frame_has_valid_requested_vs_effective_scene_trace": report.get("rendered_frames", 0) > 0 and report.get("render_layers_validated") == report.get("rendered_frames"),
        "actual_scene_draw_totals_match_renderer": report.get("effective_scene_draws") == report.get("renderer_effective_scene_draws") and sum(report.get("effective_scene_draws", {}).values()) == report.get("raw_draws", -1),
        "formerly_empty_particle_pair_exercised": report.get("empty_particle_fallback_frames", 0) > 0 and report.get("empty_particle_fallback_draws", 0) > 0,
        "no_black_frames": report.get("black_frames", -1) == 0,
        "no_one_second_exact_freeze": report.get("longest_exact_repeat_s", math.inf) < 1.,
        "no_half_second_frame_gap": report.get("max_frame_gap_s", math.inf) < .5,
        "at_least_95_percent_render_slots": report.get("rendered_frames", 0) >= report["duration_s"] * report["target_fps"] * .95,
        "feature_udp_complete": report.get("feature_sent", 0) > 0 and report.get("feature_sent") == report.get("feature_received"),
        "control_udp_complete": report.get("control_sent", 0) > 0 and report.get("control_sent") == report.get("control_received"),
        "all_control_batches_applied": len(decisions) > 0 and report.get("control_batches_applied") == len(decisions),
        "no_rejected_controls": report.get("control_rejected", -1) == 0,
        "no_osc_send_errors": report.get("osc_send_errors", -1) == 0,
        "all_decisions_reach_real_frames": bool(decisions) and all(d["first_frame"] is not None for d in decisions),
        "healthy_startup_reaches_pixels": any(d["source"] == "gpt" and d["endpoint_frame"] and d["published_elapsed_s"] < PHASES[1].start_s * profile["phase_scale"] for d in decisions),
        "healthy_subprocess_latency_observed": any(c["phase"] == "gpt_success" and c.get("outcome") == "ok" and c["end_elapsed_s"] - c["start_elapsed_s"] >= profile["ok_delay_s"] for c in calls),
        "exit_fault_reaches_rule_pixels": fault_reaches_pixels("fail", "exited 1"),
        "malformed_fault_reaches_rule_pixels": fault_reaches_pixels("garbage", "not valid JSON"),
        "hang_times_out_at_configured_deadline": bool(hung) and all(
            c.get("error_type") == "CodexError" and "timed out" in c.get("error", "")
            and profile["subprocess_timeout_s"] <= c["end_elapsed_s"] - c["start_elapsed_s"]
            <= profile["subprocess_timeout_s"] + .5 for c in hung),
        "watchdog_publishes_while_ai_hung": any(c["start_elapsed_s"] <= d["published_elapsed_s"] < c["end_elapsed_s"] for c in hung for d in watchdogs),
        "watchdog_bounded_with_declared_tick_quantization": bool(watchdogs) and all(d["since_previous_publication_s"] <= watchdog_budget for d in watchdogs),
        "watchdog_rule_changes_rendered_state_while_hung": any(
            d["endpoint_frame"] and d["first_changed_frame"]
            and c["start_elapsed_s"] <= d["published_elapsed_s"]
            <= d["endpoint_frame"]["elapsed_s"] < c["end_elapsed_s"]
            for c in hung for d in watchdogs),
        "watchdog_first_render_under_30_second_outage_budget": bool(watchdogs) and all(
            d["first_frame"] and d["since_previous_publication_s"] + d["publication_to_first_frame_s"] < 30.
            for d in watchdogs),
        "watchdog_changes_render_controls_under_30_second_outage_budget": bool(watchdogs) and all(
            d["first_changed_frame"] and d["since_previous_publication_s"]
            + d["first_changed_frame"]["elapsed_s"] - d["published_elapsed_s"] < 30.
            for d in watchdogs),
        "same_mode_gpt_recovery_reaches_pixels": bool(recovered) and all(e["director_mode"] == "gpt" for e in mode_events if e["elapsed_s"] < recovery_end),
        "manual_takes_over_active_call": manual is not None and manual["inflight_fake_subprocesses"] > 0,
        "late_healthy_reply_completes_during_manual": bool(late_calls),
        "no_late_manual_publications": not any(p["mode"] == "manual" or manual_start <= p["elapsed_s"] < manual_end for p in report.get("publications", [])),
        "manual_values_remain_fixed": report.get("manual_control_signatures") == 1 and report.get("manual_values_match", False),
        "explicit_rule_reaches_pixels": rule is not None and any(d["source"] == "rule" and d["published_elapsed_s"] >= manual_end and d["endpoint_frame"] for d in decisions),
        "resources_released": report.get("resources_released", False),
        "single_fake_subprocess_inflight": report.get("peak_fake_subprocesses") == 1,
    }
    for group in ("actual_hang_inflight", "actual_manual_mode", "empty_particle_safety_fallback"):
        values = groups.get(group, {})
        checks[group + "_visuals_keep_moving"] = (values.get("frames", 0) > 2
            and values.get("changed_frames", 0) >= values.get("frames", 0) * .9
            and values.get("motion_total_rgb24", 0) > 0
            and values.get("first_sha256") != values.get("last_sha256"))
    return checks


def run(output: Path, *, profile: Profile = PROFILES["production"], width=320, height=180, fps=30) -> dict:
    if width <= 0 or height <= 0 or fps <= 0:
        raise ValueError("width, height and fps must be positive")
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to overwrite previous evidence: {output}")
    output.mkdir(parents=True, exist_ok=True)
    (output / "snapshots").mkdir()
    duration = PHASES[-1].end_s * profile.phase_scale
    manifest = code_manifest()
    report = {"status": "running", "passed": False, "full_hour": False,
        "started_utc": datetime.now(timezone.utc).isoformat(), "duration_s": duration,
        "profile": asdict(profile), "production_timing": profile == PROFILES["production"],
        "width": width, "height": height, "target_fps": fps, "errors": [],
        "scope": "simulated AI subprocess; actual PCM extraction, loopback OSC, sidecar and EGL/GL pixels",
        "audio_fixture": "steady 48 kHz diagnostic tones and bass transients; two-second same-source calibration",
        "scene_provenance": "Frame state is the logical control request; effective_render_layers records the actual drawn GLSL branch. particle_field/none preserves particles-off but uses the declared kaleido_mesh safety fallback.",
        "credentials_accessed": False, "real_model_calls": 0, "audio_device_playback": False,
        "touchdesigner_runtime": False, "native_projectm": False,
        "watchdog_timing": "25 s production threshold, sampled by a 10 Hz hook then next render frame; exact observed delays are recorded",
        "subprocess_timeout_completion_tolerance_s": .5,
        "code_start": manifest}
    atomic_json(output / "report.json", report)
    baseline_threads = {thread.ident for thread in threading.enumerate()}
    stop = threading.Event()
    data_lock = threading.Lock()
    features = dict(bass=0., mid=0., high=0., energy=0., centroid=0., kick=0.)
    receiver = sender = transport = td = renderer = loop = sidecar = client = None
    feeder = sidecar_thread = None
    source = SteadyPCM()
    evidence = FrameEvidence()
    composer = None
    started = active_end = None
    feature_sent = 0
    snapshots = []
    phase_events = []
    snapshot_keys = set()
    next_progress = 0.
    timing = TimingStats(1 / fps)
    human_log = (output / "runtime.log").open("w")
    frame_log = (output / "frames.jsonl").open("w")
    try:
        analysis = AudioAnalysis(source)
        controls = LinuxControlState()
        transport = WireControls(controls)
        buffer = ObservedBuffer()
        receiver = FeatureReceiver(port=0, buffer=buffer)
        sender = SimpleUDPClient(*receiver.address)
        renderer = ShaderRenderer(width, height)
        report["renderer"] = renderer.info
        # All GL warmup occurs before the real-time measurement epoch.
        for scene in range(len(SCENES)):
            assert_valid_frame(renderer.render(scene), patterned=False)
        composer = VisualComposer(renderer)
        started = time.monotonic()
        elapsed = lambda: time.monotonic() - started
        transport.observation_clock = elapsed
        td = ObservedTDClient("127.0.0.1", transport.port, lambda: loop.mode if loop else "starting", started)
        client = FakeSubprocessClient(output, profile, lambda: (0, phase_at(elapsed(), profile)), started)
        loop = DirectorLoop(td, GPTDirector(client, RuleDirector(seed=736)),
            period_s=profile.period_s, min_interval_s=profile.min_interval_s,
            clock=elapsed, wall_clock=time.monotonic, out=human_log,
            log_path=output / "decisions.jsonl")
        loop.fallback_after_s = profile.watchdog_s
        loop.startup_check()  # Explicit fake --version only; no AI quota.
        sidecar = Sidecar(buffer, SectionDetector(), td, log_path=output / "sections.jsonl",
                          status_hz=profile.status_hz, on_tick=loop.on_tick,
                          should_stop=stop.is_set, out=human_log)

        def feed():
            nonlocal features, feature_sent
            try:
                deadline = time.monotonic()
                while not stop.is_set() and elapsed() < duration:
                    current, _ = analysis.step()
                    with data_lock:
                        features = dict(current)
                    for key, value in current.items():
                        sender.send_message("/feat/" + key, float(value))
                        feature_sent += 1
                    deadline += .1
                    stop.wait(max(0., deadline - time.monotonic()))
            except Exception:
                report["errors"].append("PCM feeder: " + traceback.format_exc())
                stop.set()

        def run_sidecar():
            try:
                sidecar.run(duration=duration)
            except Exception:
                report["errors"].append("Sidecar: " + traceback.format_exc())
                stop.set()

        receiver.start()
        feeder = threading.Thread(target=feed, name="linux-ai-pcm", daemon=True)
        sidecar_thread = threading.Thread(target=run_sidecar, name="linux-ai-sidecar", daemon=True)
        feeder.start()
        sidecar_thread.start()
        previous_phase = None
        previous_frame = 0.
        slot = 0
        while elapsed() < duration and not stop.is_set():
            if (output / "STOP").exists():
                raise InterruptedError("operator STOP file")
            deadline = started + slot / fps
            remaining = deadline - time.monotonic()
            if remaining > 0:
                stop.wait(remaining)
            now = elapsed()
            if now >= duration or stop.is_set():
                break
            phase = phase_at(now, profile)
            with client.calls_lock:
                active = client.active
            if phase.name != previous_phase and (phase.director_mode != "manual" or active):
                mode_returned = apply_operator_mode(loop, transport, phase.director_mode, now)
                event = {"elapsed_s": now, "phase": phase.name,
                    "director_mode": phase.director_mode, "fake_mode": phase.fake_mode,
                    "set_mode_returned_s": mode_returned, "inflight_fake_subprocesses": active}
                phase_events.append(event)
                previous_phase = phase.name
                print(json.dumps({"event": "phase", **event}), flush=True)
            transport.pump(now)
            with data_lock:
                current = dict(features)
            state = controls.update(now, kick=bool(current["kick"]))
            render_started = time.monotonic()
            frame = composer.render(state, current, max(1 / fps, now - previous_frame), float(current["kick"]))
            timing.append(time.monotonic() - render_started)
            with loop._lock:
                publications = {row["heartbeat"]: dict(row) for row in loop.decisions}
            with client.calls_lock:
                inflight = [row["fake_mode"] for row in client.calls if "end_elapsed_s" not in row]
            publication = next((value for heartbeat, value in reversed(publications.items())
                                if state.heartbeat is not None and heartbeat <= state.heartbeat), None)
            row = evidence.record(frame, state, elapsed(), phase, publication, inflight,
                                  composer.last_render_layers)
            row["control_timestamp_s"] = now
            frame_log.write(json.dumps(row) + "\n")
            # Small, useful evidence set: phase entry, source changes/decision
            # endpoints, hang in-flight, and manual mode. Never a fake preview.
            keys = ["phase:" + phase.name]
            if state.heartbeat in evidence.endpoint_by_heartbeat:
                keys.append("endpoint:" + str(state.heartbeat))
            if "hang" in inflight:
                keys.append("hung:" + str(len(client.calls)))
            if state.mode == "manual":
                keys.append("manual")
            new_keys = [key for key in keys if key not in snapshot_keys]
            if new_keys:
                name = f"snapshots/{evidence.frames - 1:06d}-{phase.name}.png"
                (output / name).write_bytes(png_bytes(frame))
                snapshots.append({"file": name, "keys": new_keys, **row})
                snapshot_keys.update(new_keys)
            if now >= next_progress:
                frame_log.flush()
                atomic_json(output / "progress.json", {"elapsed_s": now, "phase": phase.name,
                    "frames": evidence.frames, "decisions": len(publications),
                    "last_source": row["source"], "fake_calls": len(client.calls),
                    "errors": report["errors"]})
                next_progress = now + 5
            previous_frame = now
            slot = max(slot + 1, int(elapsed() * fps) + 1)
        active_end = time.monotonic()
        report["execution_completed"] = elapsed() >= duration and not stop.is_set()
    except (Exception, KeyboardInterrupt):
        report["errors"].append(traceback.format_exc())
        report["execution_completed"] = False
        active_end = time.monotonic()
    finally:
        stop.set()
        def cleanup(name, action):
            try:
                action()
            except Exception as exc:
                report["errors"].append(f"cleanup {name}: {type(exc).__name__}: {exc}")

        for thread in (feeder, sidecar_thread):
            if thread is not None:
                cleanup(thread.name, lambda: thread.join(3))
        if loop is not None:
            cleanup("director", lambda: loop.close(timeout=profile.subprocess_timeout_s + 2))
        # No producers remain. Drain all controls before comparing UDP totals.
        if transport is not None and started is not None:
            cleanup("drain controls", lambda: transport.pump(time.monotonic() - started))
        if receiver is not None:
            time.sleep(.1)
            cleanup("feature receiver", receiver.stop)
        # Close every live resource BEFORE serialization or artifact writes.
        # Even a full disk or broken evidence sink must not leave GL/UDP open.
        for name, object_, close in (
            ("renderer", renderer, lambda: renderer.close()),
            ("controls", transport, lambda: transport.close()),
            ("director UDP", td, lambda: td.close()),
            ("feature UDP", sender, lambda: sender._sock.close()),
            ("sidecar", sidecar, lambda: sidecar.close()),
            ("source", source, lambda: source.close()),
            ("frame log", frame_log, frame_log.close),
            ("runtime log", human_log, human_log.close),
        ):
            if object_ is not None:
                cleanup(name, close)
        if transport is not None:
            report.update(control_sent=(td.sent if td else 0) + transport.client.sent,
                control_received=transport.received, control_rejected=transport.rejected,
                control_batches_applied=transport.controls.applied_batches,
                osc_send_errors=(td.send_errors if td else 0) + transport.client.send_errors)
            try:
                atomic_json(output / "wire-controls.json", {"batches": transport.wire_batches,
                    "address_counts": dict(transport.address_counts)})
            except Exception as exc:
                report["errors"].append(f"wire evidence: {type(exc).__name__}: {exc}")
        report.update(feature_sent=feature_sent,
            feature_received=receiver.received if receiver else 0,
            phase_events=phase_events, snapshots=snapshots,
            active_wall_seconds=(active_end - started) if started is not None else 0.,
            rendered_frames=evidence.frames, black_frames=evidence.black,
            max_frame_gap_s=evidence.max_gap_s, longest_exact_repeat_s=evidence.longest_repeat_s,
            frame_groups=evidence.groups, manual_control_signatures=len(evidence.manual_signatures),
            manual_values_match=evidence.manual_values_match,
            render_layers_validated=evidence.render_layers_validated,
            requested_scene_draws=dict(evidence.requested_scene_draws),
            effective_scene_draws=dict(evidence.effective_scene_draws),
            renderer_effective_scene_draws=dict(composer.scene_frames) if composer else {},
            empty_particle_fallback_draws=evidence.fallback_draws,
            empty_particle_fallback_frames=evidence.fallback_frames,
            raw_draws=composer.draws if composer else 0, raw_validated=composer.validation_count if composer else 0,
            render_p95_upper_ms=timing.p95_upper_ms,
            decisions=list(loop.decisions) if loop else [], publications=list(td.publications) if td else [],
            calls=list(client.calls) if client else [], peak_fake_subprocesses=client.peak_active if client else 0,
            feature_buffer_metrics=buffer.metrics() if receiver else {},
            pcm_peak=source.peak, audio_analysis_blocks=analysis.blocks if receiver else 0,
            audio_analysis_samples=analysis.samples if receiver else 0,
            sidecar_sections=sidecar.transitions if sidecar else [])
        report["decision_provenance"] = summarize_provenance(report["decisions"], transport.wire_batches if transport else [], evidence)
        report["fault_timings"] = summarize_faults(report["calls"], report["decision_provenance"])
        report["alive_threads"] = [t.name for t in threading.enumerate()
            if t.ident not in baseline_threads and t.is_alive()]
        report["resources_released"] = (not report["alive_threads"]
            and (client is None or client.active == 0)
            and (renderer is None or renderer.display is None)
            and (transport is None or transport.server.socket.fileno() == -1)
            and (receiver is None or receiver._server.socket.fileno() == -1)
            and (sender is None or sender._sock.fileno() == -1))
        report["source_unchanged_during_run"] = all((ROOT / name).is_file()
            and hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
            for name, digest in manifest["files"].items())
        report["checks"] = evaluate(report)
        report["passed"] = all(report["checks"].values())
        report["status"] = "passed" if report["passed"] else "failed"
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        atomic_json(output / "report.json", report)
    print(json.dumps({"event": "complete", "passed": report["passed"],
        "active_wall_seconds": report["active_wall_seconds"], "frames": evidence.frames,
        "failed_checks": [key for key, value in report["checks"].items() if not value],
        "report": str(output / "report.json")}), flush=True)
    return report


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output", type=Path, default=ROOT / "artifacts/linux-ai-integration")
    result.add_argument("--profile", choices=PROFILES, default="production")
    result.add_argument("--width", type=int, default=320)
    result.add_argument("--height", type=int, default=180)
    result.add_argument("--fps", type=int, default=30)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    report = run(args.output, profile=PROFILES[args.profile], width=args.width, height=args.height, fps=args.fps)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
