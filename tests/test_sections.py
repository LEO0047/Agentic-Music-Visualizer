"""Tests for amv.sections — SPEC §4 replayed against the fake TD timeline.

Everything here runs in process: the detector is fed the same samples
``tools/fake_td.py`` would put on the wire, with no socket and no clock.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from amv.sections import BREAKDOWN, BUILD, DROP, SECTIONS, STEADY, SectionDetector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import fake_td  # noqa: E402  (needs the path above)

#: How far a detected transition may sit from where fake_td says it should.
TOLERANCE_S = 1.0


def replay(detector: SectionDetector | None = None, **kwargs) -> SectionDetector:
    """Run the default fake_td track through a detector and return it."""
    detector = detector or SectionDetector(**kwargs)
    for t, energy, bass in fake_td.energy_bass_stream():
        detector.update(t, energy, bass)
    return detector


def entries(detector: SectionDetector, name: str) -> list[float]:
    """Times at which the detector entered ``name``."""
    return [t for t, _, to in detector.events if to == name]


def expected(name: str) -> list[float]:
    """Times fake_td says ``name`` becomes detectable, in order."""
    return [s.detect_at for s in fake_td.ground_truth() if s.name == name]


# -- the whole track --------------------------------------------------------


def test_detects_the_scripted_timeline():
    detector = replay()
    detected = [(round(t, 1), frm, to) for t, frm, to in detector.events]
    assert detected == [
        (27.5, STEADY, BUILD),
        (40.0, BUILD, DROP),
        (42.0, DROP, STEADY),
        (82.1, STEADY, BREAKDOWN),
        (88.0, BREAKDOWN, DROP),
        (90.0, DROP, STEADY),
    ]
    assert detector.state == STEADY
    assert all(to in SECTIONS for _, _, to in detector.events)


@pytest.mark.parametrize("name", [BUILD, DROP, BREAKDOWN])
def test_every_scripted_section_is_detected_on_time(name):
    detector = replay()
    got = entries(detector, name)
    want = expected(name)
    assert len(got) == len(want), f"{name}: detected {got}, expected near {want}"
    for actual, target in zip(got, want):
        assert actual == pytest.approx(target, abs=TOLERANCE_S), (
            f"{name} at {actual:.1f}s, expected {target:.1f}s "
            f"(±{TOLERANCE_S}s); ground truth {fake_td.format_ground_truth()}"
        )


def test_both_drops_are_found_and_nothing_else_is():
    detector = replay()
    drops = entries(detector, DROP)
    assert len(drops) == 2
    assert drops[0] == pytest.approx(40.0, abs=TOLERANCE_S)  # out of the build
    assert drops[1] == pytest.approx(88.0, abs=TOLERANCE_S)  # out of the breakdown


def test_no_spurious_drops_during_steady():
    """A drop may only be reported inside a scripted drop section."""
    detector = replay()
    spans = {s.name: [] for s in fake_td.ground_truth()}
    for span in fake_td.ground_truth():
        spans[span.name].append((span.start, span.end))
    for t in entries(detector, DROP):
        assert any(lo <= t < hi for lo, hi in spans[DROP]), f"drop at {t:.1f}s is not in a drop"


def test_build_is_called_before_the_drop_it_predicts():
    detector = replay()
    assert entries(detector, BUILD)[0] < entries(detector, DROP)[0]
    # And it is genuinely early warning, not a rename of the drop itself.
    assert entries(detector, DROP)[0] - entries(detector, BUILD)[0] > 5.0


def test_states_are_stable_not_chattering():
    """Six transitions for a seven section track: no flapping."""
    detector = replay()
    assert len(detector.events) <= 8


def test_replay_is_deterministic():
    assert replay().events == replay().events


# -- individual rules -------------------------------------------------------


def feed(detector: SectionDetector, start, stop, energy, bass, step=0.1):
    t = start
    while t < stop - 1e-9:
        detector.update(round(t, 6), energy, bass)
        t += step
    return round(t, 6)


def test_breakdown_needs_more_than_four_seconds():
    d = SectionDetector()
    feed(d, 0.0, 10.0, 0.6, 0.55)
    t = feed(d, 10.0, 13.9, 0.2, 0.1)  # 3.9 s of quiet
    assert d.state == STEADY
    feed(d, t, 15.0, 0.2, 0.1)
    assert d.state == BREAKDOWN


def test_a_single_quiet_sample_does_not_start_a_breakdown():
    d = SectionDetector()
    feed(d, 0.0, 10.0, 0.6, 0.55)
    d.update(10.0, 0.05, 0.55)
    feed(d, 10.1, 20.0, 0.6, 0.55)
    assert d.state == STEADY
    assert d.events == []


def test_breakdown_exit_has_hysteresis():
    """0.36 energy is above the entry threshold but inside the exit band."""
    d = SectionDetector()
    feed(d, 0.0, 10.0, 0.6, 0.55)
    feed(d, 10.0, 20.0, 0.2, 0.1)
    assert d.state == BREAKDOWN
    feed(d, 20.0, 24.0, 0.37, 0.2)  # >0.35 but <0.40
    assert d.state == BREAKDOWN
    feed(d, 24.0, 26.0, 0.6, 0.55)
    assert d.state == STEADY


def test_drop_fires_out_of_a_breakdown():
    d = SectionDetector()
    feed(d, 0.0, 10.0, 0.6, 0.55)
    feed(d, 10.0, 20.0, 0.2, 0.1)
    assert d.state == BREAKDOWN
    assert d.update(20.0, 0.9, 0.9) == DROP


def test_drop_is_momentary_and_returns_to_steady():
    d = SectionDetector(drop_hold_s=2.0)
    feed(d, 0.0, 10.0, 0.6, 0.55)
    feed(d, 10.0, 20.0, 0.2, 0.1)
    d.update(20.0, 0.9, 0.9)
    assert d.state == DROP
    feed(d, 20.1, 21.9, 0.9, 0.9)
    assert d.state == DROP
    assert d.time_in_state(21.8) == pytest.approx(1.8)
    feed(d, 21.9, 24.0, 0.9, 0.9)
    assert d.state == STEADY


def test_sustained_loud_bass_does_not_retrigger_the_drop():
    d = SectionDetector()
    feed(d, 0.0, 10.0, 0.6, 0.55)
    feed(d, 10.0, 20.0, 0.2, 0.1)
    d.update(20.0, 0.9, 0.9)
    feed(d, 20.1, 40.0, 0.9, 0.9)  # twenty seconds of 0.9 bass
    assert entries(d, DROP) == [20.0]


def test_loud_bass_without_a_build_or_breakdown_is_not_a_drop():
    d = SectionDetector()
    feed(d, 0.0, 30.0, 0.6, 0.55)
    d.update(30.0, 0.9, 0.95)
    assert d.state == STEADY
    assert entries(d, DROP) == []


def test_drop_arming_expires():
    """Bass arriving long after the breakdown ended is just loud, not a drop."""
    d = SectionDetector(drop_arm_s=4.0)
    feed(d, 0.0, 10.0, 0.6, 0.55)
    feed(d, 10.0, 20.0, 0.2, 0.1)
    assert d.state == BREAKDOWN
    feed(d, 20.0, 30.0, 0.6, 0.55)  # recovered without a drop
    d.update(30.0, 0.7, 0.95)
    assert d.state == STEADY


def test_build_needs_a_rise_not_just_a_positive_slope():
    """Flat energy with a hair of drift stays steady."""
    d = SectionDetector()
    t = 0.0
    while t < 40.0:
        d.update(round(t, 6), 0.60 + 0.00005 * t, 0.55)
        t += 0.1
    assert d.state == STEADY


def test_build_is_refused_while_a_breakdown_is_still_in_the_window():
    d = SectionDetector()
    feed(d, 0.0, 10.0, 0.2, 0.1)  # start low
    assert d.state == BREAKDOWN
    t = 10.0
    while t < 25.0:  # climb 0.2 -> 0.95, steeper than any build
        d.update(round(t, 6), 0.2 + 0.05 * (t - 10.0), 0.5)
        t += 0.1
    assert BUILD not in [to for _, _, to in d.events]


def test_time_in_state_and_reset():
    d = SectionDetector()
    assert d.time_in_state(0.0) == 0.0
    feed(d, 0.0, 5.0, 0.6, 0.55)
    assert d.time_in_state(4.9) == pytest.approx(4.9)
    d.reset()
    assert d.state == STEADY
    assert d.events == []
    assert d.time_in_state(100.0) == 0.0


def test_feed_returns_the_final_state():
    d = SectionDetector()
    assert d.feed(fake_td.energy_bass_stream()) == STEADY
