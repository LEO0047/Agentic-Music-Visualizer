"""Audio feature extraction for the sidecar — pure numpy, no device I/O.

This is the sidecar-side mirror of what the TouchDesigner reflex layer does per
frame (SPEC.md §3.1): a block of PCM in, ``bass`` / ``mid`` / ``high`` /
``energy`` / ``centroid`` out. TD stays the 60 fps reflex; this module exists so
the Python side can verify the audio route, drive the meter in
``tools/audio_check.py`` and later feed section detection (SPEC.md §4) without
ever needing TouchDesigner running.

Deliberately free of ``sounddevice``: everything here takes plain arrays and
plain dicts, so the whole feature path is unit-testable on synthetic signals
with no audio hardware, no CoreAudio permission prompt and no BlackHole.
:func:`find_blackhole` follows the same rule — it takes the *list* that
``sounddevice.query_devices()`` returns rather than calling it.

Three pieces of state live here because the reflex layer has them too:

* :class:`Normalizer` — "除以 set 最大值自適應" from the spec table: a per-key
  running maximum that decays, so a quiet passage slowly re-scales instead of
  pinning every bar at zero for the rest of the set.
* :class:`KickDetector` — the ``bass Slope 超門檻 → Logic`` row, with a
  refractory window so one kick is one pulse.
* the band table :data:`BANDS`, which is the single source of truth for the
  20–150 / 150–2000 / 2000–16000 Hz split.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np

__all__ = [
    "BANDS",
    "BLOCK_SECONDS",
    "FEATURE_KEYS",
    "band_energies",
    "to_mono",
    "Normalizer",
    "KickDetector",
    "find_blackhole",
]

#: Band edges in Hz, half-open ``[low, high)`` so no bin is counted twice.
BANDS: dict[str, tuple[float, float]] = {
    "bass": (20.0, 150.0),
    "mid": (150.0, 2000.0),
    "high": (2000.0, 16000.0),
}

#: The sidecar analysis block: 100 ms, i.e. the 10 Hz OSC rate of SPEC.md §3.1.
BLOCK_SECONDS: float = 0.1

#: Keys of the :func:`band_energies` dict, in display order.
FEATURE_KEYS: tuple[str, ...] = ("bass", "mid", "high", "energy", "centroid")

_ZERO_FEATURES: dict[str, float] = {key: 0.0 for key in FEATURE_KEYS}


def to_mono(block: Any) -> np.ndarray:
    """Return ``block`` as a 1-D float64 array, averaging channels if needed.

    Accepts the shapes ``sounddevice`` hands back: ``(n,)`` for mono and
    ``(n, channels)`` for interleaved multi-channel. Non-finite samples become
    zeros so one bad buffer cannot poison every downstream feature with NaN.
    """
    array = np.asarray(block, dtype=np.float64)
    if array.ndim == 2:
        array = array.mean(axis=1)
    elif array.ndim != 1:
        raise ValueError(f"expected a 1-D or 2-D block, got shape {array.shape}")
    if array.size and not np.all(np.isfinite(array)):
        array = np.nan_to_num(array, nan=0.0, posinf=0.0, neginf=0.0)
    return array


def band_energies(block: Any, sr: int) -> dict[str, float]:
    """Band magnitudes, RMS and spectral centroid for one block of audio.

    Args:
        block: PCM samples, mono ``(n,)`` or multi-channel ``(n, channels)``.
            Multi-channel input is mono-mixed by averaging.
        sr: Sample rate in Hz.

    Returns:
        ``{"bass", "mid", "high", "energy", "centroid"}``.

        * ``bass`` / ``mid`` / ``high`` — the **mean** magnitude of the ``rfft``
          bins falling inside :data:`BANDS`. A Hann window is applied first and
          the spectrum is scaled by ``2 / sum(window)``, so a full-scale sine
          sitting alone in a band reads roughly its own amplitude spread over
          that band's bin count. Mean (not sum) keeps a 130 Hz-wide band and a
          14 kHz-wide band on comparable footing.
        * ``energy`` — plain RMS of the (un-windowed) block.
        * ``centroid`` — magnitude-weighted mean frequency in Hz over the whole
          spectrum, 0.0 for silence.

    All values are **raw**: un-normalised, in whatever units the input samples
    are. Feed them through :class:`Normalizer` to get the 0–1 range the OSC
    contract wants.
    """
    mono = to_mono(block)
    n = mono.size
    if n == 0 or sr <= 0:
        return dict(_ZERO_FEATURES)

    features = dict(_ZERO_FEATURES)
    features["energy"] = float(np.sqrt(np.mean(np.square(mono))))

    window = np.hanning(n) if n > 1 else np.ones(1)
    window_sum = float(window.sum())
    spectrum = np.abs(np.fft.rfft(mono * window))
    if window_sum > 0.0:
        spectrum *= 2.0 / window_sum
    freqs = np.fft.rfftfreq(n, d=1.0 / float(sr))

    for name, (low, high) in BANDS.items():
        selected = spectrum[(freqs >= low) & (freqs < high)]
        features[name] = float(selected.mean()) if selected.size else 0.0

    total = float(spectrum.sum())
    features["centroid"] = float((freqs * spectrum).sum() / total) if total > 0.0 else 0.0
    return features


class Normalizer:
    """Per-key adaptive maximum with slow decay — maps raw features into 0–1.

    Each key keeps its own running peak. Every :meth:`update` the peak decays by
    ``decay`` and is then raised to the new value if that value is larger, so
    the scale tracks the loudest thing heard recently rather than the loudest
    thing heard all night. At the default 0.999 per 100 ms block the peak halves
    in about 70 seconds — slow enough that a quiet bar does not blow the bars up
    to full, fast enough that a set that gets quieter re-scales within a track.

    Args:
        decay: Multiplier applied to every peak per :meth:`update`, in ``(0, 1]``.
        floor: Lower bound on a peak, which also stops division by zero. Values
            at or below the floor read as (near) zero rather than as full scale.
    """

    __slots__ = ("decay", "floor", "peaks")

    def __init__(self, decay: float = 0.999, floor: float = 1e-6) -> None:
        decay = float(decay)
        floor = float(floor)
        if not 0.0 < decay <= 1.0:
            raise ValueError(f"decay must be in (0, 1], got {decay!r}")
        if floor <= 0.0:
            raise ValueError(f"floor must be > 0, got {floor!r}")
        self.decay = decay
        self.floor = floor
        self.peaks: dict[str, float] = {}

    def update(self, values: Mapping[str, float]) -> dict[str, float]:
        """Normalise every key in ``values`` and advance that key's peak.

        Keys are independent, and only the keys present in this call decay —
        pass a stable set of keys every block.
        """
        out: dict[str, float] = {}
        for key, raw in values.items():
            value = float(raw)
            if not math.isfinite(value):
                value = 0.0
            magnitude = abs(value)
            peak = max(self.peaks.get(key, self.floor) * self.decay, magnitude, self.floor)
            self.peaks[key] = peak
            out[key] = min(max(value / peak, 0.0), 1.0)
        return out

    __call__ = update

    def reset(self) -> None:
        """Forget every peak (e.g. between tracks)."""
        self.peaks.clear()


class KickDetector:
    """Rising-slope kick detector with a refractory window.

    Feed it *smoothed, normalised* bass — one value per block, the same signal
    the ``/feat/bass`` OSC address carries. It fires when bass climbs faster
    than ``threshold`` and enough time has passed since the last hit.

    Args:
        threshold: Minimum slope **per second** to count as a kick. The default
            2.0 means "bass rose by more than 0.2 during one 100 ms block",
            which is a clear transient for a 0–1 signal without firing on the
            general swell of a build.
        refractory_s: Minimum spacing between two kicks. 0.1 s caps detection at
            600 BPM while still allowing back-to-back 100 ms blocks to fire.
    """

    __slots__ = ("threshold", "refractory_s", "count", "slope", "last_kick_t", "_t", "_prev")

    _EPS = 1e-9

    def __init__(self, threshold: float = 2.0, refractory_s: float = 0.1) -> None:
        refractory_s = float(refractory_s)
        if refractory_s < 0.0:
            raise ValueError(f"refractory_s must be >= 0, got {refractory_s!r}")
        self.threshold = float(threshold)
        self.refractory_s = refractory_s
        self.count = 0
        self.slope = 0.0
        self.last_kick_t: float | None = None
        self._t = 0.0
        self._prev: float | None = None

    @property
    def t(self) -> float:
        """Current time on the detector's own clock, in seconds."""
        return self._t

    def update(self, bass: float, dt: float | None = None, *, t: float | None = None) -> int:
        """Feed one block of smoothed bass; return 1 on a kick, else 0.

        Args:
            bass: Smoothed, normalised bass for this block.
            dt: Block duration in seconds. Defaults to :data:`BLOCK_SECONDS`.
                In this mode the clock accumulates, with the first call landing
                at ``dt`` (the end of the first block).
            t: Absolute timestamp for this block, as an alternative to ``dt``.
                Takes precedence when both are given.

        The first call only primes the previous value — a slope needs two
        blocks — so it always returns 0.
        """
        value = float(bass)
        if not math.isfinite(value):
            value = 0.0

        if t is not None:
            now = float(t)
            step = now - self._t if self._prev is not None else 0.0
            self._t = now
        else:
            step = BLOCK_SECONDS if dt is None else float(dt)
            self._t += step

        prev, self._prev = self._prev, value
        if prev is None or step <= 0.0:
            self.slope = 0.0
            return 0

        self.slope = (value - prev) / step
        if self.slope < self.threshold:
            return 0
        if (
            self.last_kick_t is not None
            and (self._t - self.last_kick_t) + self._EPS < self.refractory_s
        ):
            return 0

        self.last_kick_t = self._t
        self.count += 1
        return 1

    __call__ = update

    def reset(self) -> None:
        """Clear history, counter and clock."""
        self.count = 0
        self.slope = 0.0
        self.last_kick_t = None
        self._t = 0.0
        self._prev = None


