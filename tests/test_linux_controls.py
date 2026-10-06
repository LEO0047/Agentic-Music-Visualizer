"""Deterministic Linux control contract tests; these do not claim TD/GPU coverage."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import math
import random

import pytest

from amv.linux_controls import LinuxControlState, DISCRETE_FIELDS, CONTINUOUS_FIELDS
from amv.osc_io import DIRECTOR_ADDRESSES, TDClient
from amv.schema import PALETTES, PARTICLE_MODES, SCENES, validate_and_clamp


def decision(**changes):
    return validate_and_clamp({
        "scene": "fractal_temple", "palette": "acid_lime", "feedback": 0.8,
        "symmetry": 12, "camera_speed": 0.9, "particle_mode": "burst",
        "projectm_mix": 0.6, "transition": {"mode": "cut", "beats": 2},
        "on_drop": {"scene": "particle_field", "palette": "infrared", "particle_mode": "rain"},
        "intent": "Linux test", **changes,
    })


def messages(d=None, heartbeat=1):
    """Exercise the real outgoing formatter; never send network packets here."""
    captured = []
    with TDClient(port=0) as client:
        client.send = lambda address, value: captured.append((address, value)) or True
        client.send_director(d or decision(), heartbeat=heartbeat)
    assert tuple(address for address, _ in captured) == DIRECTOR_ADDRESSES
    return captured


def send(state, d=None, now=0, heartbeat=1):
    results = [state.receive(address, value, now) for address, value in messages(d, heartbeat)]
    assert all(results), state.last_error


def weights(frame, field):
    return dict(getattr(frame, f"{field}_weights"))


def test_defaults_render_without_any_director_and_snapshot_is_immutable():
    state = LinuxControlState()
    frame = state.update(100)
    assert frame.scene == "tunnel"
    assert frame.palette == "violet_cyan"
    assert frame.symmetry == 6
    assert frame.feedback == 0
    assert frame.camera_speed == .25
    assert frame.mode == "rule"
    assert frame.heartbeat is None
    assert frame.heartbeat_age == 0
    assert not frame.heartbeat_stale
    with pytest.raises(FrozenInstanceError):
        frame.feedback = .9
    later = state.update(146)
    assert later.heartbeat_stale
    assert later.scene_weights == frame.scene_weights


def test_heartbeat_is_atomic_commit_and_cut_applies_every_target():
    state = LinuxControlState()
    batch = messages()
    for address, value in batch[:-1]:
        assert state.receive(address, value, 0)
        assert state.update(0).scene == "tunnel"
        assert state.update(0).feedback == 0
    assert state.receive(*batch[-1], 0)
    frame = state.update(0)
    for field in DISCRETE_FIELDS + CONTINUOUS_FIELDS:
        assert getattr(frame, field) == decision()[field]
    assert frame.drop_armed
    assert state.accepted_batches == state.applied_batches == 1


@pytest.mark.parametrize("missing", range(10))
def test_each_missing_datagram_cannot_partially_apply(missing):
    state = LinuxControlState()
    batch = messages()
    batch.pop(missing)
    for address, value in batch:
        state.receive(address, value, 0)
    frame = state.update(0)
    assert frame.scene == "tunnel"
    assert frame.palette == "violet_cyan"
    assert frame.feedback == 0
    assert state.applied_batches == 0
    # A new complete transaction resynchronizes after any missing field.
    send(state, now=1, heartbeat=2)
    assert state.update(1).scene == "fractal_temple"


@pytest.mark.parametrize("index", range(9))
def test_adjacent_reordering_is_rejected_and_recovers(index):
    state = LinuxControlState()
    batch = messages()
    batch[index], batch[index + 1] = batch[index + 1], batch[index]
    for address, value in batch:
        state.receive(address, value, 0)
    assert state.update(0).scene == "tunnel"
    assert state.applied_batches == 0
    send(state, now=1)
    assert state.update(1).scene == "fractal_temple"


def test_new_transition_start_discards_incomplete_old_batch():
    state = LinuxControlState()
    for address, value in messages()[:5]:
        assert state.receive(address, value, 0)
    send(state, decision(scene="kaleido_mesh"), now=1)
    assert state.update(1).scene == "kaleido_mesh"
    assert state.rejected_batches == 1


@pytest.mark.parametrize("field,value", [
    ("scene", "not_a_scene"), ("palette", None), ("symmetry", True),
    ("feedback", float("nan")), ("camera_speed", float("inf")),
    ("projectm_mix", []), ("particle_mode", "launch"),
    ("transition_mode", "linear"), ("transition_beats", "not a number"),
    ("on_drop", "{"), ("on_drop", "[]"), ("on_drop", "null"),
    ("on_drop", '{"scene": "tunnel"}'), ("on_drop", " " * 4097),
    ("on_drop", '{"scene":"tunnel","palette":"acid_lime","particle_mode":"evil"}'),
])
def test_invalid_batch_never_changes_visible_state(field, value):
    state = LinuxControlState()
    batch = [(address, value if address == "/director/" + field else original)
             for address, original in messages()]
    results = [state.receive(address, val, 0) for address, val in batch]
    assert not all(results)
    assert state.update(0).scene == "tunnel"
    assert state.update(0).feedback == 0
    assert state.applied_batches == 0


def test_wire_numeric_repair_uses_schema_bounds_and_types():
    state = LinuxControlState()
    replacements = {"feedback": 12, "camera_speed": -3, "projectm_mix": "0.75", "symmetry": 300}
    for address, value in messages():
        assert state.receive(address, replacements.get(address.rsplit("/", 1)[-1], value), 0)
    frame = state.update(0)
    assert frame.feedback == .98
    assert frame.camera_speed == 0
    assert frame.projectm_mix == .75
    assert frame.symmetry == 16
    assert isinstance(frame.symmetry, int)


def test_heartbeat_liveness_is_independent_of_controls():
    state = LinuxControlState()
    state.set_manual_mode(True, 0)
    send(state, now=10, heartbeat=8)
    frame = state.update(12)
    assert frame.heartbeat == 8
    assert frame.heartbeat_age == 2
    assert frame.scene == "tunnel"
    assert state.accepted_batches == 1
    assert state.applied_batches == 0
    assert not state.update(55).heartbeat_stale
    assert state.update(55.001).heartbeat_stale
    assert state.receive("/director/heartbeat", 9, 56)
    assert not state.update(56).heartbeat_stale


@pytest.mark.parametrize("value", [None, "1", True, -1, 1.5, float("nan"), float("inf")])
def test_invalid_heartbeat_cannot_commit_or_update_watchdog(value):
    state = LinuxControlState()
    for address, original in messages()[:-1]:
        state.receive(address, original, 0)
    assert not state.receive("/director/heartbeat", value, 0)
    frame = state.update(1)
    assert frame.heartbeat is None
    assert frame.scene == "tunnel"


def test_continuous_glide_has_correct_beat_duration_and_exact_endpoints():
    state = LinuxControlState(bpm=120)
    send(state, decision(transition={"mode": "glide", "beats": 4}), now=10)
    start = state.update(10)
    middle = state.update(11)
    end = state.update(12)
    assert start.feedback == 0
    assert middle.feedback == pytest.approx(.4)
    assert middle.camera_speed == pytest.approx((.25 + .9) / 2)
    assert end.feedback == pytest.approx(.8)
    assert end.camera_speed == pytest.approx(.9)
    assert end.projectm_mix == pytest.approx(.6)


def test_glide_is_frame_rate_independent():
    fast, slow = LinuxControlState(120), LinuxControlState(120)
    d = decision(transition={"mode": "glide", "beats": 4})
    send(fast, d)
    send(slow, d)
    fast.update(0, kick=True)
    slow.update(0, kick=True)
    for tick in range(1, 61):
        fast.update(tick / 60)
    a, b = fast.update(1), slow.update(1)
    assert a.feedback == b.feedback
    assert a.scene_weights == b.scene_weights
    assert a.palette_weights == b.palette_weights


def test_discrete_glide_waits_for_kick_then_returns_renderable_endpoints():
    state = LinuxControlState(120)
    send(state, decision(transition={"mode": "glide", "beats": 2}))
    waiting = state.update(.2)
    assert waiting.scene == "tunnel"
    assert set(waiting.pending_fields) == set(DISCRETE_FIELDS)
    start = state.update(.5, kick=True)
    assert start.scene_from == "tunnel"
    assert start.scene_to == "fractal_temple"
    assert start.scene_mix == 0
    assert not start.pending_fields
    midpoint = state.update(1)
    assert weights(midpoint, "scene") == {"tunnel": .5, "fractal_temple": .5}
    assert weights(midpoint, "palette") == {"violet_cyan": .5, "acid_lime": .5}
    assert weights(midpoint, "symmetry") == {6: .5, 12: .5}
    assert weights(midpoint, "particle_mode") == {"spiral": .5, "burst": .5}
    end = state.update(1.5)
    assert end.scene_weights == (("fractal_temple", 1),)
    assert end.palette_weights == (("acid_lime", 1),)
    assert end.scene_mix == 1


def test_on_next_kick_cuts_discretes_but_smooths_continuous_values():
    state = LinuxControlState(120)
    send(state, decision(transition={"mode": "on_next_kick", "beats": 4}))
    before = state.update(.5)
    assert before.scene == "tunnel"
    assert 0 < before.feedback < .8
    after = state.update(.5, kick=True)
    assert after.scene == "fractal_temple"
    assert after.scene_weights == (("fractal_temple", 1),)
    assert after.feedback == before.feedback


@pytest.mark.parametrize("mode", ["glide", "on_next_kick"])
def test_kickless_timeout_cannot_strand_a_target(mode):
    state = LinuxControlState(120)
    send(state, decision(transition={"mode": mode, "beats": 2}))
    assert state.update(1.999).scene == "tunnel"
    boundary = state.update(2)
    assert not boundary.pending_fields
    if mode == "glide":
        assert boundary.scene_mix == 0
        assert state.update(2.5).scene_mix == pytest.approx(.5)
    else:
        assert boundary.scene == "fractal_temple"
    assert state.update(3).scene == "fractal_temple"


def test_held_kick_does_not_trigger_new_pending_target_until_next_edge():
    state = LinuxControlState()
    state.update(0, kick=True)
    send(state, decision(transition={"mode": "on_next_kick", "beats": 2}), now=.1)
    assert state.update(.2, kick=True).scene == "tunnel"
    state.update(.3, kick=False)
    assert state.update(.4, kick=True).scene == "fractal_temple"


def test_interrupted_glides_preserve_visible_weights_at_boundary():
    state = LinuxControlState(120)
    send(state, decision(transition={"mode": "glide", "beats": 2}))
    state.update(0, kick=True)
    before = state.update(.5)
    send(state, decision(scene="kaleido_mesh", palette="infrared", feedback=.2,
                         transition={"mode": "glide", "beats": 2}), now=.5, heartbeat=2)
    boundary = state.update(.5, kick=True)
    assert boundary.scene_weights == before.scene_weights
    assert boundary.palette_weights == before.palette_weights
    assert boundary.feedback == before.feedback
    midpoint = state.update(1)
    assert weights(midpoint, "scene") == {"tunnel": .25, "fractal_temple": .25, "kaleido_mesh": .5}
    assert weights(midpoint, "palette") == {"violet_cyan": .25, "acid_lime": .25, "infrared": .5}
    end = state.update(1.5)
    assert end.scene_weights == (("kaleido_mesh", 1),)
    assert end.feedback == pytest.approx(.2)


def test_manual_edit_immediate_freeze_expires_at_30_seconds():
    state = LinuxControlState()
    state.set_manual("scene", "kaleido_mesh", 0)
    state.set_manual("feedback", .3, 0)
    for at in (0, 1, 29.999):
        send(state, now=at)
        frame = state.update(at)
        assert frame.scene == "kaleido_mesh"
        assert frame.feedback == .3
        assert "scene" in frame.frozen_fields
        assert frame.palette == "acid_lime"  # Unedited controls remain responsive.
    send(state, now=30)
    frame = state.update(30)
    assert frame.scene == "fractal_temple"
    assert frame.feedback == .8
    assert not frame.frozen_fields


def test_manual_edit_cancels_queued_target_active_glide_and_drop_collision():
    state = LinuxControlState(120)
    send(state, decision(transition={"mode": "glide", "beats": 2}))
    state.update(0, kick=True)
    state.update(.5)
    state.set_manual("scene", "kaleido_mesh", .5)
    state.set_manual("feedback", .22, .5)
    send(state, decision(transition={"mode": "on_next_kick", "beats": 2}), now=.6)
    assert "scene" not in state.update(.6).pending_fields
    frame = state.update(.7, kick=True, section="drop")
    assert frame.scene == "kaleido_mesh"
    assert frame.feedback == .22
    assert frame.palette == "infrared"
    assert frame.drop_count == 1
    state.update(.8)
    assert state.update(31).scene == "kaleido_mesh"  # No replay when the timer expires.


def test_manual_touch_between_staging_and_heartbeat_wins():
    state = LinuxControlState()
    batch = messages()
    for address, value in batch[:-1]:
        state.receive(address, value, 0)
    state.set_manual("palette", "mono_white", 0)
    assert state.receive(*batch[-1], 0)
    frame = state.update(0)
    assert frame.palette == "mono_white"
    assert frame.scene == "fractal_temple"


def test_manual_mode_holds_exact_mid_glide_mixture_and_requires_fresh_decision():
    state = LinuxControlState(120)
    send(state, decision(transition={"mode": "glide", "beats": 2}))
    state.update(0, kick=True)
    before = state.update(.5)
    state.set_manual_mode(True, .5)
    send(state, now=10)
    held = state.update(10, kick=True, section="drop")
    assert held.scene_weights == before.scene_weights
    assert held.palette_weights == before.palette_weights
    assert held.feedback == before.feedback
    assert not held.drop_armed
    assert not held.pending_fields
    state.set_manual_mode(False, 31)
    assert state.update(31).scene_weights == before.scene_weights
    send(state, now=32)
    assert state.update(32).scene == "fractal_temple"


def test_leaving_manual_does_not_remove_recent_individual_freeze():
    state = LinuxControlState()
    state.set_manual_mode("gpt", 0)
    state.set_manual_mode(True, 0)
    state.set_manual("scene", "kaleido_mesh", 10)
    state.set_manual_mode(False, 11)
    send(state, now=11)
    frame = state.update(11)
    assert frame.mode == "gpt"
    assert frame.scene == "kaleido_mesh"


def test_drop_is_one_shot_and_wins_over_pending_scene_palette_particle():
    state = LinuxControlState(120)
    send(state, decision(transition={"mode": "glide", "beats": 2}))
    frame = state.update(.1, kick=True, section="drop")
    assert frame.scene == "particle_field"
    assert frame.palette == "infrared"
    assert frame.particle_mode == "rain"
    assert frame.drop_count == 1
    assert not frame.drop_armed
    assert not frame.pending_fields
    state.update(.2)
    again = state.update(.3, kick=True)
    assert again.scene == "particle_field"
    assert again.drop_count == 1
    assert state.update(3).scene == "particle_field"


def test_on_drop_requires_drop_section_and_a_kick():
    state = LinuxControlState()
    send(state)
    assert state.update(0, section="drop").scene == "fractal_temple"
    assert state.update(.1, kick=True, section="steady").scene == "fractal_temple"
    state.update(.2, section="drop")
    assert state.update(.3, kick=True).scene == "particle_field"


def test_sections_and_manual_controls_route_through_osc():
    state = LinuxControlState()
    assert state.receive("/manual/scene", "kaleido_mesh", 0)
    assert state.receive("/manual/symmetry", 100, 0)
    assert state.receive("/manual/mode", "manual", 0)
    assert state.receive("/feat/section", "drop", 0)
    frame = state.update(0)
    assert frame.scene == "kaleido_mesh"
    assert frame.symmetry == 16
    assert frame.section == "drop"
    assert frame.mode == "manual"
    assert not state.receive("/feat/section", "loud", 0)
    assert not state.receive("/manual/mode", "yes", 0)
    assert not state.receive("/manual/unknown", 3, 0)
    assert not state.receive("/director/scene/extra", "tunnel", 0)


def test_manual_transition_controls_survive_later_batch():
    state = LinuxControlState(120)
    state.set_manual("transition_mode", "glide", 0)
    state.set_manual("transition_beats", 4, 0)
    send(state)  # Incoming cut/2 cannot undo the manual glide/4.
    assert state.update(0).feedback == 0
    assert state.update(1).feedback == .4
    assert state.transition_mode == "glide"
    assert state.transition_beats == 4


@pytest.mark.parametrize("bpm", [0, -1, True, float("nan"), float("inf"), None, "no"])
def test_invalid_tempo_rejected(bpm):
    with pytest.raises(ValueError):
        LinuxControlState(bpm)


@pytest.mark.parametrize("now", [-1, True, float("nan"), float("inf"), None, "no"])
def test_invalid_time_rejected_without_poisoning_frame(now):
    state = LinuxControlState()
    with pytest.raises(ValueError):
        state.update(now)
    assert not state.receive("/director/heartbeat", 1, now)
    assert state.update(0).scene == "tunnel"


def test_time_reversal_cannot_rewind_controls():
    state = LinuxControlState()
    state.update(10)
    with pytest.raises(ValueError, match="backwards"):
        state.update(9)
    assert not state.receive("/director/heartbeat", 1, 9)
    assert state.update(10).heartbeat is None


def test_unknown_message_does_not_poison_valid_inflight_batch():
    state = LinuxControlState()
    for index, (address, value) in enumerate(messages()):
        assert state.receive(address, value, 0)
        if index == 4:
            assert not state.receive("/stray/message", [1, 2], 0)
    assert state.update(0).scene == "fractal_temple"


def test_randomized_transitions_stay_finite_bounded_and_enum_valid():
    rng = random.Random(77123)
    state = LinuxControlState(145)
    domains = {"scene": SCENES, "palette": PALETTES, "particle_mode": PARTICLE_MODES, "symmetry": tuple(range(1, 17))}
    now = 0.0
    for index in range(180):
        now += rng.random() * .2
        d = decision(scene=rng.choice(SCENES), palette=rng.choice(PALETTES),
                     symmetry=rng.randint(1, 16), particle_mode=rng.choice(PARTICLE_MODES),
                     feedback=rng.random(), camera_speed=rng.random(), projectm_mix=rng.random(),
                     transition={"mode": rng.choice(("glide", "cut", "on_next_kick")), "beats": rng.randint(1, 16)})
        send(state, d, now=now, heartbeat=index)
        if index % 11 == 0:
            state.set_manual("palette", rng.choice(PALETTES), now)
        frame = state.update(now, kick=index % 3 == 0, section="drop" if index % 19 == 0 else "steady")
        for field, domain in domains.items():
            distribution = getattr(frame, f"{field}_weights")
            assert 1 <= len(distribution) <= len(domain)
            assert len({value for value, _ in distribution}) == len(distribution)
            assert all(value in domain and math.isfinite(weight) and 0 < weight <= 1 for value, weight in distribution)
            assert sum(weight for _, weight in distribution) == pytest.approx(1)
            assert 0 <= getattr(frame, f"{field}_mix") <= 1
        assert 0 <= frame.feedback <= .98
        assert 0 <= frame.camera_speed <= 1
        assert 0 <= frame.projectm_mix <= 1


def test_repeated_decisions_do_not_starve_kickless_timeout():
    state = LinuxControlState(120)
    mode = {"mode": "on_next_kick", "beats": 2}
    send(state, decision(scene="fractal_temple", transition=mode))
    for now in (.5, 1, 1.5):
        send(state, decision(scene="kaleido_mesh", transition=mode), now=now)
        assert state.update(now).scene == "tunnel"
    frame = state.update(2)
    assert frame.scene == "kaleido_mesh"
    assert not frame.pending_fields


@pytest.mark.parametrize("field,value", [
    ("scene", "kaleido_mesh"), ("palette", "mono_white"),
    ("particle_mode", "none"), ("symmetry", 3), ("feedback", .11),
    ("camera_speed", .12), ("projectm_mix", .13),
])
def test_every_render_field_has_independent_manual_freeze(field, value):
    state = LinuxControlState()
    send(state)
    state.set_manual(field, value, .1)
    send(state, now=.2)
    assert getattr(state.update(.2, kick=True, section="drop"), field) == value
    assert getattr(state.update(30.09), field) == value
    send(state, now=30.1)
    assert getattr(state.update(30.1), field) == decision()[field]


def test_manual_edit_removes_its_field_from_old_drop_plan_even_after_expiry():
    state = LinuxControlState()
    send(state)
    state.set_manual("scene", "kaleido_mesh", 1)
    frame = state.update(40, kick=True, section="drop")
    assert frame.scene == "kaleido_mesh"
    assert frame.palette == "infrared"


def test_same_time_manual_cut_does_not_mutate_old_frame_snapshot():
    state = LinuxControlState()
    before = state.update(0)
    state.set_manual("scene", "particle_field", 0)
    assert before.scene_weights == (("tunnel", 1),)
    assert state.update(0).scene == "particle_field"


def test_duration_must_remain_finite_at_extreme_tempo():
    with pytest.raises(ValueError):
        LinuxControlState(1e-310)
