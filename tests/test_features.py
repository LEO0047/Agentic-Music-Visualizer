"""Tests for amv.features — the rolling window the director reads from."""

from __future__ import annotations

import pytest

from amv.features import KICK_THRESHOLD, SUMMARY_KEYS, TREND_EPSILON, FeatureBuffer


class FakeClock:
    """A clock the test drives by hand, so 120 s of history costs nothing."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


def buffer_with(clock: FakeClock, **kwargs) -> FeatureBuffer:
    return FeatureBuffer(clock=clock, **kwargs)


def fill(buf: FeatureBuffer, clock: FakeClock, key: str, values, step: float = 0.1) -> None:
    """Push ``values`` at ``step`` seconds apart, leaving the clock at the end."""
    for value in values:
        clock.advance(step)
        buf.push(key, value)


# -- bookkeeping ------------------------------------------------------------


def test_deque_is_bounded_to_the_window():
    clock = FakeClock()
    buf = buffer_with(clock, window_s=120.0, rate_hz=10)
    assert buf.maxlen == 1200
    fill(buf, clock, "energy", [0.5] * 1500)
    assert len(buf) == 1200


def test_push_defaults_to_the_injected_clock():
    clock = FakeClock(41.0)
    buf = buffer_with(clock)
    buf.push("bass", 0.5)
    assert buf.latest_t("bass") == 41.0
    buf.push("bass", 0.7, t=99.0)
    assert buf.latest_t("bass") == 99.0
    assert buf.latest("bass") == 0.7


def test_unknown_keys_are_quiet():
    buf = buffer_with(FakeClock())
    assert buf.latest("nope") is None
    assert buf.latest("nope", 0.0) == 0.0
    assert buf.avg("nope", 30) == 0.0
    assert buf.slope("nope", 30) == 0.0
    assert buf.rate_per_min("nope") == 0.0


def test_rejects_nonsense_geometry():
    with pytest.raises(ValueError):
        FeatureBuffer(window_s=0)
    with pytest.raises(ValueError):
        FeatureBuffer(rate_hz=0)


# -- averages ---------------------------------------------------------------


def test_avg_only_sees_its_window():
    clock = FakeClock()
    buf = buffer_with(clock)
    fill(buf, clock, "energy", [0.2] * 100)  # t 0.1 .. 10.0
    fill(buf, clock, "energy", [0.8] * 10)  # t 10.1 .. 11.0
    # The window edge is inclusive (``now - seconds`` still counts), so the 1 s
    # window is the ten loud samples plus the quiet one sitting exactly on 10.0.
    assert buf.avg("energy", 1) == pytest.approx((0.2 + 0.8 * 10) / 11)
    assert buf.avg("energy", 11) == pytest.approx((0.2 * 100 + 0.8 * 10) / 110)


def test_avg_ignores_samples_that_fell_out_of_the_window():
    clock = FakeClock()
    buf = buffer_with(clock)
    fill(buf, clock, "energy", [1.0] * 10)
    clock.advance(60.0)
    assert buf.avg("energy", 30) == 0.0
    assert buf.avg("energy", 120) == pytest.approx(1.0)


# -- slope ------------------------------------------------------------------


def test_slope_of_a_clean_ramp_is_the_ramp():
    clock = FakeClock()
    buf = buffer_with(clock)
    # 0.6 -> 0.85 over 20 s is 0.0125 per second, the fake_td build ramp.
    fill(buf, clock, "energy", [0.6 + 0.0125 * (i * 0.1) for i in range(200)])
    assert buf.slope("energy", 20) == pytest.approx(0.0125, abs=1e-6)


def test_slope_is_negative_falling_and_zero_flat():
    clock = FakeClock()
    buf = buffer_with(clock)
    fill(buf, clock, "energy", [0.9 - 0.01 * (i * 0.1) for i in range(200)])
    assert buf.slope("energy", 20) == pytest.approx(-0.01, abs=1e-6)
    clock2 = FakeClock()
    flat = buffer_with(clock2)
    fill(flat, clock2, "energy", [0.6] * 200)
    assert flat.slope("energy", 20) == pytest.approx(0.0, abs=1e-9)


def test_slope_needs_two_distinct_instants():
    clock = FakeClock()
    buf = buffer_with(clock)
    buf.push("energy", 0.4)
    assert buf.slope("energy", 20) == 0.0
    buf.push("energy", 0.9)  # same timestamp, no time base to fit against
    assert buf.slope("energy", 20) == 0.0


def test_slope_shrugs_off_a_single_outlier():
    """Least squares, not end-minus-start: one bad final sample must not lie."""
    clock = FakeClock()
    buf = buffer_with(clock)
    fill(buf, clock, "energy", [0.6] * 199)
    fill(buf, clock, "energy", [1.0])
    assert buf.slope("energy", 20) == pytest.approx(0.0, abs=0.005)


# -- trend ------------------------------------------------------------------


def test_energy_trend_uses_the_blueprint_epsilon():
    clock = FakeClock()
    buf = buffer_with(clock)
    fill(buf, clock, "energy", [0.3] * 900)  # 90 s of quiet
    fill(buf, clock, "energy", [0.9] * 300)  # 30 s of loud
    assert buf.avg("energy", 30) > buf.avg("energy", 120) + TREND_EPSILON
    assert buf.energy_trend() == "rising"
    assert buf.summary()["energy_trend_30s"] == "rising"


def test_energy_trend_falling_and_flat():
    clock = FakeClock()
    buf = buffer_with(clock)
    fill(buf, clock, "energy", [0.9] * 900)
    fill(buf, clock, "energy", [0.3] * 300)
    assert buf.energy_trend() == "falling"

    clock = FakeClock()
    steady = buffer_with(clock)
    fill(steady, clock, "energy", [0.6] * 1200)
    assert steady.energy_trend() == "flat"


def test_trend_epsilon_is_a_deadband_not_a_hair_trigger():
    clock = FakeClock()
    buf = buffer_with(clock)
    fill(buf, clock, "energy", [0.60] * 900)
    fill(buf, clock, "energy", [0.60 + 3 * TREND_EPSILON] * 300)  # +0.15 over 30 s
    e30, e120 = buf.avg("energy", 30), buf.avg("energy", 120)
    assert e30 - e120 > TREND_EPSILON
    assert buf.energy_trend() == "rising"
    # Same shape, a nudge instead of a lift: still flat.
    assert buf.energy_trend(epsilon=1.0) == "flat"


# -- kicks ------------------------------------------------------------------


def test_kicks_per_min_reads_the_bpm_grid():
    clock = FakeClock()
    buf = buffer_with(clock)
    beat = 60.0 / 145.0
    next_beat = beat
    for i in range(300):  # 30 s at 10 Hz
        t = clock.advance(0.1)
        if t >= next_beat:
            buf.push("kick", 1.0)
            next_beat += beat
        else:
            buf.push("kick", 0.0)
    assert buf.rate_per_min("kick", 30) == pytest.approx(145.0, abs=3.0)
    assert buf.summary()["kicks_per_min"] == pytest.approx(145.0, abs=3.0)


def test_kicks_per_min_extrapolates_from_a_short_history():
    """Eight seconds in, the answer is the tempo — not a quarter of it."""
    clock = FakeClock()
    buf = buffer_with(clock)
    for i in range(80):  # 8 s
        clock.advance(0.1)
        buf.push("kick", 1.0 if i % 5 == 0 else 0.0)  # 2 Hz = 120 / min
    assert buf.rate_per_min("kick", 30) == pytest.approx(120.0, abs=5.0)


def test_kicks_per_min_is_zero_before_there_is_anything_to_extrapolate():
    clock = FakeClock()
    buf = buffer_with(clock)
    for _ in range(5):
        clock.advance(0.1)
        buf.push("kick", 1.0)
    assert buf.rate_per_min("kick", 30) == 0.0


def test_count_above_uses_the_kick_threshold():
    clock = FakeClock()
    buf = buffer_with(clock)
    fill(buf, clock, "kick", [0.0, 1.0, 0.0, 1.0, 0.4])
    assert buf.count_above("kick", 30, KICK_THRESHOLD) == 2


# -- summary ----------------------------------------------------------------


def test_summary_shape_and_rounding():
    clock = FakeClock()
    buf = buffer_with(clock)
    for key, value in (("bass", 0.5551), ("mid", 0.4449), ("high", 0.3), ("centroid", 2201.567)):
        fill(buf, clock, key, [value] * 10, step=0.0)
    fill(buf, clock, "energy", [0.6] * 10, step=0.01)

    summary = buf.summary()
    assert tuple(summary) == SUMMARY_KEYS
    assert summary["bass"] == 0.56
    assert summary["mid"] == 0.44
    assert summary["centroid"] == 2201.57
    assert summary["energy_trend_30s"] in ("rising", "falling", "flat")


def test_summary_on_an_empty_buffer_is_all_zeros_and_flat():
    summary = FeatureBuffer(clock=FakeClock()).summary()
    assert summary["energy"] == 0.0
    assert summary["kicks_per_min"] == 0.0
    assert summary["energy_trend_30s"] == "flat"


def test_extend_bulk_loads_triples():
    clock = FakeClock(10.0)
    buf = buffer_with(clock)
    buf.extend((9.0 + i * 0.1, "energy", 0.5) for i in range(10))
    assert buf.avg("energy", 2) == pytest.approx(0.5)
    assert buf.keys() == ("energy",)