def _device_field(device: Any, key: str, default: Any) -> Any:
    if isinstance(device, Mapping):
        value = device.get(key, default)
    else:  # tolerate namedtuple-ish / attribute-style entries
        value = getattr(device, key, default)
    return default if value is None else value


def find_blackhole(devices: Iterable[Any], match: str = "blackhole") -> int | None:
    """Return the index of the input-capable BlackHole device, or ``None``.

    Takes the list ``sounddevice.query_devices()`` returns — each entry a
    mapping with at least ``name`` and ``max_input_channels`` — so device
    selection stays testable without the library or the driver installed.

    Only devices that can be *recorded from* qualify: BlackHole is a loopback
    driver and the sidecar's job is reading what was played into it. When more
    than one matches, a name containing ``2ch`` wins (SPEC.md Phase 1 pins
    ``BlackHole 2ch``); otherwise the lowest-indexed match wins.
    """
    needle = match.lower()
    fallback: int | None = None
    for position, device in enumerate(devices):
        name = str(_device_field(device, "name", ""))
        if needle not in name.lower():
            continue
        try:
            inputs = int(_device_field(device, "max_input_channels", 0))
        except (TypeError, ValueError):
            continue
        if inputs < 1:
            continue
        try:
            index = int(_device_field(device, "index", position))
        except (TypeError, ValueError):
            index = position
        if "2ch" in name.lower():
            return index
        if fallback is None:
            fallback = index
    return fallback
