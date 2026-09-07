"""Tests for amv.audio_features — synthetic signals only, no audio hardware.

Everything here is generated with numpy: no ``sounddevice``, no BlackHole, no
CoreAudio permission prompt. That is the point of keeping device I/O out of the
module — the feature path can be proved on a machine with no sound card at all.
"""

from __future__ import annotations

import numpy as np
import pytest

from amv.audio_features import (
    BANDS,
    BLOCK_SECONDS,
    FEATURE_KEYS,
    KickDetector,
    Normalizer,
    band_energies,
    find_blackhole,
    to_mono,
)

SR = 48000
BLOCK = int(SR * BLOCK_SECONDS)  # 4800 samples = 100 ms


def sine(freq: float, n: int = BLOCK, sr: int = SR, amplitude: float = 0.5) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / sr
    return amplitude * np.sin(2.0 * np.pi * freq * t)


# -- band_energies ----------------------------------------------------------


def test_returns_the_documented_keys():
    features = band_energies(sine(440.0), SR)
    assert set(features) == set(FEATURE_KEYS)
    assert all(isinstance(value, float) for value in features.values())


@pytest.mark.parametrize(
    ("freq", "expected"),
    [(60.0, "bass"), (800.0, "mid"), (5000.0, "high")],
)
def test_a_tone_lights_up_its_own_band(freq, expected):
    features = band_energies(sine(freq), SR)
    bands = {name: features[name] for name in BANDS}
    assert max(bands, key=bands.__getitem__) == expected
    others = [value for name, value in bands.items() if name != expected]
    assert all(value < features[expected] * 0.05 for value in others)


def test_bands_are_raw_not_normalised():
    """Louder in, larger out — nothing here clamps to 0–1."""
    quiet = band_energies(sine(60.0, amplitude=0.1), SR)
    loud = band_energies(sine(60.0, amplitude=1.0), SR)
    assert loud["bass"] == pytest.approx(quiet["bass"] * 10.0, rel=1e-6)
    assert loud["bass"] > 1.0 or loud["energy"] > 0.5


def test_energy_is_the_block_rms():
    features = band_energies(sine(440.0, amplitude=0.8), SR)
    assert features["energy"] == pytest.approx(0.8 / np.sqrt(2.0), rel=1e-3)


@pytest.mark.parametrize("freq", [60.0, 5000.0])
def test_centroid_sits_on_a_lone_tone(freq):
    features = band_energies(sine(freq), SR)
    assert features["centroid"] == pytest.approx(freq, rel=0.02)


def test_white_noise_centroid_is_mid_spectrum():
    noise = np.random.default_rng(20260908).normal(0.0, 0.2, BLOCK)
    centroid = band_energies(noise, SR)["centroid"]
    nyquist = SR / 2
    # A flat spectrum averages to half of Nyquist; allow a generous margin.
    assert 0.35 * nyquist < centroid < 0.65 * nyquist
    assert centroid > band_energies(sine(60.0), SR)["centroid"]


def test_silence_and_empty_blocks_are_all_zero():
    for block in (np.zeros(BLOCK), np.zeros((0, 2)), np.array([])):
        assert band_energies(block, SR) == dict.fromkeys(FEATURE_KEYS, 0.0)
    assert band_energies(sine(440.0), 0) == dict.fromkeys(FEATURE_KEYS, 0.0)


def test_stereo_is_mono_mixed():
    left = sine(60.0)
    duplicated = np.stack([left, left], axis=1)
    assert band_energies(duplicated, SR)["bass"] == pytest.approx(
        band_energies(left, SR)["bass"], rel=1e-9
    )
    # Signal on one channel only averages down to half.
    one_sided = np.stack([left, np.zeros_like(left)], axis=1)
    assert band_energies(one_sided, SR)["bass"] == pytest.approx(
        band_energies(left, SR)["bass"] * 0.5, rel=1e-9
    )


