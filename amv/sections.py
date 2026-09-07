"""Section detection — steady / build / drop / breakdown (SPEC §4).

SPEC states the rules in one line each::

    breakdown : energy < 0.35 for more than 4 s
    build     : energy rising for 20 s
    drop      : after a breakdown (or a build), bass jumps back to >= 0.7
    otherwise : steady

Turning those four lines into something that survives a real track is mostly
about *not* flapping. Three kinds of hysteresis do that work here:

* **Time**: breakdown needs 4 s of continuous low energy to arm and `drop` is
  a momentary state held for :attr:`drop_hold_s` before falling back to steady,
  so a single dark bar or one loud frame cannot move the state machine.
* **Level**: leaving a breakdown needs energy above ``0.35 + 0.05``, and
  staying in a build only needs a quarter of the slope that entering one did.
* **Shape**: a build is not merely "the least-squares slope is positive". The
  window must also have risen end-to-end and must contain no breakdown-level
  samples — otherwise the climb *out* of every breakdown reads as a build, and
  the state machine announces a build in the middle of the loudest part of the
  track.

The detector owns its own small ring of samples rather than reading a
:class:`~amv.features.FeatureBuffer`, so it can be replayed offline against a
synthetic timeline (``tools/fake_td.py``) with no OSC and no clock.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Iterable

__all__ = ["SectionDetector", "STEADY", "BUILD", "DROP", "BREAKDOWN", "SECTIONS"]

STEADY = "steady"
BUILD = "build"
DROP = "drop"
BREAKDOWN = "breakdown"

#: Every value :meth:`SectionDetector.update` can return.
SECTIONS: tuple[str, ...] = (STEADY, BUILD, DROP, BREAKDOWN)


class SectionDetector:
    """Streaming state machine over ``(t, energy, bass)`` samples.

    Feed it at whatever rate the features arrive (10 Hz from TouchDesigner)
    and read :attr:`state`, :attr:`events` and :meth:`time_in_state`.

    Args:
        breakdown_energy: Energy below which a breakdown starts counting.
        breakdown_hold_s: How long energy must stay low before we believe it.
        breakdown_exit_margin: Extra energy needed to *leave* a breakdown.
        build_window_s: Window the rising trend is measured over.
        build_min_span_s: Minimum history inside that window before a build
            can be called at all — stops start-up noise from looking like one.
        build_cooldown_s: How long after a drop a build is refused. A window
            that still contains the drop is not a clean twenty second rise;
            without this, the climb into every drop keeps reading as a build
            for another twenty seconds after the drop has landed. Defaults to
            ``build_window_s``.
        build_slope: Least-squares slope (per second) that enters a build.
        build_slope_exit: Slope that keeps an established build alive.
        build_min_rise: Required rise from the start of the window to its end.
        build_edge_s: Length of the two edges averaged for that rise test.
        drop_bass: Bass level that counts as the drop hitting.
        drop_arm_s: How long after a build or breakdown a drop can still fire.
        drop_hold_s: How long ``drop`` is reported before returning to steady.
        drop_lookback_s / drop_recent_s: The bass "jump" test looks at
            ``[t - drop_lookback_s, t - drop_recent_s]``; the drop only counts
            if bass was *below* the threshold there. Without it, eight bars of
            sustained 0.9 bass would re-trigger a drop every two seconds.
    """

    def __init__(
        self,
        *,
        breakdown_energy: float = 0.35,
        breakdown_hold_s: float = 4.0,
        breakdown_exit_margin: float = 0.05,
        build_window_s: float = 20.0,
        build_min_span_s: float = 10.0,
        build_cooldown_s: float | None = None,
        build_slope: float = 0.004,
        build_slope_exit: float = 0.001,
        build_min_rise: float = 0.08,
        build_edge_s: float = 2.0,
        drop_bass: float = 0.7,
        drop_arm_s: float = 4.0,
        drop_hold_s: float = 2.0,
        drop_lookback_s: float = 2.0,
        drop_recent_s: float = 0.4,
    ) -> None:
        self.breakdown_energy = breakdown_energy
        self.breakdown_hold_s = breakdown_hold_s
        self.breakdown_exit_margin = breakdown_exit_margin
        self.build_window_s = build_window_s
        self.build_min_span_s = build_min_span_s
        self.build_cooldown_s = (
            build_window_s if build_cooldown_s is None else build_cooldown_s
        )
        self.build_slope = build_slope
        self.build_slope_exit = build_slope_exit
        self.build_min_rise = build_min_rise
        self.build_edge_s = build_edge_s
        self.drop_bass = drop_bass
        self.drop_arm_s = drop_arm_s
        self.drop_hold_s = drop_hold_s
        self.drop_lookback_s = drop_lookback_s
        self.drop_recent_s = drop_recent_s

        self._history_s = max(build_window_s, drop_lookback_s, breakdown_hold_s) + 2.0
        self._samples: Deque[tuple[float, float, float]] = deque()
        self.state: str = STEADY
        self._state_since: float | None = None
        self._low_since: float | None = None
        self._armed_until: float = float("-inf")
        self._last_drop_t: float = float("-inf")
        self.events: list[tuple[float, str, str]] = []

    # -- lifecycle ----------------------------------------------------------

    def reset(self) -> None:
        """Back to a fresh ``steady``, history and events dropped."""
        self._samples.clear()
        self.state = STEADY
        self._state_since = None
        self._low_since = None
        self._armed_until = float("-inf")
        self._last_drop_t = float("-inf")
        self.events = []

    def time_in_state(self, t: float) -> float:
        """Seconds spent in the current state as of ``t``."""
        if self._state_since is None:
            return 0.0
        return max(0.0, t - self._state_since)

    # -- the state machine --------------------------------------------------

    def update(self, t: float, energy: float, bass: float) -> str:
        """Feed one sample and return the (possibly new) section name."""
        t = float(t)
        energy = float(energy)
        bass = float(bass)
        self._samples.append((t, energy, bass))
        cutoff = t - self._history_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        if self._state_since is None:
            self._state_since = t

        self._track_low_energy(t, energy)
        new_state = self._decide(t, energy, bass)

        if new_state != self.state:
            self.events.append((t, self.state, new_state))
            self.state = new_state
            self._state_since = t
        if self.state in (BUILD, BREAKDOWN):
            # A drop is still allowed to fire for a short while after the
            # build or breakdown that set it up has ended.
            self._armed_until = t + self.drop_arm_s
        elif self.state == DROP:
            self._last_drop_t = t
        return self.state

    def feed(self, samples: Iterable[tuple[float, float, float]]) -> str:
        """Replay many ``(t, energy, bass)`` triples; returns the final state."""
        state = self.state
        for t, energy, bass in samples:
            state = self.update(t, energy, bass)
        return state

    def _track_low_energy(self, t: float, energy: float) -> None:
        threshold = self.breakdown_energy
        if self.state == BREAKDOWN:
            threshold += self.breakdown_exit_margin
        if energy < threshold:
            if self._low_since is None:
                self._low_since = t
        else:
            self._low_since = None

    def _decide(self, t: float, energy: float, bass: float) -> str:
        # 1. A drop is momentary: hold it, then re-evaluate from scratch.
        if self.state == DROP and self.time_in_state(t) < self.drop_hold_s:
            return DROP

        # 2. Drop: armed by a build or a breakdown, confirmed by a bass jump.
        armed = self.state in (BUILD, BREAKDOWN) or t <= self._armed_until
        if armed and bass >= self.drop_bass and self._bass_was_below(t):
            return DROP

        # 3. Breakdown: 4 s of low energy to enter, hysteresis band to leave.
        if self._low_since is not None:
            if self.state == BREAKDOWN or (t - self._low_since) > self.breakdown_hold_s:
                return BREAKDOWN

        # 4. Build: a rising 20 s window that never dipped to breakdown level.
        if self._is_build(t, energy):
            return BUILD

        return STEADY

    # -- conditions ---------------------------------------------------------

    def _window(self, t: float, seconds: float) -> list[tuple[float, float, float]]:
        cutoff = t - seconds
        return [s for s in self._samples if s[0] >= cutoff]

    def _bass_was_below(self, t: float) -> bool:
        """Was bass below the drop threshold just before now?

        ``True`` only if there are samples in the lookback slice and their mean
        is below :attr:`drop_bass` — an unknown past is not a jump.
        """
        lo = t - self.drop_lookback_s
        hi = t - self.drop_recent_s
        values = [b for ts, _, b in self._samples if lo <= ts <= hi]
        if not values:
            return False
        return sum(values) / len(values) < self.drop_bass

    def _is_build(self, t: float, energy: float) -> bool:
        if energy < self.breakdown_energy:
            return False
        window = self._window(t, self.build_window_s)
        if len(window) < 2:
            return False
        span = window[-1][0] - window[0][0]
        if span < self.build_min_span_s:
            return False
        if min(e for _, e, _ in window) < self.breakdown_energy:
            # The climb out of a breakdown is not a build.
            return False
        if t - self._last_drop_t < self.build_cooldown_s:
            # Nor is the tail of a drop still sitting inside the window.
            return False

        established = self.state == BUILD
        slope_min = self.build_slope_exit if established else self.build_slope
        if self._slope(window) <= slope_min:
            return False

        rise_min = 0.0 if established else self.build_min_rise
        start_t = window[0][0]
        end_t = window[-1][0]
        head = [e for ts, e, _ in window if ts <= start_t + self.build_edge_s]
        tail = [e for ts, e, _ in window if ts >= end_t - self.build_edge_s]
        if not head or not tail:
            return False
        rise = sum(tail) / len(tail) - sum(head) / len(head)
        return rise >= rise_min

    @staticmethod
    def _slope(window: list[tuple[float, float, float]]) -> float:
        n = len(window)
        mean_t = sum(s[0] for s in window) / n
        mean_e = sum(s[1] for s in window) / n
        sxx = 0.0
        sxy = 0.0
        for ts, energy, _ in window:
            dt = ts - mean_t
            sxx += dt * dt
            sxy += dt * (energy - mean_e)
        if sxx <= 0.0:
            return 0.0
        return sxy / sxx
