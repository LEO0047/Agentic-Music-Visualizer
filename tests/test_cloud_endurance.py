"""Free software harness checks: no real Codex, TD runtime, or audio device."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import cloud_endurance as endurance  # noqa: E402
from amv.codex_client import CodexError  # noqa: E402
from amv.schema import validate_and_clamp  # noqa: E402


def test_phase_cycle_and_profiles_do_not_scale_clock():
    profile = endurance.PROFILES["production"]
    assert endurance.phase_at(0, profile)[1].name == "gpt_success"
    assert endurance.phase_at(96, profile)[1].fake_mode == "hang"
    assert endurance.phase_at(210, profile)[1].director_mode == "manual"
    assert endurance.phase_at(304, profile)[0] == 1
    assert endurance.phase_at(309, profile)[1].name == "gpt_success"
    assert endurance.phase_at(146, profile)[1].name == "gpt_same_mode_recovery"
    assert endurance.phase_at(146, profile)[1].director_mode == "gpt"
    assert profile.subprocess_timeout_s == 30
    assert profile.watchdog_s == 25
    assert profile.ok_delay_s == 13
    assert endurance.PROFILES["smoke"].ok_delay_s == .02
    assert endurance.phase_at(9, endurance.PROFILES["smoke"])[1].director_mode == "manual"


def test_pcm_source_uses_production_features_and_bounded_pcm():
    source = endurance.PCMSource()
    quiet = source.features(0)
    source.features(7.9)
    loud = source.features(8)
    assert set(quiet) == set(endurance.FEATURE_KEYS)
    assert quiet["energy"] < .35
    assert loud["energy"] > .7
    assert loud["bass"] >= .7
    assert loud["kick"] == 1
    assert source.raw_peak <= 1
    assert all(0 <= v <= 1 for k, v in loud.items() if k != "centroid")
    assert source.blocks == 3
    assert 100 < loud["centroid"] < 10000


def test_subprocess_fake_ignores_real_codex_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AMV_CODEX_BIN", "/must/not/execute/real-codex")
    monkeypatch.setenv("AMV_FAKE_CODEX_MODE", "hang")
    profile = endurance.PROFILES["smoke"]
    phase = endurance.PHASES[0]
    client = endurance.FakeSubprocessClient(tmp_path, profile, lambda: (0, phase), time.monotonic())
    assert "amv fake" in client.version()
    result = client.decide("safe generated synthetic prompt")
    assert validate_and_clamp(result) == result
    assert client.calls[0]["outcome"] == "ok"
    assert client.active == 0
    assert os.environ["AMV_FAKE_CODEX_MODE"] == "hang"
    argv = client.build_argv("test", tmp_path / "out.json")
    assert argv[0] == sys.executable
    assert str(endurance.FAKE_CODEX) in argv
    assert "/must/not/execute/real-codex" not in argv


@pytest.mark.parametrize("phase_index,error", [(1, "exited 1"), (2, "not valid JSON"), (3, "timed out")])
def test_real_fake_subprocess_failure_paths(tmp_path, phase_index, error):
    phase = endurance.PHASES[phase_index]
    profile = endurance.PROFILES["smoke"]
    client = endurance.FakeSubprocessClient(tmp_path, profile, lambda: (0, phase), time.monotonic())
    with pytest.raises(CodexError, match=error):
        client.decide("test injected failure")
    assert client.calls[0]["outcome"] == "expected_injected_error"
    assert client.active == 0
    assert client.calls[0]["end_elapsed_s"] >= client.calls[0]["start_elapsed_s"]


def test_manifest_contains_real_runtime_and_harness():
    manifest = endurance.code_manifest()
    assert len(manifest["sha256"]) == 64
    assert "amv/director.py" in manifest["files"]
    assert "amv/audio_features.py" in manifest["files"]
    assert "tools/cloud_endurance.py" in manifest["files"]
    assert "tools/fake_codex.py" in manifest["files"]


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf")])
def test_refuse_nonpositive_or_nonfinite_duration(tmp_path, duration):
    with pytest.raises(ValueError):
        endurance.run(duration, tmp_path)


def test_end_to_end_smoke_is_real_wall_time_and_never_claims_full_hour(tmp_path):
    started = time.monotonic()
    report = endurance.run(2, tmp_path, endurance.PROFILES["smoke"])
    assert time.monotonic() - started >= 2
    assert report["actual_active_wall_seconds"] >= 2
    assert report["full_hour"] is False
    assert report["full_production_timing_hour"] is False
    assert report["coverage"]["full_fault_cycle"] is False
    assert report["features"]["received_datagrams"] > 100
    assert report["director"]["wire_decisions"] > 0
    assert report["checks"]["no_manual_publications"]
    assert report["checks"]["shutdown_threads_clean"]
    assert report["checks"]["shutdown_subprocesses_clean"]
    assert report["status"] == "passed", report["checks"]
    assert json.loads((tmp_path / "summary.json").read_text())["status"] == "passed"
    assert (tmp_path / "wire-decisions.jsonl").stat().st_size > 0
    with pytest.raises(FileExistsError):
        endurance.run(.1, tmp_path, endurance.PROFILES["smoke"])


def test_automatic_gap_excludes_manual_intervals_but_counts_boundaries():
    phases = [{"director_mode": "manual", "set_mode_returned_s": 10},
              {"director_mode": "rule", "set_mode_returned_s": 50}]
    assert endurance.automatic_gap([1, 8, 54, 59], phases, 60) == 7
    assert endurance.automatic_gap([], [], 5) == 5


def test_stop_file_produces_interrupted_report_and_cleans_up(tmp_path):
    (tmp_path / "STOP").write_text("operator cancellation")
    report = endurance.run(2, tmp_path, endurance.PROFILES["smoke"])
    assert report["interrupted"]
    assert not report["full_hour"]
    assert report["actual_active_wall_seconds"] < 2
    assert report["checks"]["shutdown_threads_clean"]
    assert report["checks"]["shutdown_subprocesses_clean"]


def test_steady_recovery_override_still_extracts_pcm_and_keeps_centroid_hz(monkeypatch):
    source = endurance.PCMSource()
    original = endurance.band_energies
    blocks = []

    def record_extract(block, sample_rate):
        blocks.append((block.shape, sample_rate))
        return original(block, sample_rate)

    monkeypatch.setattr(endurance, "band_energies", record_extract)
    features = source.features(100, music_position_s=22)
    assert blocks == [((4800,), 48000)]
    assert .35 < features["energy"] < 1
    assert 100 < features["centroid"] < 10000
    assert set(features) == set(endurance.FEATURE_KEYS)
    # At synthetic music position 100s the default arrangement is a loud drop.
    default_drop = endurance.PCMSource().features(100)
    assert features["energy"] < default_drop["energy"]


def test_preconditioned_pcm_stays_steady_across_three_complete_fixture_cycles():
    # Offline sample replay only: this is NOT a wall-clock endurance test.
    source = endurance.PCMSource()
    detector = endurance.SectionDetector()
    profile = endurance.PROFILES["production"]
    calibrated = None
    outside_sections = set()
    steady_windows = {}
    kicks_before = source.kicks.count
    cycle_seconds = endurance.PHASES[-1].end_s
    for i in range(int(3 * cycle_seconds * endurance.FEATURE_HZ)):
        t = i / endurance.FEATURE_HZ
        cycle, phase = endurance.phase_at(t, profile)
        recovery = phase.name in {"gpt_recovery", "gpt_same_mode_recovery"}
        identity = cycle, phase.name
        probe = endurance.recovery_fixture_at(t, profile)
        if probe is not None and calibrated != (probe[0], probe[1].name):
            source.calibrate_steady()
            calibrated = probe[0], probe[1].name
        features = source.features(t, music_position_s=22 if probe is not None else None)
        state = detector.update(t, features["energy"], features["bass"])
        assert 0 <= features["energy"] <= 1
        assert features["centroid"] >= 0
        if not recovery:
            outside_sections.add(state)
        else:
            assert state == "steady", (cycle, phase.name, t, state)
            steady_windows[identity] = steady_windows.get(identity, 0) + 1
    assert set(outside_sections) == {"steady", "build", "drop", "breakdown"}
    assert len(steady_windows) == 6
    assert all(count == (640 if key[1] == "gpt_same_mode_recovery" else 480)
               for key, count in steady_windows.items())
    assert source.kicks.count > kicks_before
    assert source.normalizer.decay == .999
