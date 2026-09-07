"""Tests for td/osc_in_callbacks.py — OSC routing into the director's pars.

Everything runs against ``td_stub``: a fake COMP with the real parameter
surface, no TouchDesigner anywhere.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TD_DIR = REPO / "td"
if str(TD_DIR) not in sys.path:
    sys.path.insert(0, str(TD_DIR))

import osc_in_callbacks as cb  # noqa: E402
import parspec  # noqa: E402
import td_stub  # noqa: E402

NOW = 1_000_000.0


@pytest.fixture
def director():
    return td_stub.director_comp()


@pytest.fixture
def logs():
    return []


def send(director, address, value, logs, now=NOW):
    return cb.route(director, address, [value], now=now, log=logs.append)


# -- the address map --------------------------------------------------------


def test_address_map_comes_from_the_schema():
    mapping = cb.address_map(force=True)
    assert mapping["/director/scene"][0] == "Scene"
    assert mapping["/director/camera_speed"][0] == "Cameraspeed"
    assert mapping["/feat/section"][0] == "Section"
    # the spec travels with the name, so coercion knows the schema range
    assert mapping["/director/feedback"][1].max == 0.98


def test_fallback_map_agrees_with_the_schema_map():
    """If parspec ever fails to import, the hard-coded names must still match."""
    specs = parspec.pars_from_schema(parspec.load_default_schema())
    derived = {spec.address: spec.name for spec in specs if spec.address}
    assert cb.FALLBACK_ADDRESSES == derived


# -- scalar routing ---------------------------------------------------------


CONTINUOUS = [
    ("/director/feedback", 0.42, "Feedback", 0.42),
    ("/director/camera_speed", 0.75, "Cameraspeed", 0.75),
    ("/director/projectm_mix", 0.5, "Projectmmix", 0.5),
    ("/director/transition_mode", "on_next_kick", "Transitionmode", "on_next_kick"),
    ("/director/transition_beats", 4, "Transitionbeats", 4),
    ("/director/intent", "lift into the drop", "Intent", "lift into the drop"),
    ("/feat/section", "breakdown", "Section", "breakdown"),
]

DISCRETE = [
    ("/director/scene", "particle_field", "Scene", "particle_field"),
    ("/director/palette", "infrared", "Palette", "infrared"),
    ("/director/symmetry", 8, "Symmetry", 8),
    ("/director/particle_mode", "burst", "Particlemode", "burst"),
]


@pytest.mark.parametrize(("address", "value", "parname", "expected"), CONTINUOUS)
def test_every_continuous_address_lands_on_its_par(director, logs, address, value, parname, expected):
    assert send(director, address, value, logs) == "set"
    assert getattr(director.par, parname).eval() == expected
    assert logs == []


@pytest.mark.parametrize(("address", "value", "parname", "expected"), DISCRETE)
def test_every_discrete_address_lands_when_the_transition_is_a_cut(
    director, logs, address, value, parname, expected
):
    director.par.Transitionmode = "cut"
    assert send(director, address, value, logs) == "set"
    assert getattr(director.par, parname).eval() == expected
    assert logs == []


def test_bare_scalar_args_work_too(director, logs):
    """python-osc hands over a list, but a single scalar must not break it."""
    assert cb.route(director, "/director/feedback", 0.3, now=NOW, log=logs.append) == "set"
    assert director.par.Feedback.eval() == pytest.approx(0.3)


def test_values_are_clamped_to_the_schema_range(director, logs):
    director.par.Transitionmode = "cut"
    assert send(director, "/director/feedback", 5.0, logs) == "set"
    assert director.par.Feedback.eval() == pytest.approx(0.98)
    assert send(director, "/director/symmetry", 99, logs) == "set"
    assert director.par.Symmetry.eval() == 16


def test_a_queued_discrete_is_clamped_when_it_is_queued_not_when_it_lands(director, logs):
    """Validation happens on arrival so the sidecar hears about a bad value at
    once, even though the write waits for a kick."""
    assert send(director, "/director/symmetry", 99, logs) == "pending"
    assert cb.pending_discrete(director)["Symmetry"]["value"] == 16


def test_numeric_strings_are_accepted(director, logs):
    assert send(director, "/director/camera_speed", "0.6", logs) == "set"
    assert director.par.Cameraspeed.eval() == pytest.approx(0.6)


# -- rejections -------------------------------------------------------------


def test_unknown_address_is_ignored_with_one_log_line(director, logs):
    before = director.par.Scene.eval()
    assert send(director, "/director/strobe", 1.0, logs) == "ignored"
    assert director.par.Scene.eval() == before
    assert len(logs) == 1
    assert "unknown OSC address" in logs[0]


def test_completely_foreign_address_is_ignored(director, logs):
    assert send(director, "/some/other/app", 1, logs) == "ignored"
    assert len(logs) == 1


def test_unknown_enum_value_does_not_reach_the_par(director, logs):
    assert send(director, "/director/scene", "hyper_cube", logs) == "error"
    assert director.par.Scene.eval() == "tunnel"
    assert len(logs) == 1


def test_a_missing_par_is_survivable(logs):
    comp = td_stub.Comp("director")  # no custom pars at all
    assert cb.route(comp, "/director/scene", ["tunnel"], now=NOW, log=logs.append) == "ignored"
    assert "no custom par" in logs[0]


def test_no_comp_is_survivable(logs):
    assert cb.route(None, "/director/scene", ["tunnel"], now=NOW, log=logs.append) == "ignored"


# -- transition -------------------------------------------------------------


def test_transition_as_two_messages(director, logs):
    send(director, "/director/transition_mode", "cut", logs)
    send(director, "/director/transition_beats", 6, logs)
    assert director.par.Transitionmode.eval() == "cut"
    assert director.par.Transitionbeats.eval() == 6


def test_transition_as_one_json_string(director, logs):
    payload = json.dumps({"mode": "on_next_kick", "beats": 8})
    assert send(director, "/director/transition", payload, logs) == "set"
    assert director.par.Transitionmode.eval() == "on_next_kick"
    assert director.par.Transitionbeats.eval() == 8


def test_transition_json_may_be_partial(director, logs):
    assert send(director, "/director/transition", json.dumps({"beats": 3}), logs) == "set"
    assert director.par.Transitionbeats.eval() == 3


def test_broken_transition_json_is_an_error_not_a_crash(director, logs):
    assert send(director, "/director/transition", "{not json", logs) == "error"
    assert len(logs) == 1


# -- on_drop ----------------------------------------------------------------


def test_on_drop_goes_to_the_text_dat_and_the_par(director, logs):
    payload = {"scene": "particle_field", "palette": "infrared", "particle_mode": "burst"}
    assert send(director, "/director/on_drop", json.dumps(payload), logs) == "set"
    assert json.loads(director.op("on_drop_dat").text) == payload
    assert json.loads(director.par.Ondrop.eval()) == payload
    assert logs == []


def test_on_drop_that_is_not_json_is_rejected(director, logs):
    assert send(director, "/director/on_drop", "particle_field", logs) == "error"
    assert director.op("on_drop_dat").text == ""
    assert len(logs) == 1


def test_on_drop_must_be_an_object(director, logs):
    assert send(director, "/director/on_drop", "[1, 2]", logs) == "error"
    assert director.op("on_drop_dat").text == ""


# -- heartbeat --------------------------------------------------------------


def test_heartbeat_stores_the_time_and_sets_the_par(director, logs):
    assert send(director, "/director/heartbeat", 7, logs) == "heartbeat"
    assert director.fetch(cb.HEARTBEAT_STORE_KEY) == NOW
    assert director.par.Heartbeat.eval() == 7
    assert logs == []


def test_heartbeat_without_a_usable_argument_still_counts(director, logs):
    cb.route(director, "/director/heartbeat", [], now=NOW, log=logs.append)
    assert director.fetch(cb.HEARTBEAT_STORE_KEY) == NOW
    assert director.par.Heartbeat.eval() == 1


# -- freeze (SPEC §5) -------------------------------------------------------
#
# "任何參數被手動一動，該欄位凍結 30 s" — in every mode, not just manual.
# manual is the stronger rule on top: nothing the director sends lands at all.


@pytest.mark.parametrize("mode", ["gpt", "rule", "manual"])
def test_a_touched_par_is_frozen_for_30s_in_every_mode(director, logs, mode):
    director.par.Mode = mode
    cb.note_touch(director, "Feedback", now=NOW)
    assert cb.is_frozen(director, "Feedback", NOW + 1) is True
    assert send(director, "/director/feedback", 0.9, logs, now=NOW + 1) == "frozen"
    assert director.par.Feedback.eval() == 0.0
    assert "frozen" in logs[0]


@pytest.mark.parametrize("mode", ["gpt", "rule"])
def test_the_freeze_expires_after_30s(director, logs, mode):
    director.par.Mode = mode
    cb.note_touch(director, "Feedback", now=NOW)
    assert cb.is_frozen(director, "Feedback", NOW + 31) is False
    assert send(director, "/director/feedback", 0.9, logs, now=NOW + 31) == "set"
    assert director.par.Feedback.eval() == pytest.approx(0.9)


@pytest.mark.parametrize("mode", ["gpt", "rule"])
def test_the_freeze_only_covers_the_touched_par(director, logs, mode):
    director.par.Mode = mode
    cb.note_touch(director, "Feedback", now=NOW)
    assert send(director, "/director/camera_speed", 0.4, logs, now=NOW + 1) == "set"
    assert director.par.Cameraspeed.eval() == pytest.approx(0.4)


def test_manual_mode_blocks_every_director_write_touched_or_not(director, logs):
    director.par.Mode = "manual"
    assert cb.is_frozen(director, "Cameraspeed", NOW) is True
    assert send(director, "/director/camera_speed", 0.4, logs) == "frozen"
    assert director.par.Cameraspeed.eval() == 0.0
    assert send(director, "/director/scene", "particle_field", logs) == "frozen"
    assert director.par.Scene.eval() == "tunnel"
    assert cb.pending_discrete(director) == {}


def test_manual_mode_still_takes_the_section_and_the_heartbeat(director, logs):
    """Show state, not a director decision: freezing these would make a manual
    show look like a dead director to the watchdog."""
    director.par.Mode = "manual"
    assert send(director, "/feat/section", "drop", logs) == "set"
    assert director.par.Section.eval() == "drop"
    assert send(director, "/director/heartbeat", 9, logs) == "heartbeat"
    assert director.par.Heartbeat.eval() == 9
    assert director.fetch(cb.HEARTBEAT_STORE_KEY) == NOW
    assert logs == []


def test_a_touched_section_or_heartbeat_is_never_frozen(director):
    cb.note_touch(director, "Section", now=NOW)
    cb.note_touch(director, "Heartbeat", now=NOW)
    assert cb.is_frozen(director, "Section", NOW + 1) is False
    assert cb.is_frozen(director, "Heartbeat", NOW + 1) is False


def test_on_drop_respects_the_freeze(director, logs):
    director.par.Mode = "rule"
    cb.note_touch(director, "Ondrop", now=NOW)
    payload = json.dumps({"scene": "tunnel", "palette": "infrared", "particle_mode": "rain"})
    assert send(director, "/director/on_drop", payload, logs, now=NOW + 5) == "frozen"
    assert director.op("on_drop_dat").text == ""


def test_manual_mode_also_blocks_on_drop(director, logs):
    director.par.Mode = "manual"
    payload = json.dumps({"scene": "tunnel", "palette": "infrared", "particle_mode": "rain"})
    assert send(director, "/director/on_drop", payload, logs) == "frozen"
    assert director.op("on_drop_dat").text == ""


def test_clear_touches_unfreezes_everything(director):
    director.par.Mode = "rule"
    cb.note_touch(director, "Feedback", now=NOW)
    cb.clear_touches(director)
    assert cb.is_frozen(director, "Feedback", NOW + 1) is False


def test_note_touch_uses_the_wall_clock_by_default(director):
    director.par.Mode = "gpt"
    stamp = cb.note_touch(director, "Symmetry")
    assert cb.is_frozen(director, "Symmetry", stamp + 1) is True


# -- the director's own writes must not freeze the director ------------------


def test_a_director_write_does_not_start_a_freeze(director, logs):
    """The Parameter Execute DAT cannot tell a script write from a human hand.
    Without the echo guard, one OSC message would freeze its own parameter for
    30 s and the director would go silent after a single decision."""
    assert send(director, "/director/feedback", 0.42, logs) == "set"
    cb.onValueChange(director.par.Feedback, 0.0)          # what TD fires next
    assert cb.is_frozen(director, "Feedback", NOW) is False
    assert send(director, "/director/feedback", 0.7, logs) == "set"
    assert director.par.Feedback.eval() == pytest.approx(0.7)


def test_a_second_change_with_no_script_write_behind_it_does_freeze(director, logs):
    """Each script write is claimable exactly once, so a human moving the same
    fader right after the director still wins."""
    send(director, "/director/feedback", 0.42, logs)
    cb.onValueChange(director.par.Feedback, 0.0)          # the echo
    cb.onValueChange(director.par.Feedback, 0.42)         # a hand on the fader
    assert cb.is_frozen(director, "Feedback") is True


# -- TD entry points --------------------------------------------------------


def test_director_from_finds_the_parent_comp(director):
    dat = director.op("osc_in")
    assert cb.director_from(dat) is director


def test_director_from_walks_up_one_level(director):
    """The DAT still works if someone drags it out of the director COMP."""
    amv = director.parent()
    stray = td_stub.TextDAT("stray_osc_in", amv)
    assert cb.director_from(stray) is director


def test_director_from_gives_up_quietly():
    assert cb.director_from(None) is None
    assert cb.director_from(td_stub.TextDAT("lonely")) is None


def test_on_receive_osc_signature_and_routing(director):
    dat = director.op("osc_in")
    director.par.Transitionmode = "cut"
    result = cb.onReceiveOSC(dat, 0, ["/director/scene", "tunnel"], b"", 0.0,
                             "/director/scene", ["particle_field"], ("127.0.0.1", 9001))
    assert result == "set"
    assert director.par.Scene.eval() == "particle_field"


def test_on_value_change_starts_the_freeze(director):
    director.par.Mode = "rule"
    par = director.par.Feedback
    cb.onValueChange(par, 0.0)
    assert cb.is_frozen(director, "Feedback") is True


# -- discrete pars wait for the next kick (SPEC §3.3) ------------------------


@pytest.mark.parametrize(("address", "value", "parname", "expected"), DISCRETE)
def test_a_discrete_par_is_queued_instead_of_written(director, logs, address, value, parname, expected):
    before = getattr(director.par, parname).eval()
    assert send(director, address, value, logs) == "pending"
    assert getattr(director.par, parname).eval() == before
    entry = cb.pending_discrete(director)[parname]
    assert entry == {"value": expected, "t": NOW}


def test_a_float_par_never_waits_for_a_kick(director, logs):
    """SPEC §3.3: floats glide through the Lag CHOPs, so they land at once."""
    for address, parname in (("/director/feedback", "Feedback"),
                             ("/director/camera_speed", "Cameraspeed"),
                             ("/director/projectm_mix", "Projectmmix")):
        assert send(director, address, 0.5, logs) == "set"
        assert getattr(director.par, parname).eval() == pytest.approx(0.5)
    assert cb.pending_discrete(director) == {}


def test_on_next_kick_and_glide_both_queue(director, logs):
    for mode in ("glide", "on_next_kick"):
        director.par.Transitionmode = mode
        assert send(director, "/director/scene", "particle_field", logs) == "pending"
    assert director.par.Scene.eval() == "tunnel"


def test_a_bad_enum_is_rejected_on_arrival_not_queued(director, logs):
    assert send(director, "/director/scene", "hyper_cube", logs) == "error"
    assert cb.pending_discrete(director) == {}


def test_the_newest_queued_value_wins(director, logs):
    send(director, "/director/scene", "particle_field", logs)
    send(director, "/director/scene", "fractal_temple", logs, now=NOW + 1)
    assert cb.pending_discrete(director)["Scene"] == {"value": "fractal_temple", "t": NOW + 1}


def test_the_two_modules_agree_on_the_pending_store_key():
    import drop_executor

    assert cb.PENDING_STORE_KEY == drop_executor.PENDING_STORE_KEY
    assert cb.SCRIPT_WRITE_KEY == drop_executor.SCRIPT_WRITE_KEY
