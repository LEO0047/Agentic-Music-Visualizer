"""Rolling feature buffer — the sidecar's short-term memory (SPEC §3.1, §4).

TouchDesigner pushes ``/feat/*`` at 10 Hz. Nothing downstream cares about a
single sample: the section detector wants trends over seconds and the director
prompt wants a handful of rounded numbers describing the last two minutes.
:class:`FeatureBuffer` is the one place that turns a stream of samples into
both.

Design notes:

* One bounded deque per key (``window_s * rate_hz`` samples), so memory is
  fixed no matter how long the set runs and stale samples fall off the back
  without anyone sweeping them.
* Every read is time-based (``avg("energy", 30)``), never index-based, so a
  dropped OSC packet or a jittery sender changes the accuracy of an answer but
  never its meaning.
* A lock guards the deques: :class:`~amv.osc_io.FeatureReceiver` pushes from
  the OSC server thread while the main loop reads.
* The clock is injectable so tests can drive 120 s of history in a millisecond.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Deque, Iterable

__all__ = ["FeatureBuffer", "TREND_EPSILON", "KICK_THRESHOLD", "SUMMARY_KEYS"]

#: ``energy_30s`` must beat ``energy_120s`` by this much to count as a trend.
#: Straight from the blueprint sketch in ``docs/index.html``.
TREND_EPSILON = 0.05

#: ``/feat/kick`` is an int 0/1 pulse; anything at or above this counts as a hit.
KICK_THRESHOLD = 0.5

#: Keys :meth:`FeatureBuffer.summary` always emits, in order.
SUMMARY_KEYS: tuple[str, ...] = (
    "bass",
    "mid",
    "high",
    "energy",
    "energy_30s",
    "energy_120s",
    "energy_trend_30s",
    "kicks_per_min",
    "centroid",
)


class FeatureBuffer:
    """Bounded, time-indexed history of the ``/feat/*`` streams.

    Args:
        window_s: How much history to keep, in seconds (default 120 — the
            longest window the director prompt asks for).
        rate_hz: Expected push rate; with ``window_s`` it sets the deque bound.
        clock: Callable returning "now" in seconds. Defaults to
            :func:`time.monotonic`; tests inject a fake so they can fabricate
            two minutes of history without sleeping.
    """

    def __init__(
        self,
        window_s: float = 120.0,
        rate_hz: int = 10,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if window_s <= 0:
            raise ValueError("window_s must be positive")
        if rate_hz <= 0:
            raise ValueError("rate_hz must be positive")
        self.window_s = float(window_s)
        self.rate_hz = int(rate_hz)
        self.maxlen = max(1, int(round(self.window_s * self.rate_hz)))
        self._clock = clock
        self._lock = threading.Lock()
        self._series: dict[str, Deque[tuple[float, float]]] = {}

    # -- writing ------------------------------------------------------------

    def push(self, key: str, value: float, t: float | None = None) -> None:
        """Record one sample. ``t`` defaults to the buffer's clock."""
        ts = self._clock() if t is None else float(t)
        with self._lock:
            series = self._series.get(key)
            if series is None:
                series = self._series[key] = deque(maxlen=self.maxlen)
            series.append((ts, float(value)))

    def clear(self) -> None:
        """Drop all history (used between takes, and by tests)."""
        with self._lock:
            self._series.clear()

    def keys(self) -> tuple[str, ...]:
        """Feature names seen so far."""
        with self._lock:
            return tuple(self._series)

    def __len__(self) -> int:
        with self._lock:
            return sum(len(s) for s in self._series.values())

    # -- reading ------------------------------------------------------------

    def _window(self, key: str, seconds: float) -> list[tuple[float, float]]:
        """Samples for ``key`` no older than ``seconds``, oldest first."""
        now = self._clock()
        cutoff = now - float(seconds)
        with self._lock:
            series = self._series.get(key)
            if not series:
                return []
            # Deques are append-ordered, so walking from the back and stopping
            # at the first stale sample touches only what we need.
            out: list[tuple[float, float]] = []
            for ts, value in reversed(series):
                if ts < cutoff:
                    break
                out.append((ts, value))
        out.reverse()
        return out

    def latest(self, key: str, default: float | None = None) -> float | None:
        """Most recent value for ``key``, or ``default`` if never seen."""
        with self._lock:
            series = self._series.get(key)
            if not series:
                return default
            return series[-1][1]

    def latest_t(self, key: str) -> float | None:
        """Timestamp of the most recent sample for ``key``, or ``None``."""
        with self._lock:
            series = self._series.get(key)
            if not series:
                return None
            return series[-1][0]

    def avg(self, key: str, seconds: float) -> float:
        """Mean value of ``key`` over the last ``seconds``. ``0.0`` if empty."""
        window = self._window(key, seconds)
        if not window:
            return 0.0
        return sum(v for _, v in window) / len(window)

    def slope(self, key: str, seconds: float) -> float:
        """Least-squares slope of ``key`` over the last ``seconds``, per second.

        Returns ``0.0`` when there is nothing to fit (fewer than two samples,
        or every sample at the same instant). Least squares rather than
        end-minus-start because a 10 Hz feature stream is noisy and a single
        outlier at either end would otherwise decide whether we are in a build.
        """
        window = self._window(key, seconds)
        n = len(window)
        if n < 2:
            return 0.0
        mean_t = sum(t for t, _ in window) / n
        mean_v = sum(v for _, v in window) / n
        sxx = 0.0
        sxy = 0.0
        for t, v in window:
            dt = t - mean_t
            sxx += dt * dt
            sxy += dt * (v - mean_v)
        if sxx <= 0.0:
            return 0.0
        return sxy / sxx

    def span(self, key: str, seconds: float) -> float:
        """Seconds actually covered by the samples inside the window."""
        window = self._window(key, seconds)
        if len(window) < 2:
            return 0.0
        return window[-1][0] - window[0][0]

    def count_above(self, key: str, seconds: float, threshold: float) -> int:
        """How many samples of ``key`` in the window are ``>= threshold``."""
        return sum(1 for _, v in self._window(key, seconds) if v >= threshold)

    def rate_per_min(
        self, key: str, seconds: float = 30.0, threshold: float = KICK_THRESHOLD
    ) -> float:
        """Pulses per minute for a 0/1 stream such as ``/feat/kick``.

        Divides by the time actually covered rather than by ``seconds``, so a
        set that started 8 seconds ago reports the real tempo instead of a
        quarter of it. Below one second of history there is nothing to
        extrapolate from and the answer is ``0.0``.
        """
        window = self._window(key, seconds)
        if len(window) < 2:
            return 0.0
        covered = window[-1][0] - window[0][0]
        if covered < 1.0:
            return 0.0
        hits = sum(1 for _, v in window if v >= threshold)
        return hits * 60.0 / covered

    # -- the director's view ------------------------------------------------

    def energy_trend(self, epsilon: float = TREND_EPSILON) -> str:
        """``rising`` / ``falling`` / ``flat`` from 30 s vs 120 s energy."""
        e30 = self.avg("energy", 30)
        e120 = self.avg("energy", 120)
        if e30 > e120 + epsilon:
            return "rising"
        if e30 < e120 - epsilon:
            return "falling"
        return "flat"

    def summary(self) -> dict:
        """The compact dict handed to the director prompt (SPEC §3.1).

        Everything is rounded to two decimals: the LLM gains nothing from the
        sixth digit of a normalised RMS, and the prompt gets shorter.
        """
        return {
            "bass": round(self.avg("bass", 1), 2),
            "mid": round(self.avg("mid", 1), 2),
            "high": round(self.avg("high", 1), 2),
            "energy": round(self.avg("energy", 1), 2),
            "energy_30s": round(self.avg("energy", 30), 2),
            "energy_120s": round(self.avg("energy", 120), 2),
            "energy_trend_30s": self.energy_trend(),
            "kicks_per_min": round(self.rate_per_min("kick", 30), 1),
            "centroid": round(self.avg("centroid", 1), 2),
        }

    # -- bulk load (tests, replaying a log) ---------------------------------

    def extend(self, samples: Iterable[tuple[float, str, float]]) -> None:
        """Push many ``(t, key, value)`` triples at once."""
        for t, key, value in samples:
            self.push(key, value, t)
