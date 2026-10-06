"""CPU tests plus opt-in real EGL/FFmpeg integration; no audio hardware/API."""
import json
import math
import os
from pathlib import Path
import shutil
import sys
import wave

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.linux_visualizer import (AudioAnalysis, ControlTransport, SyntheticPsytrance,
    WavSource, composition_layers, exact_counts, parser, run, write_audio)
from amv.linux_controls import LinuxControlState


def decision(**updates):
    d = dict(scene="tunnel", palette="acid_lime", feedback=.4, symmetry=8,
             camera_speed=.5, particle_mode="orbit", projectm_mix=.1,
             transition={"mode": "glide", "beats": 4},
             on_drop={"scene": "kaleido_mesh", "palette": "infrared", "particle_mode": "burst"},
             intent="Linux renderer test")
    d.update(updates)
    return d


@pytest.mark.parametrize("seconds,fps,rate,frames,samples", [
    (120, 30, 48000, 3600, 5760000), (1.01, 30, 48000, 30, 48000),
    (.01, 30, 44100, 1, 1470), (1, 29, 44100, 29, 44100)])
def test_exact_counts(seconds, fps, rate, frames, samples):
    assert exact_counts(seconds, fps, rate) == (frames, samples)


@pytest.mark.parametrize("seconds,fps,rate", [(0, 30, 48000), (-1, 30, 48000),
    (math.nan, 30, 48000), (math.inf, 30, 48000), (1, 0, 48000), (1, 30, 0)])
def test_bad_counts(seconds, fps, rate):
    with pytest.raises(ValueError):
        exact_counts(seconds, fps, rate)


def test_synthetic_pcm_is_finite_stereo_and_chunk_independent():
    source = SyntheticPsytrance()
    whole = source.read(12000, 12000)
    pieces = np.vstack([source.read(12000, 4317), source.read(16317, 7683)])
    np.testing.assert_array_equal(whole, pieces)
    assert whole.shape == (12000, 2)
    assert np.isfinite(whole).all()
    assert 0.05 < np.abs(whole).max() < 1
    assert np.mean(np.square(source.read(42 * 48000, 4800))) < np.mean(np.square(source.read(2 * 48000, 4800))) / 50


def test_wav_write_read_exact_sample_count(tmp_path):
    path = tmp_path / "source.wav"
    source = SyntheticPsytrance()
    report = write_audio(source, path, 48521)
    assert report["samples_per_channel"] == 48521
    with wave.open(str(path)) as file:
        assert file.getnframes() == 48521
    other = WavSource(path)
    try:
        expected = source.read(210, 700)
        np.testing.assert_allclose(other.read(210, 700), expected, atol=4e-5)
        tail = other.read(48520, 20)
        assert not np.any(tail[1:])
    finally:
        other.close()


@pytest.mark.parametrize("width,raw,expected", [
    (1, bytes([0, 128, 255]), [-1., 0., 127 / 128]),
    (2, np.array([-32768, 0, 32767], dtype="<i2").tobytes(), [-1., 0., 32767 / 32768]),
    (3, bytes([0, 0, 128, 0, 0, 0, 255, 255, 127]), [-1., 0., 8388607 / 8388608]),
    (4, np.array([-2147483648, 0, 2147483647], dtype="<i4").tobytes(), [-1., 0., 2147483647 / 2147483648])])