def test_to_mono_scrubs_nan_and_rejects_3d():
    assert to_mono(np.array([1.0, np.nan, np.inf])).tolist() == [1.0, 0.0, 0.0]
    with pytest.raises(ValueError):
        to_mono(np.zeros((2, 2, 2)))


# -- Normalizer -------------------------------------------------------------


def test_normalizer_output_stays_in_unit_range():
    normalizer = Normalizer()
    rng = np.random.default_rng(7)
    for value in rng.uniform(0.0, 50.0, 200):
        out = normalizer.update({"bass": float(value)})
        assert 0.0 <= out["bass"] <= 1.0


def test_a_new_peak_reads_as_one_and_smaller_values_scale_against_it():
    normalizer = Normalizer()
    assert normalizer.update({"bass": 4.0})["bass"] == pytest.approx(1.0)
    assert normalizer.update({"bass": 2.0})["bass"] == pytest.approx(0.5, abs=1e-3)
    assert normalizer.update({"bass": 8.0})["bass"] == pytest.approx(1.0)


def test_peaks_decay_so_a_quiet_passage_rescales():
    normalizer = Normalizer(decay=0.999)
    normalizer.update({"bass": 1.0})
    assert normalizer.peaks["bass"] == pytest.approx(1.0)
    for _ in range(200):
        normalizer.update({"bass": 0.0})
    assert normalizer.peaks["bass"] == pytest.approx(0.999**200, rel=1e-6)
    assert normalizer.peaks["bass"] < 1.0
    # The same input now reads louder than it did against the old peak.
    assert normalizer.update({"bass": 0.5})["bass"] > 0.5


def test_keys_are_independent_and_reset_clears_them():
    normalizer = Normalizer()
    out = normalizer.update({"bass": 10.0, "high": 0.01})
    assert out["bass"] == pytest.approx(1.0)
    assert out["high"] == pytest.approx(1.0)
    assert normalizer.peaks["bass"] != normalizer.peaks["high"]
    normalizer.reset()
    assert normalizer.peaks == {}


def test_silence_reads_zero_not_full_scale():
    normalizer = Normalizer()
    assert normalizer.update({"bass": 0.0})["bass"] == 0.0


def test_normalizer_survives_nan_and_negatives():
    normalizer = Normalizer()
    assert normalizer.update({"bass": float("nan")})["bass"] == 0.0
    assert normalizer.update({"bass": -3.0})["bass"] == 0.0


@pytest.mark.parametrize(("decay", "floor"), [(0.0, 1e-6), (1.5, 1e-6), (0.999, 0.0)])
def test_normalizer_rejects_nonsense_settings(decay, floor):
    with pytest.raises(ValueError):
        Normalizer(decay=decay, floor=floor)


# -- KickDetector -----------------------------------------------------------


def impulse_train(bpm: float, seconds: float, block_s: float = BLOCK_SECONDS) -> np.ndarray:
    """One full-scale block per beat, silence in between."""
    n_blocks = int(round(seconds / block_s))
    bass = np.zeros(n_blocks)
    interval = 60.0 / bpm
    beat = 0.0
    while beat < seconds:
        bass[int(beat / block_s)] = 1.0
        beat += interval
    return bass


def run(detector: KickDetector, bass: np.ndarray, dt: float = BLOCK_SECONDS) -> list[float]:
    return [detector.t for value in bass if detector.update(float(value), dt=dt)]


def test_140_bpm_impulse_train_is_counted():
    seconds = 12.0
    bass = impulse_train(140.0, seconds)
    detector = KickDetector()
    times = run(detector, bass)

    expected = int(bass[1:].sum())  # the first block only primes the slope
    assert abs(len(times) - expected) <= 1
    assert detector.count == len(times)
    assert len(times) / seconds * 60.0 == pytest.approx(140.0, abs=10.0)


