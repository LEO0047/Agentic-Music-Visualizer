"""Tests for td/drop_executor.py — the on_drop one-shot and the watchdog."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TD_DIR = REPO / "td"
if str(TD_DIR) not in sys.path:
    sys.path.insert(0, str(TD_DIR))

import drop_executor as dx  # noqa: E402
import osc_in_callbacks as cb  # noqa: E402
import td_stub  # noqa: E402

NOW = 2_000_000.0
PAYLOAD = {"scene": "particle_field", "palette": "infrared", "particle_mode": "burst"}


@pytest.fixture
def director():
    return td_stub.director_comp()


@pytest.fixture
def armed(director):
    """A director in the drop section with an on_drop payload waiting."""
    director.par.Section = "drop"
    director.op("on_drop_dat").text = json.dumps(PAYLOAD)
    director.par.Ondrop = json.dumps(PAYLOAD)
    return director


@pytest.fixture
def logs():
    return []


# -- firing -----------------------------------------------------------------


def test_on_kick_applies_the_payload(armed, logs):
    applied = dx.on_kick(armed, NOW, log=logs.append)
    assert applied == {"Scene": "particle_field", "Palette": "infrared", "Particlemode": "burst"}
    assert armed.par.Scene.eval() == "particle_field"
    assert armed.par.Palette.eval() == "infrared"
    assert armed.par.Particlemode.eval() == "burst"


def test_on_kick_fires_once_and_clears(armed, logs):
    assert dx.on_kick(armed, NOW, log=logs.append) is not None
    assert armed.op("on_drop_dat").text == ""
    assert armed.par.Ondrop.eval() == ""
    # a second kick in the same drop must not re-fire
    armed.par.Scene = "tunnel"
    assert dx.on_kick(armed, NOW + 0.5, log=logs.append) is None
    assert armed.par.Scene.eval() == "tunnel"


def test_on_kick_records_when_the_drop_happened(armed, logs):
    dx.on_kick(armed, NOW, log=logs.append)
    assert armed.fetch("last_drop") == NOW


def test_the_write_is_a_cut(armed, logs):
    """Scene/Palette/Particlemode are menus: no Lag CHOP touches them, so the
    value lands on the very frame the kick fires."""
    dx.on_kick(armed, NOW, log=logs.append)
    assert armed.par.Scene.writes[-1] == "particle_field"


# -- not firing -------------------------------------------------------------


def test_no_drop_section_means_no_fire(director, logs):
    director.op("on_drop_dat").text = json.dumps(PAYLOAD)
    assert director.par.Section.eval() == "steady"
    assert dx.on_kick(director, NOW, log=logs.append) is None
    assert director.op("on_drop_dat").text != ""
    assert director.par.Scene.eval() == "tunnel"


def test_empty_payload_means_no_fire(director, logs):
    director.par.Section = "drop"
    assert dx.on_kick(director, NOW, log=logs.append) is None
    assert logs == []


def test_a_local_drop_flag_also_arms_the_kick(director, logs):
    director.op("on_drop_dat").text = json.dumps(PAYLOAD)
    assert dx.drop_pending(director) is False
    dx.flag_drop(director, NOW)
    assert dx.drop_pending(director) is True
    assert dx.on_kick(director, NOW, log=logs.append) is not None
    assert dx.drop_pending(director) is False


def test_broken_json_is_cleared_not_retried(armed, logs):
    armed.op("on_drop_dat").text = "{not json"
    assert dx.on_kick(armed, NOW, log=logs.append) is None
    assert armed.op("on_drop_dat").text == ""
    assert len(logs) == 1


def test_non_object_payload_is_cleared(armed, logs):
    armed.op("on_drop_dat").text = "[1, 2, 3]"
    assert dx.on_kick(armed, NOW, log=logs.append) is None
    assert armed.op("on_drop_dat").text == ""


def test_unknown_enum_in_the_payload_is_skipped_not_fatal(armed, logs):
    armed.op("on_drop_dat").text = json.dumps(dict(PAYLOAD, scene="hyper_cube"))
    applied = dx.on_kick(armed, NOW, log=logs.append)
    assert applied == {"Palette": "infrared", "Particlemode": "burst"}
    assert armed.par.Scene.eval() == "tunnel"
    assert any("hyper_cube" in line for line in logs)


def test_partial_payload_applies_what_it_has(armed, logs):
    armed.op("on_drop_dat").text = json.dumps({"palette": "acid_lime"})
    assert dx.on_kick(armed, NOW, log=logs.append) == {"Palette": "acid_lime"}


def test_par_only_fallback_when_the_text_dat_is_missing(logs):
    """The Ondrop par carries the same JSON, so a renamed DAT is not fatal."""
    import parspec

    comp = td_stub.Comp("director")
    td_stub.build_pars(comp, parspec.pars_from_schema(parspec.load_default_schema()))
    comp.par.Section = "drop"
    comp.par.Ondrop = json.dumps(PAYLOAD)
    assert dx.on_kick(comp, NOW, log=logs.append) is not None
    assert comp.par.Scene.eval() == "particle_field"
    assert comp.par.Ondrop.eval() == ""


def test_the_osc_callback_and_the_executor_agree(director, logs):
    """End to end: /director/on_drop lands, Section flips, the kick fires it."""
    cb.route(director, "/director/on_drop", [json.dumps(PAYLOAD)], now=NOW, log=logs.append)
    cb.route(director, "/feat/section", ["drop"], now=NOW, log=logs.append)
    assert dx.on_kick(director, NOW + 0.1, log=logs.append) is not None
    assert director.par.Scene.eval() == "particle_field"


# -- discrete pars land on the next kick (SPEC §3.3) ------------------------


def test_a_pending_discrete_is_applied_on_the_next_kick(director, logs):
    assert cb.route(director, "/director/scene", ["particle_field"], now=NOW, log=logs.append) == "pending"
    assert director.par.Scene.eval() == "tunnel"
    assert dx.on_kick(director, NOW + 0.4, log=logs.append) == {"Scene": "particle_field"}
    assert director.par.Scene.eval() == "particle_field"
    assert director.fetch(dx.PENDING_STORE_KEY, {}) in ({}, None)


def test_several_pending_discretes_land_together(director, logs):
    cb.route(director, "/director/scene", ["particle_field"], now=NOW, log=logs.append)
    cb.route(director, "/director/palette", ["acid_lime"], now=NOW, log=logs.append)
    cb.route(director, "/director/symmetry", [6], now=NOW, log=logs.append)
    applied = dx.on_kick(director, NOW + 0.2, log=logs.append)
    assert applied == {"Scene": "particle_field", "Palette": "acid_lime", "Symmetry": 6}
    assert director.par.Symmetry.eval() == 6


def test_a_kick_with_nothing_waiting_still_returns_none(director, logs):
    assert dx.on_kick(director, NOW, log=logs.append) is None


def test_flush_applies_a_pending_value_after_2s_without_a_kick(director, logs):
    """A breakdown can run for bars with no kick; the decision still has to land."""
    cb.route(director, "/director/palette", ["infrared"], now=NOW, log=logs.append)
    assert dx.flush_pending(director, NOW + 1.0, log=logs.append) == {}
    assert director.par.Palette.eval() == "violet_cyan"
    assert dx.flush_pending(director, NOW + 2.1, log=logs.append) == {"Palette": "infrared"}
    assert director.par.Palette.eval() == "infrared"


def test_flush_leaves_a_younger_entry_queued(director, logs):
    cb.route(director, "/director/palette", ["infrared"], now=NOW, log=logs.append)
    cb.route(director, "/director/scene", ["particle_field"], now=NOW + 2.0, log=logs.append)
    assert dx.flush_pending(director, NOW + 2.5, log=logs.append) == {"Palette": "infrared"}
    assert set(director.fetch(dx.PENDING_STORE_KEY, {})) == {"Scene"}
    assert dx.on_kick(director, NOW + 2.6, log=logs.append) == {"Scene": "particle_field"}


def test_flush_max_wait_is_configurable(director, logs):
    cb.route(director, "/director/scene", ["particle_field"], now=NOW, log=logs.append)
    assert dx.flush_pending(director, NOW + 0.2, max_wait_s=0.1, log=logs.append) == {
        "Scene": "particle_field"
    }


def test_flush_with_an_empty_queue_is_a_no_op(director, logs):
    assert dx.flush_pending(director, NOW, log=logs.append) == {}
    assert logs == []


def test_a_cut_transition_never_queues(director, logs):
    director.par.Transitionmode = "cut"
    assert cb.route(director, "/director/scene", ["particle_field"], now=NOW, log=logs.append) == "set"
    assert director.par.Scene.eval() == "particle_field"
    assert dx.on_kick(director, NOW + 0.1, log=logs.append) is None


def test_on_drop_beats_a_pending_value_for_the_same_par(armed, logs):
    """The director chose the on_drop scene *for this drop*; a scene that was
    merely waiting for a kick must not overwrite it a microsecond later."""
    cb.route(armed, "/director/scene", ["fractal_temple"], now=NOW, log=logs.append)
    assert cb.pending_discrete(armed)["Scene"]["value"] == "fractal_temple"
    applied = dx.on_kick(armed, NOW + 0.3, log=logs.append)
    assert applied["Scene"] == "particle_field"       # from on_drop
    assert armed.par.Scene.eval() == "particle_field"
    assert armed.par.Scene.writes.count("fractal_temple") == 0
    assert armed.fetch(dx.PENDING_STORE_KEY, {}) in ({}, None)


def test_a_pending_par_the_drop_did_not_set_still_lands(armed, logs):
    armed.op("on_drop_dat").text = json.dumps({"scene": "tunnel"})
    cb.route(armed, "/director/symmetry", [12], now=NOW, log=logs.append)
    applied = dx.on_kick(armed, NOW + 0.3, log=logs.append)
    assert applied == {"Scene": "tunnel", "Symmetry": 12}


def test_applying_a_pending_value_does_not_freeze_it(director, logs):
    """The write goes through the Parameter Execute DAT like any other; without
    the echo guard the par would freeze itself for 30 s (SPEC §5)."""
    cb.route(director, "/director/scene", ["particle_field"], now=NOW, log=logs.append)
    dx.on_kick(director, NOW + 0.1, log=logs.append)
    cb.onValueChange(director.par.Scene, "tunnel")
    assert cb.is_frozen(director, "Scene", NOW + 0.2) is False


def test_a_malformed_queue_entry_is_dropped_not_fatal(director, logs):
    director.store(dx.PENDING_STORE_KEY, {"Scene": {"nope": 1}})
    assert dx.on_kick(director, NOW, log=logs.append) is None
    assert director.fetch(dx.PENDING_STORE_KEY, {}) in ({}, None)
    assert any("malformed" in line for line in logs)


# -- watchdog ---------------------------------------------------------------


def test_watchdog_reports_the_age(director):
    director.store(dx.HEARTBEAT_STORE_KEY, NOW)
    assert dx.heartbeat_watchdog(director, NOW + 12.0) is False
    assert director.par.Heartbeatage.eval() == pytest.approx(12.0)


def test_watchdog_trips_after_45s(director):
    director.store(dx.HEARTBEAT_STORE_KEY, NOW)
    assert dx.heartbeat_watchdog(director, NOW + 45.0) is False
    assert dx.heartbeat_watchdog(director, NOW + 45.1) is True
    assert director.par.Heartbeatage.eval() == pytest.approx(45.1)


def test_watchdog_measures_from_the_first_call_when_no_heartbeat_ever_arrived(director):
    assert dx.heartbeat_watchdog(director, NOW) is False
    assert dx.heartbeat_watchdog(director, NOW + 10) is False
    assert dx.heartbeat_watchdog(director, NOW + 46) is True


def test_a_heartbeat_resets_the_watchdog(director, logs):
    dx.heartbeat_watchdog(director, NOW)
    assert dx.heartbeat_watchdog(director, NOW + 46) is True
    cb.route(director, "/director/heartbeat", [3], now=NOW + 50, log=logs.append)
    assert dx.heartbeat_watchdog(director, NOW + 51) is False
    assert director.par.Heartbeatage.eval() == pytest.approx(1.0)


def test_watchdog_never_goes_negative(director):
    director.store(dx.HEARTBEAT_STORE_KEY, NOW + 5)
    assert dx.heartbeat_watchdog(director, NOW) is False
    assert director.par.Heartbeatage.eval() == 0.0


def test_watchdog_timeout_is_configurable(director):
    director.store(dx.HEARTBEAT_STORE_KEY, NOW)
    assert dx.heartbeat_watchdog(director, NOW + 10, timeout=5) is True


def test_watchdog_survives_a_comp_without_the_par():
    comp = td_stub.Comp("director")
    comp.store(dx.HEARTBEAT_STORE_KEY, NOW)
    assert dx.heartbeat_watchdog(comp, NOW + 60) is True