def test_pcm_bit_depths(tmp_path, width, raw, expected):
    path = tmp_path / "bits.wav"
    with wave.open(str(path), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(width)
        file.setframerate(44100)
        file.writeframes(raw)
    other = WavSource(path)
    try:
        np.testing.assert_allclose(other.read(0, 3)[:, 0], expected, atol=1e-7)
    finally:
        other.close()


def test_audio_features_are_extracted_and_kicks_detected():
    analysis = AudioAnalysis(SyntheticPsytrance())
    features = [analysis.step()[0] for _ in range(30)]
    assert analysis.samples == 144000
    assert analysis.blocks == 30
    assert analysis.kicks > 0
    for feature in features:
        assert all(math.isfinite(v) for v in feature.values())
        assert all(0 <= feature[k] <= 1 for k in ("bass", "mid", "high", "energy", "kick"))
    assert np.std([f["bass"] for f in features]) > .05


def test_control_transport_uses_actual_localhost_datagrams():
    state = LinuxControlState()
    transport = ControlTransport(state)
    try:
        transport.decision(decision(transition={"mode": "cut", "beats": 1}), 1.)
        assert transport.client.sent == transport.received == 11
        assert transport.rejected == 0
        assert state.update(1.).scene == "tunnel"
        transport.client.send("/manual/camera_speed", .9)
        transport.pump(2., 1)
        assert state.update(2.).camera_speed == pytest.approx(.9)
        assert "camera_speed" in state.update(2.).frozen_fields
    finally:
        transport.close()


def test_composition_joint_weights_preserve_all_marginals():
    from types import SimpleNamespace
    weights = dict(scene_weights=(("tunnel", .3), ("particle_field", .7)),
        palette_weights=(("acid_lime", .5), ("infrared", .5)),
        particle_mode_weights=(("orbit", .2), ("rain", .8)), symmetry_weights=((4, .1), (8, .9)))
    state = SimpleNamespace(**weights)
    layers = composition_layers(state)
    assert len(layers) <= 5
    assert sum(weight for _, weight in layers) == pytest.approx(1)
    for key, table in weights.items():
        key = key.removesuffix("_weights")
        for value, wanted in table:
            assert sum(w for layer, w in layers if layer[key] == value) == pytest.approx(wanted)


@pytest.mark.skipif(os.environ.get("AMV_TEST_EGL") != "1", reason="opt in to real EGL/FFmpeg with AMV_TEST_EGL=1")
def test_real_egl_video_export_and_cleanup(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is not installed")
    args = parser().parse_args(["--seconds", "1", "--width", "160", "--height", "90", "--output", str(tmp_path)])
    report = run(args)
    assert report["passed"]
    assert report["rendered_frames"] == 30
    assert report["audio_analysis_samples"] == 48000
    assert report["raw_shader_draws"] == report["raw_float_frames_validated"]
    assert report["black_frames"] == 0
    assert report["osc_send_errors"] == 0
    assert report["feature_packets_sent"] == report["feature_packets_received"]
    import subprocess
    result = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-of", "json", str(tmp_path / "visualizer.mp4")], capture_output=True, text=True, check=True)
    streams = json.loads(result.stdout)["streams"]
    assert int(next(s for s in streams if s["codec_type"] == "video")["nb_read_frames"]) == 30
    assert next(s for s in streams if s["codec_type"] == "audio")["sample_rate"] == "48000"


def test_timing_statistics_are_cumulative_and_bounded():
    from tools.linux_visualizer import TimingStats
    stats = TimingStats(.033)
    for _ in range(10000):
        stats.append(.01)
    stats.append(.6001)
    assert len(stats) == 10001
    assert len(stats.bins) == 5002
    assert stats.p95_upper_ms == 10
    assert stats.maximum == .6001
    assert stats.above_budget == 1
    assert stats.total == pytest.approx(100.6001)


def test_finished_loop_does_not_imply_accepted_realtime_target():
    from tools.linux_visualizer import acceptance_checks
    report = dict(mode="realtime-headless", director="showcase", duration_s=90,
        target_fps=30, expected_frames=2700, rendered_frames=2000,
        wall_elapsed_s=90.01, execution_completed=True, source_unchanged_during_run=True,
        errors=[], raw_shader_draws=2500, raw_float_frames_validated=2500,
        black_frames=0, feature_packets_sent=5400, feature_packets_received=5400,
        control_packets_sent=55, control_packets_received=55, internal_control_packets_received=55, control_packets_rejected=0,
        osc_send_errors=0, longest_exact_repeat_run=0, longest_exact_repeat_duration_s=0., resources_released=True,
        exact_audio_sample_count=True, all_five_scenes_rendered=True,
        palette_render_draws={name: 1 for name in ("violet_cyan", "acid_lime", "amber_dusk", "mono_white", "infrared")},
        frame_interval_max_ms=35, heartbeat_stale_frames=0)
    checks = acceptance_checks(report)
    assert not checks["at_least_98_percent_render_slots"]
    assert checks["full_requested_wall_duration"]
    report.update(rendered_frames=2690, frame_interval_max_ms=700)
    assert not acceptance_checks(report)["no_half_second_frame_gap"]
    report.update(frame_interval_max_ms=35, heartbeat_stale_frames=0)
    assert all(acceptance_checks(report).values())


def test_external_udp_accounting_and_section_authority():
    from amv.osc_io import TDClient
    from tools.linux_visualizer import update_render_controls
    controls = LinuxControlState()
    transport = ControlTransport(controls)
    external = TDClient(port=transport.port)
    try:
        external.send_director(decision(transition={"mode": "cut", "beats": 1}))
        transport.pump(0.)
        assert transport.internal_received == transport.client.sent == 0
        assert transport.external_received == transport.received == 11
        external.send_section("drop")
        transport.pump(1.)
        state = update_render_controls(controls, 1., True, "steady", "external")
        assert state.section == "drop"
        assert state.drop_count == 1
        assert state.scene == "kaleido_mesh"
        # Local modes explicitly choose the detector as authority instead.
        state = update_render_controls(controls, 2., False, "steady", "showcase")
        assert state.section == "steady"
        transport.decision(decision(transition={"mode": "cut", "beats": 1}), 3.)
        assert transport.internal_received == transport.client.sent == 11
        assert transport.external_received == 12
        assert transport.received == 23
    finally:
        external.close()
        transport.close()


@pytest.mark.parametrize("scene", ("fractal_temple", "tunnel", "particle_field", "kaleido_mesh", "projectm_blend"))
@pytest.mark.parametrize("particle_mode", ("spiral", "burst", "rain", "orbit", "none"))
def test_only_empty_particle_combination_changes_render_branch(scene, particle_mode):
    from tools.linux_visualizer import render_scene_for
    expected = "kaleido_mesh" if (scene, particle_mode) == ("particle_field", "none") else scene
    assert render_scene_for(scene, particle_mode) == expected


def _render_test_state(scene="particle_field", particle_mode="none", palette="violet_cyan", feedback=0.):
    from types import SimpleNamespace
    return SimpleNamespace(scene_weights=((scene, 1.),), palette_weights=((palette, 1.),),
        particle_mode_weights=((particle_mode, 1.),), symmetry_weights=((8, 1.),),
        camera_speed=0., feedback=feedback, projectm_mix=0.)


def test_fallback_preserves_none_and_controls_and_counts_effective_draws():
    from tools.linux_visualizer import VisualComposer, SCENES, PARTICLE_MODES, EMPTY_PARTICLE_FALLBACK_REASON
    class RecordingRenderer:
        def __init__(self):
            self.calls = []
        def render(self, scene, **kwargs):
            self.calls.append((scene, kwargs))
            frame = np.full((4, 8, 4), .2, np.float32)
            frame[..., 3] = 1
            return frame
    renderer = RecordingRenderer()
    composer = VisualComposer(renderer)
    state = _render_test_state()
    before = dict(vars(state))
    features = dict(bass=0., mid=0., high=0., energy=0.)
    composer.render(state, features, 1 / 30, 0.)
    assert vars(state) == before
    assert renderer.calls[0][0] == SCENES.index("kaleido_mesh")
    assert renderer.calls[0][1]["particle_mode"] == PARTICLE_MODES.index("none")
    assert composer.scene_frames == {"kaleido_mesh": 1}
    assert composer.requested_scene_frames == {"particle_field": 1}
    assert composer.empty_particle_fallback_draws == 1
    assert composer.empty_particle_fallback_output_frames == 1
    assert composer.last_fallback_applied
    assert composer.last_effective_scene_weights == (("kaleido_mesh", 1.),)
    assert composer.last_render_layers == [{"requested_scene": "particle_field",
        "effective_scene": "kaleido_mesh", "particle_mode": "none", "palette": "violet_cyan",
        "symmetry": 8, "weight": 1., "fallback_reason": EMPTY_PARTICLE_FALLBACK_REASON}]
    composer.render(_render_test_state(particle_mode="rain"), features, 1 / 30, 0.)
    assert not composer.last_fallback_applied
    assert composer.empty_particle_fallback_output_frames == 1
    assert composer.scene_frames == {"kaleido_mesh": 1, "particle_field": 1}
    assert composer.last_render_layers[0]["fallback_reason"] is None


def test_showcase_alternates_off_particles_without_faking_five_scene_coverage():
    from tools.linux_visualizer import showcase_particle_mode, render_scene_for, SCENES
    assert showcase_particle_mode(2) == "rain"
    assert showcase_particle_mode(7) == "none"
    assert showcase_particle_mode(12) == "rain"
    assert showcase_particle_mode(17) == "none"
    first_cycle = {render_scene_for(SCENES[i], showcase_particle_mode(i)) for i in range(5)}
    second_cycle = {render_scene_for(SCENES[i], showcase_particle_mode(i + 5)) for i in range(5)}
    assert first_cycle == set(SCENES)
    assert "particle_field" not in second_cycle
    assert "kaleido_mesh" in second_cycle


@pytest.mark.skipif(os.environ.get("AMV_TEST_EGL") != "1", reason="opt in to actual EGL")
@pytest.mark.parametrize("palette", ("violet_cyan", "acid_lime", "amber_dusk", "mono_white", "infrared"))
@pytest.mark.parametrize("audio_level", (0., .6), ids=("silence", "signal"))
def test_actual_egl_particles_off_stays_animated_across_palettes(palette, audio_level):
    import hashlib
    from tools.linux_visualizer import VisualComposer, ShaderRenderer, PALETTES, PARTICLE_MODES, SCENES
    state = _render_test_state(palette=palette, feedback=.98)
    features = {key: audio_level for key in ("bass", "mid", "high", "energy")}
    with ShaderRenderer(160, 90) as renderer:
        # Reproduce the exact original-shader corner, with original bytes intact.
        first = renderer.render(SCENES.index("particle_field"), time=0.,
            particle_mode=PARTICLE_MODES.index("none"), palette=PALETTES.index(palette), audio=(0.,) * 4)
        later = renderer.render(SCENES.index("particle_field"), time=2.,
            particle_mode=PARTICLE_MODES.index("none"), palette=PALETTES.index(palette), audio=(0.,) * 4)
        np.testing.assert_array_equal(first, later)
        composer = VisualComposer(renderer)
        hashes = []
        for _ in range(60):
            frame = composer.render(state, features, 1 / 30, 0.)
            assert np.isfinite(frame).all()
            assert frame[..., :3].std() > .001
            pixels = np.rint(np.clip(frame[..., :3], 0, 1) * 255).astype(np.uint8)
            hashes.append(hashlib.sha256(pixels.tobytes()).hexdigest())
        assert len(set(hashes)) == 60
        assert all(a != b for a, b in zip(hashes, hashes[1:]))
        assert composer.scene_frames == {"kaleido_mesh": 60}
        assert composer.requested_scene_frames == {"particle_field": 60}
        assert composer.empty_particle_fallback_draws == 60
        assert composer.empty_particle_fallback_output_frames == 60
        assert composer.validation_count == 60