def test_kicks_respect_the_refractory_window():
    detector = KickDetector(refractory_s=0.5)
    bass = np.array([0.0, 1.0] * 25)  # a rising edge every 0.2 s
    times = run(detector, bass)
    assert len(times) >= 2
    gaps = np.diff(times)
    assert gaps.min() >= 0.5 - 1e-9
    assert len(times) < 25  # the refractory really did suppress some edges


def test_a_slow_build_is_not_a_kick():
    detector = KickDetector()
    run(detector, np.linspace(0.0, 1.0, 100))  # +0.01 per block = 0.1 /s
    assert detector.count == 0


def test_steady_and_falling_bass_never_fire():
    detector = KickDetector()
    run(detector, np.array([0.8] * 20 + [0.1] * 20))
    assert detector.count == 0


def test_the_first_block_only_primes():
    detector = KickDetector()
    assert detector.update(1.0) == 0
    assert detector.update(0.0) == 0
    assert detector.update(1.0) == 1


def test_timestamps_and_block_durations_agree():
    bass = impulse_train(140.0, 6.0)
    by_dt = run(KickDetector(), bass)
    stamped = KickDetector()
    by_t = [
        stamped.t
        for i, value in enumerate(bass)
        if stamped.update(float(value), t=(i + 1) * BLOCK_SECONDS)
    ]
    assert by_t == pytest.approx(by_dt)


def test_reset_clears_the_detector():
    detector = KickDetector()
    run(detector, impulse_train(140.0, 4.0))
    assert detector.count > 0
    detector.reset()
    assert (detector.count, detector.t, detector.last_kick_t) == (0, 0.0, None)


def test_kick_detector_rejects_negative_refractory():
    with pytest.raises(ValueError):
        KickDetector(refractory_s=-0.1)


# -- find_blackhole ---------------------------------------------------------


def device(name: str, index: int, inputs: int, outputs: int) -> dict:
    return {
        "name": name,
        "index": index,
        "hostapi": 0,
        "max_input_channels": inputs,
        "max_output_channels": outputs,
        "default_samplerate": 48000.0,
    }


# What sounddevice.query_devices() returns on this machine (2026-09-08).
THIS_MACHINE = [
    device("Q27G3XMN", 0, 0, 2),
    device("BlackHole 2ch", 1, 2, 2),
    device("MacBook Pro的麥克風", 2, 1, 0),
    device("MacBook Pro的揚聲器", 3, 0, 2),
]


def test_finds_blackhole_in_the_real_device_list():
    assert find_blackhole(THIS_MACHINE) == 1


def test_returns_none_without_a_blackhole():
    assert find_blackhole([row for row in THIS_MACHINE if "BlackHole" not in row["name"]]) is None
    assert find_blackhole([]) is None


def test_output_only_blackhole_does_not_count():
    """A loopback we cannot record from is useless to the sidecar."""
    assert find_blackhole([device("BlackHole 16ch", 0, 0, 16)]) is None


def test_prefers_the_2ch_device_over_other_blackholes():
    devices = [
        device("BlackHole 16ch", 0, 16, 16),
        device("BlackHole 2ch", 1, 2, 2),
    ]
    assert find_blackhole(devices) == 1


def test_falls_back_to_any_input_capable_blackhole():
    assert find_blackhole([device("BlackHole 64ch", 0, 64, 64)]) == 0


def test_trusts_the_index_field_over_list_position():
    devices = [device("BlackHole 2ch", 9, 2, 2)]
    assert find_blackhole(devices) == 9


def test_index_falls_back_to_position_when_absent():
    devices = [
        {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "BlackHole 2ch", "max_input_channels": 2, "max_output_channels": 2},
    ]
    assert find_blackhole(devices) == 1


def test_match_is_case_insensitive_and_overridable():
    devices = [device("blackhole 2CH", 0, 2, 2), device("Speakers+BlackHole", 1, 2, 2)]
    assert find_blackhole(devices) == 0
    assert find_blackhole(devices, match="speakers+") == 1
