#!/usr/bin/env python3
"""Credential-free, real-wall-clock software endurance test.

    python tools/cloud_endurance.py --duration-seconds 12 --profile smoke
    python tools/cloud_endurance.py --duration-seconds 3600 --output artifacts/cloud-verification/endurance/hour

This never discovers or starts a real Codex binary. Synthetic 48 kHz PCM goes
through the production numpy feature extractors, real localhost OSC ingestion,
Sidecar and DirectorLoop, a CodexClient invoking ONLY tools/fake_codex.py, and a
real TDClient UDP link ending in an instrumented software sink. There is no
TouchDesigner runtime, audio device, paid AI request, GPU/FPS or MIDI hardware.
Production timing uses 13s fake-success latency and 304s repeated fault cycles.
The smoke profile shortens configuration intervals; it does NOT scale time.
Only at least 3600 actually elapsed monotonic seconds earns full_hour=true.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
import threading
import time
import traceback
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import jsonschema  # noqa: E402
import numpy as np  # noqa: E402
from pythonosc.dispatcher import Dispatcher  # noqa: E402
from pythonosc.osc_server import BlockingOSCUDPServer  # noqa: E402
from pythonosc.udp_client import SimpleUDPClient  # noqa: E402

from amv.audio_features import KickDetector, Normalizer, band_energies  # noqa: E402
from amv.codex_client import CodexClient  # noqa: E402
from amv.director import DirectorLoop, GPTDirector, RuleDirector  # noqa: E402
from amv.features import FeatureBuffer  # noqa: E402
from amv.osc_io import DIRECTOR_ADDRESSES, FeatureReceiver, TDClient  # noqa: E402
from amv.schema import load_schema, validate_and_clamp  # noqa: E402
from amv.sections import SectionDetector  # noqa: E402
from amv.sidecar import Sidecar  # noqa: E402

FAKE_CODEX = ROOT / "tools/fake_codex.py"
FEATURE_HZ = 10.0
FEATURE_KEYS = ("bass", "mid", "high", "energy", "centroid", "kick")
# Executes the checked-in fake in a new interpreter, with child-only env changes.
# No PATH lookup, credential inspection, or real Codex entry point is possible.
FAKE_BOOTSTRAP = (
    "import os,runpy,sys; p,m,d=sys.argv[1:4]; "
    "os.environ['AMV_FAKE_CODEX_MODE']=m; "
    "os.environ['AMV_FAKE_CODEX_DELAY']=d; "
    "sys.argv=[p]+sys.argv[4:]; runpy.run_path(p,run_name='__main__')"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Profile:
    name: str = "production-timing"
    period_s: float = 18.0
    min_interval_s: float = 6.0
    watchdog_s: float = 25.0
    subprocess_timeout_s: float = 30.0
    phase_scale: float = 1.0
    status_hz: float = 1.0
    ok_delay_s: float = 13.0


PROFILES = {
    "production": Profile(),
    "smoke": Profile("short-software-smoke", 0.5, 0.2, 0.8, 1.2, 1 / 24, 10, .02),
}


@dataclass(frozen=True)
class Phase:
    name: str
    start_s: float
    end_s: float
    director_mode: str
    fake_mode: str


PHASES = (
    Phase("gpt_success", 0, 48, "gpt", "ok"),
    Phase("process_failure", 48, 72, "gpt", "fail"),
    Phase("malformed_output", 72, 96, "gpt", "garbage"),
    Phase("hung_subprocess", 96, 146, "gpt", "hang"),
    Phase("gpt_same_mode_recovery", 146, 210, "gpt", "ok"),
    Phase("manual_takeover", 210, 232, "manual", "ok"),
    Phase("explicit_rule", 232, 256, "rule", "ok"),
    Phase("gpt_recovery", 256, 304, "gpt", "ok"),
)


def phase_at(elapsed: float, profile: Profile) -> tuple[int, Phase]:
    position = max(0.0, elapsed) / profile.phase_scale
    cycle = int(position // PHASES[-1].end_s)
    within = position % PHASES[-1].end_s
    return cycle, next(p for p in PHASES if p.start_s <= within < p.end_s)


def recovery_fixture_at(elapsed: float, profile: Profile) -> tuple[int, Phase] | None:
    """Recovery PCM starts 30 fixture seconds early, allowing real settling."""
    position = max(0.0, elapsed) / profile.phase_scale
    cycle = int(position // PHASES[-1].end_s)
    within = position % PHASES[-1].end_s
    for phase in PHASES:
        if phase.name in {"gpt_recovery", "gpt_same_mode_recovery"} and phase.start_s - 30 <= within < phase.end_s:
            return cycle, phase
    return None


def code_manifest() -> dict:
    paths = [ROOT / "director_schema.json", ROOT / "pyproject.toml", ROOT / "uv.lock"]
    for folder in ("amv", "td", "tools", "tests"):
        paths.extend(p for p in (ROOT / folder).rglob("*")
                     if p.is_file() and p.suffix in {".py", ".glsl", ".frag", ".vert"})
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(set(paths)) if p.is_file()}
    combined = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                          text=True, timeout=5, check=False).stdout.strip()
    return {"sha256": combined, "git_head": head, "files": hashes}


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def os_thread_count() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("Threads:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def rss_bytes() -> int:
    try:
        return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)


class PCMSource:
    """Deterministic synthetic music, real 100 ms blocks, no audio device I/O."""

    def __init__(self) -> None:
        self.sample_rate = 48000
        self.block_size = 4800
        t = np.arange(self.block_size) / self.sample_rate
        self.tones = np.array([np.sin(2 * np.pi * f * t) for f in (60, 700, 6000)])
        self.normalizer = Normalizer()
        self.kicks = KickDetector()
        # A declared synthetic calibration block prevents quiet startup from
        # becoming full-scale solely because it is the first observed block.
        self.normalizer.update(band_energies(.45 * (np.array([.9, .8, .4]) @ self.tones),
                                             self.sample_rate))
        self.blocks = 0
        self.raw_peak = 0.0
        self.recovery_calibrations: list[dict] = []

    def calibrate_steady(self) -> None:
        """Prime fixture peaks from its actual steady arrangement's peak PCM.

        This removes a preceding louder fixture's decaying maximum; it never
        changes production extraction, detector history, or received features.
        The real SectionDetector still gets its normal history-settling time.
        """
        peak_block = .45 * (np.array([.60, .45, .20]) @ self.tones)
        self.normalizer.reset()
        self.normalizer.update(band_energies(peak_block, self.sample_rate))

    def features(self, elapsed: float, *, music_position_s: float | None = None) -> dict[str, float]:
        # A bounded recovery probe can hold the arrangement steady while the
        # PCM blocks, kick transients, extraction and wall clock keep running.
        t = (elapsed if music_position_s is None else music_position_s) % 120
        pulse = 1.0 if int(elapsed * FEATURE_HZ) % 4 == 0 else 0.0
        if t < 8 or 90 <= t < 100:
            amps = (.025, .075, .03)
        elif 40 <= t < 65:
            rise = (t - 40) / 25
            amps = (.16, .40 + .45 * rise, .12 + .20 * rise)
        elif 8 <= t < 18 or 65 <= t < 75 or t >= 100:
            amps = (.65 + .25 * pulse, .70, .30)
        else:
            amps = (.32 + .28 * pulse, .45, .20)
        block = .45 * (np.array(amps) @ self.tones)
        self.raw_peak = max(self.raw_peak, float(np.max(np.abs(block))))
        raw = band_energies(block, self.sample_rate)
        features = self.normalizer.update({k: v for k, v in raw.items() if k != "centroid"})
        features["centroid"] = raw["centroid"]  # OSC centroid contract is Hz.
        features["kick"] = float(self.kicks.update(features["bass"], t=elapsed))
        self.blocks += 1
        return features


class ObservedBuffer(FeatureBuffer):
    def __init__(self) -> None:
        super().__init__()
        self.metrics_lock = threading.Lock()
        self.counts: Counter = Counter()
        self.last_received: dict[str, float] = {}
        self.max_gaps: dict[str, float] = {}

    def push(self, key: str, value: float, t: float | None = None) -> None:
        super().push(key, value, t)
        now = time.monotonic()
        with self.metrics_lock:
            last = self.last_received.get(key)
            if last is not None:
                self.max_gaps[key] = max(self.max_gaps.get(key, 0), now - last)
            self.last_received[key] = now
            self.counts[key] += 1

    def metrics(self) -> dict:
        with self.metrics_lock:
            return {"counts": dict(self.counts), "max_gaps_s": dict(self.max_gaps)}


class FakeSubprocessClient(CodexClient):
    """Production CodexClient parse/timeout behavior with a hard-wired free fake."""

    def __init__(self, output: Path, profile: Profile, phase: Callable[[], tuple[int, Phase]],
                 started: float) -> None:
        super().__init__(binary=FAKE_CODEX, cwd=output / "empty-codex-cwd",
                         timeout=profile.subprocess_timeout_s,
                         schema_path=ROOT / "director_schema.json")
        self.profile, self.phase, self.started = profile, phase, started
        self.calls: list[dict] = []
        self.calls_lock = threading.Lock()
        self.active = 0
        self.peak_active = 0
        self.local = threading.local()

    def build_argv(self, prompt: str, out_path: str | os.PathLike[str]) -> list[str]:
        mode = getattr(self.local, "mode", "ok")
        argv = super().build_argv(prompt, out_path)
        delay = getattr(self.local, "delay", self.profile.ok_delay_s if mode == "ok" else .02)
        return [sys.executable, "-c", FAKE_BOOTSTRAP, str(FAKE_CODEX), mode, str(delay), *argv[1:]]

    def version(self) -> str:
        # The same explicit fake, independent of environment/PATH credentials.
        return subprocess.run([sys.executable, str(FAKE_CODEX), "--version"],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=self.timeout, check=True).stdout.strip()

    def decide(self, prompt: str) -> dict:
        cycle, phase = self.phase()
        self.local.mode = phase.fake_mode
        self.local.delay = self.profile.ok_delay_s if phase.fake_mode == "ok" else .02
        # A short smoke still needs an observable live subprocess at manual
        # takeover. Only this explicit handover probe delays fake success .3s.
        if phase.name == "manual_takeover" and self.profile.name != "production-timing":
            self.local.delay = max(.3, self.local.delay)
        call = {"start_elapsed_s": time.monotonic() - self.started,
                "cycle": cycle, "phase": phase.name, "fake_mode": phase.fake_mode,
                "configured_delay_s": self.local.delay}
        with self.calls_lock:
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            self.calls.append(call)
        try:
            result = super().decide(prompt)
            call["outcome"] = "ok"
            return result
        except Exception as exc:
            call.update(outcome="expected_injected_error" if phase.fake_mode != "ok" else "error",
                        error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            with self.calls_lock:
                call["end_elapsed_s"] = time.monotonic() - self.started
                self.active -= 1


class UDPSink:
    """Ordered OSC collector, not a TouchDesigner callback or runtime emulation."""

    def __init__(self, output: Path, started: float) -> None:
        self.started = started
        self.lock = threading.Lock()
        self.messages: Counter = Counter()
        self.heartbeats: list[tuple[float, int]] = []
        self.decisions: list[dict] = []
        self.sections: Counter = Counter()
        self.errors: list[str] = []
        self.pending: dict[str, Any] = {}
        self.expected = {p.rsplit("/", 1)[-1] for p in DIRECTOR_ADDRESSES[:-1]}
        self.validator = jsonschema.Draft202012Validator(load_schema())
        self.log = (output / "wire-decisions.jsonl").open("w", encoding="utf-8")
        dispatcher = Dispatcher()
        dispatcher.set_default_handler(self.on_message)
        self.server = BlockingOSCUDPServer(("127.0.0.1", 0), dispatcher)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .02},
                                       name="endurance-udp-sink", daemon=True)

    def on_message(self, address: str, *args: Any) -> None:
        with self.lock:
            self.messages[address] += 1
            if not args:
                self.errors.append(f"no arguments: {address}")
                return
            value = args[0]
            if address == "/feat/section":
                self.sections[str(value)] += 1
            elif address == "/director/heartbeat":
                self.heartbeats.append((time.monotonic() - self.started, int(value)))
                if self.pending:
                    try:
                        if set(self.pending) != self.expected:
                            raise ValueError(f"incomplete UDP decision: {sorted(self.pending)}")
                        decision = {k: v for k, v in self.pending.items()
                                    if k not in {"transition_mode", "transition_beats", "on_drop"}}
                        decision.update(transition={"mode": self.pending["transition_mode"],
                                                    "beats": self.pending["transition_beats"]},
                                        on_drop=json.loads(self.pending["on_drop"]), intent="")
                        # OSC float32 may turn the legal 0.98 boundary into
                        # 0.980000019. Permit only that tiny transport rounding.
                        clean = validate_and_clamp(decision)
                        for key in ("feedback", "camera_speed", "projectm_mix"):
                            if abs(clean[key] - decision[key]) > 1e-6:
                                raise ValueError(f"out-of-bounds wire value: {key}")
                        self.validator.validate(clean)
                        record = {"elapsed_s": time.monotonic() - self.started,
                                  "heartbeat": value, "decision": decision}
                        self.decisions.append(record)
                        self.log.write(json.dumps(record) + "\n")
                        self.log.flush()
                    except Exception as exc:
                        self.errors.append(f"{type(exc).__name__}: {exc}")
                    self.pending.clear()
            elif address.startswith("/director/"):
                key = address.rsplit("/", 1)[-1]
                if key in self.pending:
                    self.errors.append(f"duplicate field before heartbeat: {key}")
                self.pending[key] = value

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.log.close()


class ObservedTDClient(TDClient):
    def __init__(self, host: str, port: int, mode: Callable[[], str], started: float) -> None:
        super().__init__(host, port)
        self.mode, self.started = mode, started
        self.publications: list[dict] = []

    def send_director(self, decision: dict, heartbeat: int | None = None) -> int:
        self.publications.append({"elapsed_s": time.monotonic() - self.started,
                                  "mode": self.mode(), "heartbeat": heartbeat})
        return super().send_director(decision, heartbeat)


def max_gap(times: list[float]) -> float:
    return max((b - a for a, b in zip(times, times[1:])), default=0.0)


def automatic_gap(times: list[float], phases: list[dict], end: float) -> float:
    """Largest silence within any contiguous non-manual operating interval."""
    start = 0.0
    intervals: list[tuple[float, float]] = []
    for event in phases:
        t = event["set_mode_returned_s"]
        if event["director_mode"] == "manual" and start is not None:
            intervals.append((start, t))
            start = None
        elif event["director_mode"] != "manual" and start is None:
            start = t
    if start is not None:
        intervals.append((start, end))
    return max((max_gap([lo] + [t for t in times if lo <= t <= hi] + [hi])
                for lo, hi in intervals), default=0.0)


def run(duration_s: float, output: Path, profile: Profile = PROFILES["production"]) -> dict:
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError("duration_s must be finite and positive")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "summary.json").exists():
        raise FileExistsError(f"refusing to overwrite previous evidence: {output}")
    manifest_start = code_manifest()
    started, started_wall = time.monotonic(), utc_now()
    baseline_threads = {thread.ident for thread in threading.enumerate()}
    baseline_rss = rss_bytes()
    stop = threading.Event()
    thread_errors: list[str] = []
    original_thread_hook = threading.excepthook

    def thread_hook(args: Any) -> None:
        thread_errors.append("".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                              args.exc_traceback)))
        original_thread_hook(args)

    threading.excepthook = thread_hook
    source, buffer = PCMSource(), ObservedBuffer()
    receiver = FeatureReceiver(port=0, buffer=buffer)
    sink = UDPSink(output, started)
    loop: DirectorLoop | None = None
    td = ObservedTDClient(*sink.server.server_address, lambda: loop.mode if loop else "starting", started)
    phase_now = lambda: phase_at(time.monotonic() - started, profile)
    client = FakeSubprocessClient(output, profile, phase_now, started)
    human_log = (output / "runtime.log").open("w", encoding="utf-8")
    loop = DirectorLoop(td, GPTDirector(client, RuleDirector(seed=736)),
                        period_s=profile.period_s, min_interval_s=profile.min_interval_s,
                        log_path=output / "decisions.jsonl", out=human_log)
    loop.fallback_after_s = profile.watchdog_s
    phase_events: list[dict] = []
    samples: list[dict] = []
    feeder_sent = 0
    interrupted = False
    failure: str | None = None
    next_sample = 0.0
    previous_phase: tuple[int, str] | None = None
    sample_log = (output / "samples.jsonl").open("w", encoding="utf-8")
    phase_log = (output / "phases.jsonl").open("w", encoding="utf-8")
    startup = {"status": "running", "started_utc": started_wall,
               "requested_wall_seconds": duration_s, "profile": asdict(profile),
               "code": manifest_start, "pid": os.getpid(),
               "scope": "cloud software only; simulated PCM and fake Codex; UDP sink is not TD"}
    atomic_json(output / "summary.json", startup)

    def on_tick(summary: dict, section: str) -> None:
        nonlocal next_sample, previous_phase
        elapsed = time.monotonic() - started
        cycle, phase = phase_now()
        identity = cycle, phase.name
        # Wait at most this declared manual phase for an actual asynchronous
        # call to be active, then take over. No forced/private scheduling edits.
        # A missing overlap fails coverage instead of being silently accepted.
        defer_manual = phase.director_mode == "manual" and client.active == 0
        if identity != previous_phase and not defer_manual:
            event = {"elapsed_s": elapsed, "cycle": cycle, "phase": phase.name,
                     "director_mode": phase.director_mode, "fake_mode": phase.fake_mode,
                     "inflight_fake_subprocesses": client.active}
            loop.set_mode(phase.director_mode)
            event["set_mode_returned_s"] = time.monotonic() - started
            phase_events.append(event)
            phase_log.write(json.dumps(event) + "\n")
            phase_log.flush()
            previous_phase = identity
        if (output / "STOP").exists():
            loop.request_stop()
        loop.on_tick(summary, section)
        if elapsed >= next_sample:
            sample = {"elapsed_s": elapsed, "rss_bytes": rss_bytes(),
                      "thread_count": threading.active_count(), "os_thread_count": os_thread_count(),
                      "feature_received": receiver.received, "pcm_blocks": source.blocks,
                      "published_decisions": len(loop.decisions), "wire_decisions": len(sink.decisions),
                      "heartbeats": len(sink.heartbeats), "section": section,
                      "mode": loop.mode, "fake_subprocesses": client.active,
                      "max_feature_gap_s": max(buffer.metrics()["max_gaps_s"].values(), default=0),
                      "director_errors": loop.errors}
            samples.append(sample)
            sample_log.write(json.dumps(sample) + "\n")
            sample_log.flush()
            atomic_json(output / "progress.json", sample)
            next_sample = elapsed + min(5.0, duration_s / 3)

    sidecar = Sidecar(buffer, SectionDetector(), td, log_path=output / "sections.jsonl",
                      status_hz=profile.status_hz, on_tick=on_tick, out=human_log)
    sender = SimpleUDPClient(*receiver.address)

    def feed() -> None:
        nonlocal feeder_sent
        deadline = time.monotonic()
        calibrated_recovery: tuple[int, str] | None = None
        while not stop.is_set():
            elapsed = time.monotonic() - started
            recovery_probe = recovery_fixture_at(elapsed, profile)
            steady_recovery = recovery_probe is not None
            if recovery_probe is not None:
                cycle, phase = recovery_probe
                if calibrated_recovery != (cycle, phase.name):
                    source.calibrate_steady()
                    source.recovery_calibrations.append({"elapsed_s": elapsed, "cycle": cycle,
                                                         "recovery_phase": phase.name,
                                                         "preconditioning_fixture_s": 30})
                    calibrated_recovery = cycle, phase.name
            features = source.features(elapsed, music_position_s=22.0 if steady_recovery else None)
            for key, value in features.items():
                sender.send_message(f"/feat/{key}", value)
                feeder_sent += 1
            deadline += 1 / FEATURE_HZ
            stop.wait(max(0, deadline - time.monotonic()))

    feeder = threading.Thread(target=feed, name="endurance-pcm-feeder", daemon=True)
    active_start = time.monotonic()
    try:
        sink.thread.start()
        receiver.start()
        feeder.start()
        sidecar.run(duration=duration_s)
    except KeyboardInterrupt:
        interrupted = True
    except Exception:
        failure = traceback.format_exc()
    finally:
        active_end = time.monotonic()
        stop.set()
        feeder.join(3)
        # Stop publication first; wait for the bounded subprocess timeout so
        # shutdown evidence includes completion of every real fake subprocess.
        loop.close(timeout=profile.subprocess_timeout_s + 2)
        sidecar.close()
        time.sleep(.15)  # Drain localhost datagrams already in the kernel.
        receiver.stop()
        sender._sock.close()
        td.close()
        sink.close()
        sample_log.close()
        phase_log.close()
        human_log.close()
        threading.excepthook = original_thread_hook
    ended, ended_wall = time.monotonic(), utc_now()
    actual_duration = active_end - active_start
    if actual_duration + .05 < duration_s:
        interrupted = True  # Sidecar intentionally consumes KeyboardInterrupt.
    manifest_end = code_manifest()
    feature_metrics = buffer.metrics()
    publications = td.publications
    manual_publications = sum(p["mode"] == "manual" for p in publications)
    alive_threads = [t.name for t in threading.enumerate()
                     if t.ident not in baseline_threads and t.is_alive()]
    full_cycle = actual_duration >= PHASES[-1].end_s * profile.phase_scale
    sources = Counter(r["source"] for r in loop.decisions)
    call_modes = Counter(c["fake_mode"] for c in client.calls)
    wire_times = [r["elapsed_s"] for r in sink.decisions]
    automatic_max_gap = automatic_gap(wire_times, phase_events, active_end - started)
    heartbeat_times = [t for t, _ in sink.heartbeats]
    beats = [n for _, n in sink.heartbeats]
    valid_logs = all(not list(sink.validator.iter_errors(r["decision"])) for r in loop.decisions)
    # These align with actual TDClient publication calls rather than rounded
    # director timestamps. Assess only phases completed before shutdown.
    published = [{"elapsed_s": p["elapsed_s"], "source": r["source"]}
                 for p, r in zip(publications, loop.decisions)]
    fault_recovery: list[dict] = []
    gpt_recovery: list[dict] = []
    for event in phase_events:
        phase = next(p for p in PHASES if p.name == event["phase"])
        planned_end = (event["cycle"] * PHASES[-1].end_s + phase.end_s) * profile.phase_scale
        if planned_end > actual_duration:
            continue
        phase_start = event["elapsed_s"]
        in_phase = [p for p in published if phase_start <= p["elapsed_s"] < planned_end]
        if phase.fake_mode in {"fail", "garbage", "hang"}:
            rule_times = [p["elapsed_s"] for p in in_phase if p["source"].startswith("rule")]
            delay = min(rule_times) - phase_start if rule_times else None
            fault_recovery.append({"cycle": event["cycle"], "phase": phase.name,
                                   "phase_start_s": phase_start, "first_rule_delay_s": delay})
        if phase.name in {"gpt_recovery", "gpt_same_mode_recovery"}:
            recovered = [p for p in in_phase if p["source"] == "gpt"]
            gpt_recovery.append({"cycle": event["cycle"], "phase": phase.name, "phase_start_s": phase_start,
                                 "gpt_publications": len(recovered),
                                 "first_gpt_delay_s": recovered[0]["elapsed_s"] - phase_start if recovered else None})
    max_feature_gap = max(feature_metrics["max_gaps_s"].values(), default=0)
    expected_sections = {"steady", "breakdown", "drop", "build"}
    manual_phase = next(p for p in PHASES if p.name == "manual_takeover")
    expected_manual_phases = sum(
        (cycle * PHASES[-1].end_s + manual_phase.end_s) * profile.phase_scale <= actual_duration
        for cycle in range(int(actual_duration / (PHASES[-1].end_s * profile.phase_scale)) + 1))
    completed_manual_events = [e for e in phase_events if e["director_mode"] == "manual" and
        (e["cycle"] * PHASES[-1].end_s + manual_phase.end_s) * profile.phase_scale <= actual_duration]
    checks: dict[str, bool] = {
        "requested_wall_duration_completed": not interrupted and actual_duration >= duration_s,
        "no_unexpected_exception": failure is None and not thread_errors,
        "source_code_unchanged": manifest_start["sha256"] == manifest_end["sha256"],
        "feature_datagrams_all_received": receiver.received == feeder_sent,
        "feature_gap_below_one_second": max_feature_gap < 1.0,
        "feature_keys_all_received": set(FEATURE_KEYS).issubset(feature_metrics["counts"]),
        "feature_buffer_bounded": len(buffer) <= buffer.maxlen * len(FEATURE_KEYS),
        "director_decisions_exist": bool(loop.decisions),
        "decisions_schema_valid": valid_logs and not sink.errors,
        "all_published_decisions_received": len(loop.decisions) == len(sink.decisions) == len(publications),
        "heartbeats_strictly_increase": bool(beats) and all(b > a for a, b in zip(beats, beats[1:])),
        "heartbeat_gap_below_td_45s_limit": max_gap(heartbeat_times) < 45,
        "no_manual_publications": manual_publications == 0,
        "automatic_decision_gap_within_watchdog_budget": automatic_max_gap < profile.watchdog_s + 1.0,
        "no_unexpected_director_errors": loop.errors == 0,
        "no_udp_send_errors": getattr(td, "errors", getattr(td, "send_errors", 0)) == 0,
        "injected_failures_detected": all(c.get("outcome") == "expected_injected_error" for c in client.calls
                                           if c["fake_mode"] != "ok"),
        "fake_success_calls_have_no_errors": all(c.get("outcome") == "ok" for c in client.calls
                                                 if c["fake_mode"] == "ok"),
        "shutdown_threads_clean": not alive_threads,
        "shutdown_subprocesses_clean": client.active == 0,
        "single_fake_subprocess_at_once": client.peak_active <= 1,
        "rss_growth_below_96mib": max([s["rss_bytes"] for s in samples] + [baseline_rss]) - baseline_rss < 96 * 1024**2,
        "thread_peak_bounded": max([s["thread_count"] for s in samples] + [len(baseline_threads)]) <= len(baseline_threads) + 20,
    }
    if full_cycle:
        checks.update(fallback_within_each_completed_outage=bool(fault_recovery) and all(
                          item["first_rule_delay_s"] is not None and item["first_rule_delay_s"] <=
                          (30.0 if profile.name == "production-timing" else 1.8) for item in fault_recovery),
                      gpt_recovers_in_each_completed_recovery_phase=bool(gpt_recovery) and all(
                          item["gpt_publications"] > 0 for item in gpt_recovery),
                      all_fault_modes_exercised={"ok", "fail", "garbage", "hang"}.issubset(call_modes),
                      all_operator_modes_exercised={"gpt", "manual", "rule"}.issubset(
                          e["director_mode"] for e in phase_events),
                      gpt_success_and_rule_fallback=bool(sources.get("gpt")) and any(
                          s.startswith("rule") for s in sources),
                      watchdog_fallback_observed=any("watchdog" in s.lower() for s in sources),
                      manual_takeover_during_subprocess=(
                          len(completed_manual_events) == expected_manual_phases and
                          all(e["inflight_fake_subprocesses"] > 0 for e in completed_manual_events)))
    if actual_duration >= 120:
        checks["all_music_sections_observed"] = expected_sections.issubset(sink.sections)
    summary = {**startup, "status": "passed" if all(checks.values()) else "failed",
               "ended_utc": ended_wall, "actual_active_wall_seconds": actual_duration,
               "total_wall_seconds_including_shutdown": ended - started,
               "shutdown_seconds": ended - active_end,
               "full_hour": actual_duration >= 3600 and not interrupted,
               "full_hour_passed": actual_duration >= 3600 and all(checks.values()),
               "full_production_timing_hour": actual_duration >= 3600 and profile.name == "production-timing" and not interrupted,
               "interrupted": interrupted, "checks": checks,
               "coverage": {"full_fault_cycle": full_cycle,
                            "recovery_music_isolation": "Synthetic PCM is held steady (with kick transients) beginning 30 fixture seconds BEFORE each GPT recovery phase and through its end. At preconditioning entry only, fixture Normalizer peaks are reset and primed from that actual PCM arrangement. The normal default-decay extractors, SectionDetector history and UDP continue untouched, so settling happens before recovery. No section labels or post-extraction features are forced; smoke scales fixture intervals explicitly",
                            "pcm_path": "generated 48kHz mono PCM -> band_energies -> Normalizer -> KickDetector -> 10Hz OSC",
                            "real_components": ["FeatureReceiver", "FeatureBuffer", "Sidecar", "SectionDetector",
                                                "DirectorLoop", "GPTDirector", "CodexClient", "TDClient"],
                            "test_doubles": ["generated PCM", "tools/fake_codex.py", "UDP sink replacing TouchDesigner"],
                            "not_tested": ["paid AI/auth/quota", "TouchDesigner runtime/callbacks", "GPU/shaders/FPS",
                                           "Mac audio routing", "MIDI hardware", "projectM runtime"]},
               "features": {**feature_metrics, "sent_datagrams": feeder_sent,
                            "received_datagrams": receiver.received, "ignored_datagrams": receiver.ignored,
                            "pcm_blocks": source.blocks, "sample_rate": source.sample_rate,
                            "pcm_samples": source.blocks * source.block_size,
                            "pcm_peak_amplitude": source.raw_peak,
                            "synthetic_kicks": source.kicks.count,
                            "recovery_calibrations": source.recovery_calibrations,
                            "buffer_total_samples_end": len(buffer),
                            "max_gap_s": max_feature_gap},
               "director": {**loop.stats(), "wire_decisions": len(sink.decisions),
                            "max_decision_gap_s_including_manual": max_gap(wire_times),
                            "wire_heartbeats": len(beats), "max_heartbeat_gap_s": max_gap(heartbeat_times),
                            "manual_publications": manual_publications,
                            "max_automatic_decision_silence_s": automatic_max_gap,
                            "fault_phase_recovery": fault_recovery,
                            "gpt_phase_recovery": gpt_recovery,
                            "fake_call_modes": dict(call_modes), "peak_active_fake_subprocesses": client.peak_active,
                            "udp_send_errors": getattr(td, "errors", getattr(td, "send_errors", 0))},
               "sections": dict(sink.sections), "phase_events": phase_events,
               "resources": {"baseline_rss_bytes": baseline_rss,
                             "peak_sampled_rss_bytes": max([s["rss_bytes"] for s in samples] + [baseline_rss]),
                             "end_rss_bytes": rss_bytes(), "baseline_threads": len(baseline_threads),
                             "peak_sampled_threads": max([s["thread_count"] for s in samples] + [len(baseline_threads)]),
                             "peak_sampled_os_threads": max((s["os_thread_count"] for s in samples
                                                             if s["os_thread_count"] is not None), default=None),
                             "remaining_new_threads": alive_threads},
               "errors": {"main": failure, "threads": thread_errors, "wire": sink.errors},
               "code_at_end": manifest_end, "platform": platform.platform(),
               "python": sys.version, "fake_calls": client.calls}
    atomic_json(output / "summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, default=3600)
    parser.add_argument("--profile", choices=PROFILES, default="production")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    output = args.output or ROOT / "artifacts/cloud-verification/endurance" / datetime.now(
        timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = run(args.duration_seconds, output, PROFILES[args.profile])
    print(json.dumps({"status": report["status"], "full_hour": report["full_hour"],
                      "actual_active_wall_seconds": report["actual_active_wall_seconds"],
                      "failed_checks": [k for k, passed in report["checks"].items() if not passed],
                      "summary": str(output / "summary.json")}, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
