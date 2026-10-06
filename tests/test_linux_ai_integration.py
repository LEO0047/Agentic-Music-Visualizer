"""Lightweight seam evidence tests; actual EGL and 220 s probe require opt-in."""
from dataclasses import asdict
import copy
import io
import json
import os
from pathlib import Path
import sys
import threading
import time

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import linux_ai_integration as seam
from amv.director import RuleDirector, History
from amv.linux_controls import LinuxControlState
from amv.schema import validate_and_clamp


def sample_decision():
    return validate_and_clamp(dict(scene="particle_field", palette="infrared", feedback=.18,
        symmetry=9, camera_speed=.7, particle_mode="none", projectm_mix=0.,
        transition={"mode": "cut", "beats": 1},
        on_drop={"scene": "tunnel", "palette": "acid_lime", "particle_mode": "burst"}, intent="probe"))


def sample_frame(value=.3):
    frame = np.ones((8, 16, 4), dtype=np.float32)
    frame[..., :3] = value
    return frame


def test_production_timing_has_no_scaled_wall_clock_or_hour_claim():
    profile = seam.PROFILES["production"]
    assert profile.period_s == 18
    assert profile.ok_delay_s == 13
    assert profile.subprocess_timeout_s == 30
    assert profile.watchdog_s == 25
    assert profile.phase_scale == 1
    assert seam.PHASES[-1].end_s == 220
    assert seam.phase_at(0, profile).name == "gpt_success"
    assert seam.phase_at(32, profile).fake_mode == "fail"
    assert seam.phase_at(54, profile).fake_mode == "garbage"
    assert seam.phase_at(76, profile).fake_mode == "hang"
    assert seam.phase_at(118, profile).name == "gpt_same_mode_recovery"
    assert seam.phase_at(174, profile).director_mode == "manual"
    assert seam.phase_at(194, profile).director_mode == "rule"
    assert seam.phase_at(9000, profile).director_mode == "rule"
    assert all(phase.director_mode == "gpt" for phase in seam.PHASES[:5])


def test_steady_pcm_is_real_finite_chunk_independent_and_production_extracted():
    source = seam.SteadyPCM()
    block = source.read(0, 11000)
    chunks = np.vstack((source.read(0, 4700), source.read(4700, 6300)))
    np.testing.assert_array_equal(block, chunks)
    assert block.shape == (11000, 2)
    assert np.isfinite(block).all()
    assert 0 < source.peak < 1
    analysis = seam.AudioAnalysis(source)
    extracted = [analysis.step() for _ in range(40)]
    assert analysis.samples == 40 * 4800
    assert analysis.blocks == 40
    assert analysis.kicks > 0
    assert all(section == "steady" for _, section in extracted)
    assert np.std([features["bass"] for features, _ in extracted]) > .15
    assert all(features["energy"] > .35 for features, _ in extracted)
    assert all(100 < features["centroid"] < 10000 for features, _ in extracted)


def test_subprocess_is_hard_wired_to_fake_even_with_hostile_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AMV_CODEX_BIN", "/never/run/a/real/codex")
    monkeypatch.setenv("AMV_FAKE_CODEX_MODE", "hang")
    profile = seam.Profile(ok_delay_s=.02, subprocess_timeout_s=1)
    client = seam.FakeSubprocessClient(tmp_path, profile, lambda: (0, seam.PHASES[0]), time.monotonic())
    result = client.decide("only generated fixture data")
    assert validate_and_clamp(result) == result
    assert client.calls[0]["outcome"] == "ok"
    argv = client.build_argv("fixture", tmp_path / "out.json")
    assert argv[0] == sys.executable
    assert str(ROOT / "tools/fake_codex.py") in argv
    assert not any("/never/run" in item for item in argv)
    assert os.environ["AMV_FAKE_CODEX_MODE"] == "hang"


