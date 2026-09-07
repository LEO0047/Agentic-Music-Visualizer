"""Tests for amv.sidecar's run loop — above all, what ``duration`` measures.

The regression these pin down: ``--duration`` is a promise about the *process*
("keep the sidecar alive this long"), while ``--speed`` scales the *track*
clock. Measuring the deadline on the scaled clock made ``--speed 20
--duration 9`` exit after 0.45 real seconds — before ``tools/fake_td.py`` had
sent a single packet, which is why every end-to-end run printed
``stopped — 0 feature messages``.

Everything here runs on fake clocks that only move when the loop sleeps, so a
nine second run and a twenty second run both finish instantly.
"""

from __future__ import annotations

import io
import json
import time

import pytest

from amv.features import FeatureBuffer
from amv.sections import BREAKDOWN, STEADY, SectionDetector
from amv.sidecar import TICK_S, ScaledClock, Sidecar

#: The compressed-run settings from the README's end-to-end recipe.
SPEED = 20.0

#: Real seconds one 10 Hz tick costs at that speed — ``ScaledClock.sleep``'s
#: conversion, and the unit the fake wall clock advances in.
REAL_TICK_S = TICK_S / SPEED  # 0.005


class FakeWall:
    """A wall clock that only moves when someone sleeps on it."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeSleep:
    """``ScaledClock.sleep`` with the real sleeping taken out.

    Takes *scaled* seconds like the real one, advances the fake wall clock by
    ``seconds / speed`` — which advances the :class:`ScaledClock` built on top
    of that wall clock by ``seconds``, i.e. by ``real * speed``. Optionally
    feeds the buffer and/or raises ``KeyboardInterrupt``, standing in for Ctrl-C
    and for TouchDesigner's 10 Hz stream respectively.
    """

    def __init__(self, wall: FakeWall, speed: float, *, feed=None, interrupt_after=None) -> None:
        self.wall = wall
        self.speed = speed
        self.feed = feed
        self.interrupt_after = interrupt_after
        self.calls = 0

    def __call__(self, seconds: float) -> None:
        self.calls += 1
        self.wall.advance(seconds / self.speed)
        if self.feed is not None:
            self.feed()
        if self.interrupt_after is not None and self.calls >= self.interrupt_after:
            raise KeyboardInterrupt


def build(wall: FakeWall, speed: float = SPEED, **kwargs) -> tuple[Sidecar, FeatureBuffer]:
    """A sidecar on a scaled clock driven by ``wall``, printing to a buffer."""
    clock = ScaledClock(speed, source=wall)
    buffer = FeatureBuffer(window_s=120.0, rate_hz=10, clock=clock)
    sidecar = Sidecar(
        buffer,
        SectionDetector(),
        None,
        clock=clock,
        out=io.StringIO(),
        **kwargs,
    )
    return sidecar, buffer


# -- the fake clock matches the real one ------------------------------------


def test_fake_sleep_matches_scaled_clock(monkeypatch):
    """The conversion the tests below rely on is the one ScaledClock uses."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)

    ScaledClock(SPEED).sleep(TICK_S)

    assert slept == [pytest.approx(REAL_TICK_S)]


def test_scaled_clock_reads_scaled_time():
    wall = FakeWall()
    clock = ScaledClock(SPEED, source=wall)

    assert clock() == 0.0
    wall.advance(9.0)
    assert clock() == pytest.approx(180.0)


# -- duration is wall-clock seconds -----------------------------------------


def test_duration_is_measured_on_the_wall_clock_not_the_scaled_one():
    wall = FakeWall()
    sidecar, _ = build(wall)
    sleep = FakeSleep(wall, SPEED)

    sidecar.run(duration=9.0, sleep=sleep, wall=wall)

    # Nine seconds of *wall* time, to within the tick it overshoots by.
    assert wall.t == pytest.approx(9.0, abs=2 * REAL_TICK_S)
    # ~1800 ticks of 5 ms — not the ~90 the scaled clock would have allowed.
    assert sleep.calls == pytest.approx(9.0 / REAL_TICK_S, abs=5)
    assert sleep.calls > 1000
    # The scaled clock is long past 9: proof the deadline did not consult it.
    assert sidecar.clock() == pytest.approx(180.0, abs=1.0)


def test_duration_ignores_speed():
    """Same wall duration, four different speeds, same real running time."""
    for speed in (1.0, 4.0, 20.0, 50.0):
        wall = FakeWall()
        sidecar, _ = build(wall, speed=speed)
        sleep = FakeSleep(wall, speed)

        sidecar.run(duration=9.0, sleep=sleep, wall=wall)

        assert wall.t == pytest.approx(9.0, abs=2 * TICK_S / speed), speed
        assert sidecar.clock() == pytest.approx(9.0 * speed, abs=2 * TICK_S), speed


def test_duration_defaults_to_the_real_monotonic_clock():
    """No ``wall`` given: the deadline is still real seconds, not scaled ones."""
    wall = FakeWall()  # drives the scaled clock only, never the deadline
    sidecar, _ = build(wall, status_hz=0.0)

    started = time.monotonic()
    sidecar.run(duration=0.05, sleep=lambda _s: wall.advance(REAL_TICK_S))
    real = time.monotonic() - started

    assert real >= 0.05
    # The scaled clock ran far ahead and did not end the run.
    assert sidecar.clock() > 0.05


# -- duration 0 runs until Ctrl-C -------------------------------------------


def test_zero_duration_loops_until_keyboard_interrupt(tmp_path):
    """Runs past any deadline the old code would have hit, then returns a count."""
    wall = FakeWall()
    log = tmp_path / "amv.jsonl"
    sidecar, buffer = build(wall, log_path=log)

    def feed() -> None:
        # A quiet passage: 4 s of track time under 0.35 energy is a breakdown.
        buffer.push("energy", 0.2)
        buffer.push("bass", 0.1)

    feed()
    sleep = FakeSleep(wall, SPEED, feed=feed, interrupt_after=200)

    count = sidecar.run(duration=0.0, sleep=sleep, wall=wall)

    assert sleep.calls == 200  # it never stopped on its own
    assert count == len(sidecar.transitions) == 1
    assert (sidecar.transitions[0]["from"], sidecar.transitions[0]["to"]) == (STEADY, BREAKDOWN)
    assert sidecar.section == BREAKDOWN
    assert f"{STEADY} → {BREAKDOWN}" in sidecar.out.getvalue()

    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [r["to"] for r in records] == [BREAKDOWN]
    # Logged in track time, which is where --speed still applies.
    assert records[0]["t"] == pytest.approx(4.0, abs=0.5)


def test_keyboard_interrupt_closes_the_log(tmp_path):
    wall = FakeWall()
    log = tmp_path / "amv.jsonl"
    sidecar, _ = build(wall, log_path=log)

    sidecar.run(duration=0.0, sleep=FakeSleep(wall, SPEED, interrupt_after=3), wall=wall)

    assert sidecar._log is None
