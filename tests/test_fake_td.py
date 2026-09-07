"""Tests for tools/fake_td.py — the stand-in for TouchDesigner.

The fake is only useful if its shape is trustworthy, so these tests check the
timeline itself (levels, ramps, the kick grid, determinism) and then run one
end-to-end pass through the real sidecar over real UDP.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from amv.features import FeatureBuffer
from amv.osc_io import FeatureReceiver, TDClient
from amv.sections import BREAKDOWN, BUILD, DROP, STEADY, SectionDetector
from amv.sidecar import Sidecar

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import fake_td  # noqa: E402  (needs the path above)

RATE_HZ = 10


def samples_between(start: float, end: float, key: str) -> list[float]:
    return [s[key] for t, s in fake_td.timeline() if start <= t < end]


# -- timeline shape ---------------------------------------------------------


def test_ground_truth_matches_the_brief():
    spans = [(s.name, s.start, s.end) for s in fake_td.ground_truth()]
    assert spans == [
        ("steady", 0.0, 20.0),
        ("build", 20.0, 40.0),
        ("drop", 40.0, 48.0),
        ("steady", 48.0, 78.0),
        ("breakdown", 78.0, 88.0),
        ("drop", 88.0, 96.0),
        ("steady", 96.0, 116.0),
    ]
    assert fake_td.duration() == pytest.approx(116.0)


def test_sample_count_and_spacing():
    timeline = fake_td.timeline()
    assert len(timeline) == int(fake_td.duration() * RATE_HZ)
    times = [t for t, _ in timeline]
    assert times[0] == 0.0
    deltas = {round(b - a, 6) for a, b in zip(times, times[1:])}
    assert deltas == {0.1}


def test_every_sample_carries_the_full_feature_set():
    for t, sample in fake_td.timeline():
        assert set(sample) == set(fake_td.FEATURES) | {"kick"}
        for key in ("bass", "mid", "high", "energy"):
            assert 0.0 <= sample[key] <= 1.0, f"{key} out of range at t={t}"
        assert sample["centroid"] > 20.0
        assert sample["kick"] in (0.0, 1.0)


def test_section_at_agrees_with_the_spans():
    for span in fake_td.ground_truth():
        assert fake_td.section_at(span.start) == span.name
        assert fake_td.section_at(span.end - 0.01) == span.name
    assert fake_td.section_at(fake_td.duration() + 10) == "steady"


def test_steady_sits_at_six_tenths():
    energy = samples_between(0, 20, "energy")
    assert sum(energy) / len(energy) == pytest.approx(0.6, abs=0.01)
    assert max(energy) < 0.7


def test_build_ramps_energy_and_opens_the_highs():
    energy = samples_between(20, 40, "energy")
    high = samples_between(20, 40, "high")
    assert energy[0] == pytest.approx(0.60, abs=0.05)
    assert energy[-1] == pytest.approx(0.85, abs=0.05)
    assert high[-1] - high[0] > 0.35
    # The build must never fake a drop by itself.
    assert max(samples_between(20, 40, "bass")) < 0.7


def test_drops_put_the_bass_at_nine_tenths():
    for start, end in ((40, 48), (88, 96)):
        bass = samples_between(start, end, "bass")
        assert min(bass) > 0.8
        assert sum(bass) / len(bass) == pytest.approx(0.9, abs=0.02)


def test_breakdown_is_quiet_and_kickless():
    energy = samples_between(78, 88, "energy")
    bass = samples_between(78, 88, "bass")
    kicks = samples_between(78, 88, "kick")
    assert max(energy) < 0.35, "breakdown must stay under the SPEC §4 threshold"
    assert max(bass) < 0.2
    assert sum(kicks) == 0


def test_kicks_follow_the_bpm_grid():
    kicks = [t for t, s in fake_td.timeline() if s["kick"]]
    assert kicks, "no kicks at all"
    # 145 BPM outside the ten second breakdown.
    playing = fake_td.duration() - 10.0
    assert len(kicks) * 60.0 / playing == pytest.approx(145.0, abs=3.0)
    gaps = [round(b - a, 3) for a, b in zip(kicks, kicks[1:]) if b - a < 1.0]
    assert set(gaps) <= {0.4, 0.5}  # 60/145 = 0.414 s, quantised to 10 Hz


def test_kicks_per_min_measured_through_the_buffer():
    """What the director actually reads should say 145, not something else."""
    clock = {"t": 0.0}
    buffer = FeatureBuffer(clock=lambda: clock["t"])
    for t, sample in fake_td.timeline():
        if t > 30.0:
            break
        clock["t"] = t
        buffer.push("kick", sample["kick"], t)
    assert buffer.summary()["kicks_per_min"] == pytest.approx(145.0, abs=3.0)


def test_noise_is_present_but_small():
    energy = samples_between(0, 20, "energy")
    assert len(set(energy)) > 100, "a flat section with no noise is not realistic"
    assert max(abs(e - 0.6) for e in energy) < 0.08


def test_timeline_is_deterministic_and_seedable():
    assert fake_td.timeline() == fake_td.timeline()
    assert fake_td.timeline(seed=1) != fake_td.timeline(seed=2)


def test_energy_bass_stream_matches_the_timeline():
    stream = list(fake_td.energy_bass_stream())
    timeline = fake_td.timeline()
    assert len(stream) == len(timeline)
    assert stream[0] == (timeline[0][0], timeline[0][1]["energy"], timeline[0][1]["bass"])


# -- scripts ----------------------------------------------------------------


def test_custom_script_round_trips(tmp_path):
    script = {
        "name": "tiny",
        "bpm": 120,
        "noise": 0.0,
        "sections": [
            {"name": "steady", "duration": 2.0, "energy": 0.5, "bass": 0.4},
            {"name": "drop", "duration": 1.0, "energy": 0.9, "bass": 0.9, "kicks": False},
        ],
    }
    path = tmp_path / "tiny.json"
    path.write_text(json.dumps(script), encoding="utf-8")

    loaded = fake_td.resolve_script(str(path))
    assert fake_td.duration(loaded) == pytest.approx(3.0)
    timeline = fake_td.timeline(loaded)
    assert len(timeline) == 30
    assert timeline[0][1]["energy"] == pytest.approx(0.5)  # noise 0 means exact
    assert all(s["kick"] == 0.0 for t, s in timeline if t >= 2.0)
    assert fake_td.resolve_script("default") is fake_td.DEFAULT_SCRIPT


def test_bad_script_is_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"sections": []}', encoding="utf-8")
    with pytest.raises(ValueError):
        fake_td.load_script(path)


def test_format_ground_truth_names_every_section():
    text = fake_td.format_ground_truth()
    assert "psytrance_145" in text
    assert "145 BPM" in text
    for name in ("steady", "build", "drop", "breakdown"):
        assert name in text
    assert text.count("\n") == len(fake_td.ground_truth()) + 1


def test_cli_dry_run_prints_ground_truth(capsys):
    assert fake_td.main(["--dry-run"]) == 0
    assert "ground truth" in capsys.readouterr().out


# -- end to end -------------------------------------------------------------


def test_timeline_through_udp_into_the_sidecar():
    """fake_td -> UDP -> FeatureReceiver -> FeatureBuffer -> Sidecar -> TD.

    Time is driven by hand rather than by the wall clock: the samples carry
    their own timestamps, so the whole 116 s track replays in well under a
    second and the assertion is on section timing, not on scheduling luck.
    """
    now = {"t": 0.0}
    buffer = FeatureBuffer(clock=lambda: now["t"])
    receiver = FeatureReceiver("127.0.0.1", 0, buffer).start()
    td = TDClient("127.0.0.1", receiver.port)  # its /feat/section echo is ignored
    sidecar = Sidecar(buffer, SectionDetector(), td, status_hz=0.0, clock=lambda: now["t"])
    sender = TDClient("127.0.0.1", receiver.port)
    try:
        for t, sample in fake_td.timeline():
            now["t"] = t
            sender.send("/feat/energy", float(sample["energy"]))
            sender.send("/feat/bass", float(sample["bass"]))
            deadline = time.monotonic() + 2.0
            while buffer.latest_t("bass") != t and time.monotonic() < deadline:
                time.sleep(0.0005)
            sidecar.tick(t)
    finally:
        sender.close()
        td.close()
        receiver.stop()

    detected = [(round(r["t"], 1), r["from"], r["to"]) for r in sidecar.transitions]
    assert detected == [
        (27.5, STEADY, BUILD),
        (40.0, BUILD, DROP),
        (42.0, DROP, STEADY),
        (82.1, STEADY, BREAKDOWN),
        (88.0, BREAKDOWN, DROP),
        (90.0, DROP, STEADY),
    ]