def test_actual_udp_batches_are_received_and_applied_on_owner_thread():
    controls = LinuxControlState()
    transport = seam.WireControls(controls)
    try:
        transport.decision(sample_decision(), 1.)
        state = controls.update(1.)
        assert transport.received == 11
        assert transport.rejected == 0
        assert controls.applied_batches == 1
        assert seam.matches_decision(seam.control_values(state), sample_decision())
        assert len(transport.wire_batches) == 1
        wire = transport.wire_batches[0]
        assert wire["elapsed_s"] == 1
        assert wire["render_thread"] == threading.get_ident()
        assert wire["fields"]["scene"] == "particle_field"
        assert json.loads(wire["fields"]["on_drop"])["scene"] == "tunnel"
    finally:
        transport.close()
    assert transport.server.socket.fileno() == -1


def test_control_observer_refuses_cross_thread_callbacks():
    transport = seam.WireControls(LinuxControlState())
    errors = []
    def other_thread():
        try:
            transport._receive(("127.0.0.1", 1), "/feat/section", "steady")
        except RuntimeError as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=other_thread)
    try:
        thread.start()
        thread.join()
        assert errors == ["Linux controls must only run on the GL owner thread"]
        assert transport.received == 0
    finally:
        transport.close()


def test_rule_takeover_cannot_publish_before_render_mode_switch():
    controls = LinuxControlState()
    controls.set_manual_mode("manual", 0)
    transport = seam.WireControls(controls)
    td = seam.ObservedTDClient("127.0.0.1", transport.port, lambda: loop.mode, time.monotonic())
    loop = seam.DirectorLoop(td, RuleDirector(), mode="manual", worker=False,
                            clock=lambda: 1., out=io.StringIO())
    original_pump = transport.pump
    attempted = threading.Event()
    completed = threading.Event()
    worker = None
    def tick():
        attempted.set()
        loop.on_tick({}, "steady")
        completed.set()
    def pump_with_concurrent_tick(now, expected=0):
        nonlocal worker
        if worker is None:
            worker = threading.Thread(target=tick)
            worker.start()
            assert attempted.wait(1)
            assert not completed.wait(.02)
        return original_pump(now, expected)
    transport.pump = pump_with_concurrent_tick
    try:
        assert seam.apply_operator_mode(loop, transport, "rule", 1.) == 1.
        worker.join(1)
        assert completed.is_set()
        original_pump(1.)
        assert controls.mode == "rule"
        assert controls.applied_batches == 1
        assert transport.rejected == 0
        assert len(loop.decisions) == 1
    finally:
        loop.close()
        td.close()
        transport.close()


def test_frame_evidence_links_heartbeat_motion_endpoint_and_frozen_manual():
    controls = LinuxControlState()
    controls.set_manual_mode("manual", 0)
    for key, value in seam.MANUAL_VALUES.items():
        controls.set_manual(key, value, 0)
    evidence = seam.FrameEvidence()
    state = controls.update(0)
    evidence.record(sample_frame(.2), state, 1., seam.PHASES[5], None, ["hang"])
    evidence.record(sample_frame(.3), state, 1.03, seam.PHASES[5], None, ["hang"])
    assert evidence.frames == 2
    assert evidence.black == 0
    assert evidence.groups["actual_hang_inflight"]["changed_frames"] == 1
    assert evidence.groups["actual_manual_mode"]["first_sha256"] != evidence.groups["actual_manual_mode"]["last_sha256"]
    assert len(evidence.manual_signatures) == 1
    assert evidence.manual_values_match
    controls.set_manual_mode("gpt", 1.03)
    state = controls.update(1.03)
    publication = dict(heartbeat=3, source="gpt", decision=sample_decision())
    evidence.record(sample_frame(.4), state, 1.06, seam.PHASES[4], publication, [])
    assert evidence.first_by_heartbeat[3]["frame_index"] == 2
    assert evidence.endpoint_by_heartbeat[3]["source"] == "gpt"


def test_nonfinite_pixels_are_rejected_before_hashing_and_freeze_is_measured():
    evidence = seam.FrameEvidence()
    state = LinuxControlState().update(0)
    bad = sample_frame()
    bad[0, 0, 0] = np.nan
    with pytest.raises(ValueError, match="non-finite"):
        evidence.record(bad, state, 0, seam.PHASES[0], None, [])
    assert evidence.frames == 0
    for t in (0., .5, 1.1):
        evidence.record(sample_frame(), state, t, seam.PHASES[0], None, [])
    assert evidence.longest_repeat_s == pytest.approx(1.1)
    assert evidence.max_gap_s == pytest.approx(.6)


def test_endpoint_requires_all_controls_not_only_enum_label():
    controls = LinuxControlState()
    for key, value in seam.MANUAL_VALUES.items():
        controls.set_manual(key, value, 0)
    values = seam.control_values(controls.update(0))
    assert seam.matches_decision(values, sample_decision())
    values["scene_weights"] = (("tunnel", .01), ("particle_field", .99))
    assert not seam.matches_decision(values, sample_decision())
    values["scene_weights"] = (("particle_field", 1.),)
    values["feedback"] = .2
    assert not seam.matches_decision(values, sample_decision())


def test_effective_scene_trace_declares_particles_off_safety_mapping():
    controls = LinuxControlState()
    for key, value in seam.MANUAL_VALUES.items():
        controls.set_manual(key, value, 0)
    state = controls.update(0)
    layers = [dict(requested_scene="particle_field", effective_scene="kaleido_mesh",
                   particle_mode="none", palette="infrared", symmetry=9,
                   weight=1., fallback_reason=seam.EMPTY_PARTICLE_REASON)]
    assert seam.valid_render_layers(layers, state)
    evidence = seam.FrameEvidence()
    row = evidence.record(sample_frame(), state, 0, seam.PHASES[5], None, [], layers)
    assert row["state"]["scene_to"] == "particle_field"
    assert row["effective_render_layers"][0]["effective_scene"] == "kaleido_mesh"
    assert row["safety_fallback_applied"]
    assert evidence.fallback_frames == evidence.fallback_draws == 1
    assert evidence.render_layers_validated == 1
    layers[0]["effective_scene"] = "particle_field"
    assert not seam.valid_render_layers(layers, state)
    layers[0]["effective_scene"] = "kaleido_mesh"
    layers[0]["particle_mode"] = "spiral"
    assert not seam.valid_render_layers(layers, state)


def complete_report():
    decision = sample_decision()
    def provenance(t, source, heartbeat, endpoint=None):
        return dict(heartbeat=heartbeat, source=source, published_elapsed_s=t,
            wire_received_elapsed_s=t + .01, first_frame={"elapsed_s": t + .03},
            first_changed_frame={"elapsed_s": t + .03},
            endpoint_frame={"elapsed_s": endpoint if endpoint is not None else t + 1},
            publication_to_first_frame_s=.03, since_previous_publication_s=1,
            decision_latency_s=13 if source == "gpt" else 0, decision=decision)
    decisions = [provenance(13, "gpt", 1), provenance(36, "rule (CodexError)", 2),
        provenance(54, "rule (CodexError)", 3),
        provenance(97, "rule (AI freshness watchdog)", 4, 104),
        provenance(133, "gpt", 5), provenance(194.1, "rule", 7, 218)]
    decisions[3]["since_previous_publication_s"] = 25.07
    decisions[1]["error"] = "exited 1"
    decisions[2]["error"] = "not valid JSON"
    return dict(profile=asdict(seam.PROFILES["production"]), target_fps=30, duration_s=220,
        execution_completed=True, active_wall_seconds=220, full_hour=False,
        source_unchanged_during_run=True, errors=[], renderer={"gl_renderer": "test fixture"},
        real_model_calls=0, audio_device_playback=False,
        raw_draws=7000, raw_validated=7000, black_frames=0, longest_exact_repeat_s=.03,
        render_layers_validated=6600, effective_scene_draws={"kaleido_mesh": 7000},
        renderer_effective_scene_draws={"kaleido_mesh": 7000},
        empty_particle_fallback_frames=500, empty_particle_fallback_draws=500,
        max_frame_gap_s=.05, rendered_frames=6600, feature_sent=13200, feature_received=13200,
        control_sent=100, control_received=100, control_batches_applied=6, control_rejected=0,
        osc_send_errors=0, decision_provenance=decisions,
        decisions=[dict(source="rule (CodexError)", error="exited 1"),
                   dict(source="rule (CodexError)", error="not valid JSON")],
        phase_events=[dict(phase=p.name, elapsed_s=p.start_s, set_mode_returned_s=p.start_s,
                           director_mode=p.director_mode, inflight_fake_subprocesses=1)
                      for p in seam.PHASES],
        calls=[dict(phase="gpt_success", fake_mode="ok", start_elapsed_s=0, end_elapsed_s=13.1, outcome="ok"),
               dict(phase="process_failure", fake_mode="fail", start_elapsed_s=35.9, end_elapsed_s=36, outcome="expected_injected_error"),
               dict(phase="malformed_output", fake_mode="garbage", start_elapsed_s=53.9, end_elapsed_s=54, outcome="expected_injected_error"),
               dict(phase="hung_subprocess", fake_mode="hang", start_elapsed_s=90, end_elapsed_s=120.05,
                    outcome="expected_injected_error", error_type="CodexError", error="codex exec timed out after 30s"),
               dict(phase="gpt_same_mode_recovery", fake_mode="ok", start_elapsed_s=173, end_elapsed_s=186.1, outcome="ok")],
        publications=[dict(mode="gpt", elapsed_s=13), dict(mode="rule", elapsed_s=194.1)],
        manual_control_signatures=1, manual_values_match=True,
        resources_released=True, peak_fake_subprocesses=1,
        frame_groups={group: dict(frames=500, changed_frames=499, motion_total_rgb24=10,
                                 first_sha256="a", last_sha256="b")
                      for group in ("actual_hang_inflight", "actual_manual_mode", "empty_particle_safety_fallback")})


def test_acceptance_requires_full_seam_evidence_and_discloses_tick_quantization():
    report = complete_report()
    checks = seam.evaluate(report)
    assert all(checks.values()), [key for key, value in checks.items() if not value]
    report["decision_provenance"][3]["since_previous_publication_s"] = 25.9
    checks = seam.evaluate(report)
    assert not checks["watchdog_bounded_with_declared_tick_quantization"]
    assert checks["watchdog_first_render_under_30_second_outage_budget"]


@pytest.mark.parametrize("change,failed", [
    (lambda r: r.update(full_hour=True), "never_claims_full_hour"),
    (lambda r: r.update(feature_received=13199), "feature_udp_complete"),
    (lambda r: r.update(control_received=99), "control_udp_complete"),
    (lambda r: r.update(render_layers_validated=6599), "every_frame_has_valid_requested_vs_effective_scene_trace"),
    (lambda r: r.update(renderer_effective_scene_draws={"particle_field": 7000}), "actual_scene_draw_totals_match_renderer"),
    (lambda r: r.update(manual_control_signatures=2), "manual_values_remain_fixed"),
    (lambda r: r["publications"].append(dict(mode="manual", elapsed_s=180)), "no_late_manual_publications"),
    (lambda r: r["decision_provenance"][3].update(endpoint_frame={"elapsed_s": 121}), "watchdog_rule_changes_rendered_state_while_hung"),
    (lambda r: r["decision_provenance"][4].update(first_changed_frame=None), "same_mode_gpt_recovery_reaches_pixels"),
    (lambda r: r["decision_provenance"][1].update(endpoint_frame=None), "exit_fault_reaches_rule_pixels"),
    (lambda r: r["decision_provenance"][2].update(first_changed_frame=None), "malformed_fault_reaches_rule_pixels"),
    (lambda r: r["calls"][-1].update(end_elapsed_s=200), "late_healthy_reply_completes_during_manual"),
    (lambda r: r["calls"][3].update(end_elapsed_s=130.05), "hang_times_out_at_configured_deadline"),
    (lambda r: r["calls"][3].update(error="unexpected unrelated error"), "hang_times_out_at_configured_deadline"),
    (lambda r: r["phase_events"][4].update(director_mode="rule"), "same_mode_gpt_recovery_reaches_pixels"),
    (lambda r: r["frame_groups"]["actual_hang_inflight"].update(changed_frames=0), "actual_hang_inflight_visuals_keep_moving"),
])
def test_missing_evidence_cannot_silently_pass(change, failed):
    report = complete_report()
    change(report)
    assert not seam.evaluate(report)[failed]


def test_fault_timing_starts_at_actual_call_not_scheduled_phase():
    report = complete_report()
    rows = seam.summarize_faults(report["calls"], report["decision_provenance"])
    hang = next(row for row in rows if row["fake_mode"] == "hang")
    assert hang["call_start_elapsed_s"] == 90
    assert hang["call_to_rule_publication_s"] == 7
    assert hang["call_to_rule_wire_s"] == pytest.approx(7.01)
    assert hang["call_to_rule_first_frame_s"] == pytest.approx(7.03)
    assert hang["call_to_rule_endpoint_frame_s"] == 14
    assert hang["fresh_age_at_fallback_s"] == 25.07


def test_cli_defaults_are_explicit_actual_render_production_probe():
    args = seam.parser().parse_args([])
    assert (args.width, args.height, args.fps) == (320, 180, 30)
    assert args.profile == "production"


def test_previous_evidence_is_never_overwritten(tmp_path):
    (tmp_path / "report.json").write_text("previous evidence")
    with pytest.raises(FileExistsError):
        seam.run(tmp_path)
    assert (tmp_path / "report.json").read_text() == "previous evidence"


@pytest.mark.parametrize("options", [dict(width=0), dict(height=-1), dict(fps=0)])
def test_invalid_render_dimensions_fail_before_any_gl(tmp_path, options):
    with pytest.raises(ValueError):
        seam.run(tmp_path, **options)


def test_failed_egl_initialization_releases_udp_and_preserves_report(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("EGL deliberately unavailable in CPU-only test")
    monkeypatch.setattr(seam, "ShaderRenderer", unavailable)
    report = seam.run(tmp_path)
    assert not report["passed"]
    assert report["resources_released"]
    assert not report["execution_completed"]
    assert report["full_hour"] is False
    assert any("EGL deliberately unavailable" in error for error in report["errors"])
    assert json.loads((tmp_path / "report.json").read_text())["status"] == "failed"


def test_failed_evidence_write_cannot_prevent_resource_cleanup(tmp_path, monkeypatch):
    original = seam.atomic_json
    def unavailable(*args, **kwargs):
        raise RuntimeError("no actual EGL in unit test")
    def broken_wire_evidence(path, value):
        if path.name == "wire-controls.json":
            raise OSError("simulated evidence write failure")
        return original(path, value)
    monkeypatch.setattr(seam, "ShaderRenderer", unavailable)
    monkeypatch.setattr(seam, "atomic_json", broken_wire_evidence)
    report = seam.run(tmp_path)
    assert report["resources_released"]
    assert not report["passed"]
    assert any("simulated evidence write failure" in error for error in report["errors"])


@pytest.mark.skipif(os.environ.get("AMV_TEST_AI_EGL") != "1", reason="opt in to 220 real seconds and actual EGL using AMV_TEST_AI_EGL=1")
def test_actual_ai_sidecar_to_gl_pixels_full_bounded_probe(tmp_path):
    report = seam.run(tmp_path)
    assert report["passed"], report["checks"]
    assert report["production_timing"]
    assert report["active_wall_seconds"] >= 220
    assert report["full_hour"] is False
    assert report["real_model_calls"] == 0
    assert report["snapshots"]
    assert all((tmp_path / row["file"]).is_file() for row in report["snapshots"])
