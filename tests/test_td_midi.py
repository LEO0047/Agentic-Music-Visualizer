"""Phase 6 — the MIDI override, checked without TouchDesigner and without a
controller.

Two halves, and the second one is the point of the phase:

* ``td/midi_override.py`` is pure logic — 0–127 onto a parameter's own range,
  the CHOP channel-name grammar, the two callbacks. Straightforward to pin.
* The *freeze* is not implemented in that file at all. It is implemented by
  ``on_cc`` deliberately **not** leaving the ``script_write`` echo marker that
  every other writer leaves, so ``osc_in_callbacks.onValueChange`` reads the
  write as a hand on the fader. That claim is worth more than any of the unit
  tests here, so it gets tested end to end: knob → onValueChange → the
  director's next OSC write for that field is skipped, while a different field
  still lands (``test_a_knob_freezes_that_field_against_the_director``).

What is *not* provable here is the same class of thing Phase 5 could not
prove: that ``midiinCHOP`` is the class name, that its channels really are
called ``ch1c1``, and that a real controller reaches TD at all. Those are the
``# VERIFY`` rows #36–#41 in ``td/README.md`` and the manual acceptance step.
"""

from __future__ import annotations

import ast
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TD_DIR = REPO / "td"
if str(TD_DIR) not in sys.path:
    sys.path.insert(0, str(TD_DIR))

import midi_override as midi  # noqa: E402
import osc_in_callbacks as cb  # noqa: E402
import parspec  # noqa: E402
import td_stub  # noqa: E402

from test_td_build_compiles import Recorder  # noqa: E402  (the shared harness)

NOW = 1_000_000.0


@pytest.fixture
def director():
    return td_stub.director_comp()


@pytest.fixture
def logs():
    return []


@pytest.fixture
def specs():
    return parspec.specs_by_name(parspec.pars_from_schema(parspec.load_default_schema()))


# --------------------------------------------------------------------------
# the map itself
# --------------------------------------------------------------------------


def test_every_mapped_cc_names_a_real_director_parameter(specs):
    for cc, name in midi.DEFAULT_CC_MAP.items():
        assert name in specs, f"CC {cc} points at {name!r}, which is not a custom par"


def test_the_mapped_parameters_are_the_ones_a_knob_can_drive(specs):
    """No ``Str`` par is mapped: a knob has nothing to say to JSON or prose."""
    styles = {specs[name].style for name in midi.DEFAULT_CC_MAP.values()}
    assert styles <= {"Float", "Int", "Menu"}
    assert "Ondrop" not in midi.DEFAULT_CC_MAP.values()
    assert "Intent" not in midi.DEFAULT_CC_MAP.values()


def test_the_cc_map_is_the_seven_documented_knobs():
    assert midi.DEFAULT_CC_MAP == {
        1: "Feedback",
        2: "Cameraspeed",
        3: "Projectmmix",
        4: "Symmetry",
        5: "Scene",
        6: "Palette",
        7: "Particlemode",
    }


def test_the_note_map_is_the_three_modes():
    assert midi.DEFAULT_NOTE_MAP == {60: "gpt", 61: "rule", 62: "manual"}
    assert set(midi.DEFAULT_NOTE_MAP.values()) == set(parspec.MODES)


# --------------------------------------------------------------------------
# cc_to_par_value: float / int / menu
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "cc_value", "expected"),
    [
        ("Feedback", 0, 0.0),
        ("Feedback", 127, 0.98),          # the schema ceiling, not 1.0
        ("Feedback", 64, 0.98 * 64 / 127),
        ("Cameraspeed", 127, 1.0),
        ("Projectmmix", 0, 0.0),
    ],
)
def test_a_float_par_gets_the_schema_range(specs, name, cc_value, expected):
    value = midi.cc_to_par_value(specs[name], cc_value)
    assert isinstance(value, float)
    assert value == pytest.approx(expected)


@pytest.mark.parametrize(("cc_value", "expected"), [(0, 1), (127, 16), (64, 9)])
def test_an_int_par_is_rounded_into_its_range(specs, cc_value, expected):
    value = midi.cc_to_par_value(specs["Symmetry"], cc_value)
    assert isinstance(value, int)
    assert value == expected


def test_the_int_ramp_is_monotonic_and_never_leaves_the_range(specs):
    values = [midi.cc_to_par_value(specs["Symmetry"], cc) for cc in range(128)]
    assert values == sorted(values)
    assert min(values) == 1 and max(values) == 16


@pytest.mark.parametrize(
    ("name", "cc_value", "expected"),
    [
        ("Scene", 0, "fractal_temple"),
        ("Scene", 127, "projectm_blend"),
        ("Palette", 64, "amber_dusk"),
        ("Particlemode", 0, "spiral"),
        ("Particlemode", 127, "none"),
    ],
)
def test_a_menu_par_gets_a_menu_name_chosen_by_index(specs, name, cc_value, expected):
    assert midi.cc_to_par_value(specs[name], cc_value) == expected


def test_every_menu_entry_gets_an_equal_share_of_the_knob(specs):
    """128 CC steps over 5 scenes: each entry owns ~25 steps, none is a sliver."""
    hits = [midi.cc_to_par_value(specs["Scene"], cc) for cc in range(128)]
    counts = {name: hits.count(name) for name in specs["Scene"].menu_names}
    assert set(counts) == set(specs["Scene"].menu_names)
    assert max(counts.values()) - min(counts.values()) <= 1


def test_a_menu_value_is_always_writable_to_its_par(director, specs):
    """The menu *name* comes back, not an index, so it can be assigned as-is."""
    for cc in (0, 40, 90, 127):
        director.par.Scene = midi.cc_to_par_value(specs["Scene"], cc)


def test_out_of_range_and_nonsense_values(specs):
    assert midi.cc_to_par_value(specs["Feedback"], -20) == pytest.approx(0.0)
    assert midi.cc_to_par_value(specs["Feedback"], 999) == pytest.approx(0.98)
    assert midi.cc_to_par_value(specs["Cameraspeed"], "64") == pytest.approx(64 / 127)
    with pytest.raises(ValueError):
        midi.cc_to_par_value(specs["Feedback"], "loud")
    with pytest.raises(ValueError):
        midi.cc_to_par_value(specs["Feedback"], float("nan"))


def test_a_string_par_refuses_a_knob(specs):
    with pytest.raises(ValueError):
        midi.cc_to_par_value(specs["Intent"], 64)


def test_a_live_td_par_works_as_a_spec_too(director):
    """Without parspec importable the ranges come off the TD par itself, which
    spells the same four things differently (``menuNames``)."""
    assert midi.cc_to_par_value(director.par.Scene, 0) == "fractal_temple"
    assert midi.cc_to_par_value(director.par.Feedback, 127) == pytest.approx(0.98)
    assert midi.cc_to_par_value(director.par.Symmetry, 127) == 16


# --------------------------------------------------------------------------
# CHOP channel names
# --------------------------------------------------------------------------


def test_the_channel_name_round_trips():
    for cc in midi.DEFAULT_CC_MAP:
        assert midi.parse_channel(midi.midi_channel_name(cc)) == ("cc", cc)
    for note in midi.DEFAULT_NOTE_MAP:
        assert midi.parse_channel(midi.note_channel_name(note)) == ("note", note)


def test_the_documented_spelling_is_the_ch1c1_pattern():
    assert midi.midi_channel_name(1) == "ch1c1"
    assert midi.midi_channel_name(7) == "ch1c7"
    assert midi.note_channel_name(60) == "ch1n60"


@pytest.mark.parametrize("name", ["", None, "chan1", "ch1p", "c1", "ch1c", "tx"])
def test_an_unparseable_channel_is_ignored_not_raised(name):
    assert midi.parse_channel(name) == (None, None)


def test_another_midi_channel_still_parses():
    """The CHOP is set to channel 1, but a message that arrives anyway is
    better applied than silently dropped."""
    assert midi.parse_channel("ch10c4") == ("cc", 4)


# --------------------------------------------------------------------------
# on_cc / on_note
# --------------------------------------------------------------------------


def test_on_cc_writes_the_par_and_returns_its_name(director, logs):
    assert midi.on_cc(director, 1, 127, NOW, log=logs.append) == "Feedback"
    assert director.par.Feedback.eval() == pytest.approx(0.98)
    assert logs == []


def test_on_cc_writes_a_menu_par_straight_through(director, logs):
    """No waiting for the next kick: a hand on a knob is not a director
    decision on a musical grid (osc_in_callbacks queues these; this does not)."""
    assert midi.on_cc(director, 5, 127, NOW, log=logs.append) == "Scene"
    assert director.par.Scene.eval() == "projectm_blend"
    assert cb.pending_discrete(director) == {}


def test_on_cc_records_a_breadcrumb(director):
    midi.on_cc(director, 4, 127, NOW)
    assert director.fetch(midi.MIDI_STORE_KEY)["Symmetry"] == {"value": 16, "t": NOW}


@pytest.mark.parametrize("cc", [0, 8, 64, 127, -1])
def test_an_unmapped_cc_is_ignored(director, logs, cc):
    before = [par.eval() for par in director.par]
    assert midi.on_cc(director, cc, 100, NOW, log=logs.append) is None
    assert [par.eval() for par in director.par] == before
    assert len(logs) == 1 and "not mapped" in logs[0]


def test_a_custom_cc_map_is_honoured(director, logs):
    assert midi.on_cc(director, 20, 127, NOW, cc_map={20: "Feedback"}, log=logs.append) == "Feedback"
    assert director.par.Feedback.eval() == pytest.approx(0.98)
    assert midi.on_cc(director, 1, 127, NOW, cc_map={20: "Feedback"}, log=logs.append) is None


def test_on_cc_survives_a_missing_par_and_a_missing_comp(logs):
    bare = td_stub.Comp("not_the_director")
    assert midi.on_cc(bare, 1, 64, NOW, log=logs.append) is None
    assert midi.on_cc(None, 1, 64, NOW, log=logs.append) is None
    assert all("no custom par" in line for line in logs)


def test_a_garbage_cc_number_is_ignored(director, logs):
    assert midi.on_cc(director, "knob", 64, NOW, log=logs.append) is None
    assert "not a number" in logs[0]


@pytest.mark.parametrize(("note", "mode"), sorted(midi.DEFAULT_NOTE_MAP.items()))
def test_on_note_switches_the_mode(director, logs, note, mode):
    assert midi.on_note(director, note, 100, NOW, log=logs.append) == mode
    assert director.par.Mode.eval() == mode


def test_the_three_pads_cycle_the_modes(director):
    for note, mode in sorted(midi.DEFAULT_NOTE_MAP.items()):
        midi.on_note(director, note, 64, NOW)
        assert director.par.Mode.eval() == mode


def test_a_note_off_does_not_switch_back(director):
    midi.on_note(director, 62, 100, NOW)
    assert director.par.Mode.eval() == "manual"
    assert midi.on_note(director, 60, 0, NOW) is None      # release of the gpt pad
    assert director.par.Mode.eval() == "manual"


def test_an_unmapped_note_is_ignored(director, logs):
    assert midi.on_note(director, 36, 100, NOW, log=logs.append) is None
    assert director.par.Mode.eval() == "rule"
    assert "not mapped" in logs[0]


def test_manual_mode_from_a_pad_freezes_everything(director, logs):
    """SPEC §5: the pad is how you take the whole show off the director."""
    midi.on_note(director, 62, 127, NOW)
    assert cb.route(director, "/director/feedback", [0.5], now=NOW, log=logs.append) == "frozen"
    assert cb.route(director, "/feat/section", ["drop"], now=NOW, log=logs.append) == "set"


# --------------------------------------------------------------------------
# the point of the phase: a knob freezes that field against the director
# --------------------------------------------------------------------------


def test_on_cc_leaves_no_script_write_marker(director):
    """The whole mechanism in one assertion. ``osc_in_callbacks.write_par``
    stores a marker so its own echo does not count as a touch; this must not,
    or the knob would be indistinguishable from the director and never freeze
    anything."""
    midi.on_cc(director, 1, 127, NOW)
    assert director.fetch(cb.SCRIPT_WRITE_KEY, {}) in ({}, None)
    assert cb.claim_script_write(director, "Feedback", 0.98) is False


def test_a_director_write_by_contrast_does_leave_one(director, logs):
    cb.route(director, "/director/feedback", [0.42], now=NOW, log=logs.append)
    assert cb.claim_script_write(director, "Feedback", 0.42) is True


def test_a_knob_freezes_that_field_against_the_director(director, logs):
    """SPEC §6 Phase 6, end to end, exactly as the operators are wired:

    knob → ``on_cc`` writes the par → TD fires the Parameter Execute DAT →
    ``osc_in_callbacks.onValueChange`` finds no script-write marker to claim →
    30 s freeze → the director's next ``/director/feedback`` is skipped, while
    ``/director/camera_speed`` still lands.
    """
    director.par.Mode = "gpt"
    before = director.par.Feedback.eval()

    assert midi.on_cc(director, 1, 127) == "Feedback"
    knob_value = director.par.Feedback.eval()
    assert knob_value != before

    # what TD fires next, on the same parameter (tests/test_td_callbacks.py
    # simulates the Parameter Execute DAT the same way)
    cb.onValueChange(director.par.Feedback, before)

    now = time.time()
    assert cb.is_frozen(director, "Feedback", now) is True
    assert cb.route(director, "/director/feedback", [0.5], now=now, log=logs.append) == "frozen"
    assert director.par.Feedback.eval() == pytest.approx(knob_value), "the knob still wins"

    # ... and only that field
    assert cb.is_frozen(director, "Cameraspeed", now) is False
    assert cb.route(director, "/director/camera_speed", [0.4], now=now, log=logs.append) == "set"
    assert director.par.Cameraspeed.eval() == pytest.approx(0.4)


def test_the_freeze_a_knob_starts_expires_after_30s(director, logs):
    """30 s, not forever: the director takes the field back on its own."""
    midi.on_cc(director, 1, 127)
    stamp = cb.note_touch(director, "Feedback", now=NOW)   # what onValueChange does
    assert cb.is_frozen(director, "Feedback", stamp + cb.FREEZE_SECONDS - 1) is True
    assert cb.is_frozen(director, "Feedback", stamp + cb.FREEZE_SECONDS + 1) is False
    assert cb.route(director, "/director/feedback", [0.5], now=stamp + 31, log=logs.append) == "set"
    assert director.par.Feedback.eval() == pytest.approx(0.5)


def test_a_knob_on_a_discrete_par_freezes_it_too(director, logs):
    """``Scene`` normally queues for the next kick; frozen, it is not even
    queued — SPEC §5 outranks SPEC §3.3's "wait for the kick"."""
    midi.on_cc(director, 5, 127)
    cb.onValueChange(director.par.Scene, "tunnel")
    now = time.time()
    assert cb.route(director, "/director/scene", ["tunnel"], now=now, log=logs.append) == "frozen"
    assert cb.pending_discrete(director) == {}
    assert director.par.Scene.eval() == "projectm_blend"


# --------------------------------------------------------------------------
# the network build (Recorder harness, no TD)
# --------------------------------------------------------------------------


@pytest.fixture
def recorder(monkeypatch):
    return Recorder(monkeypatch)


@pytest.fixture
def midi_rig(recorder):
    """``build_midi_override`` on an amv that already has a director COMP."""
    td_stub.Comp("director", recorder.amv)
    recorder.build_network.build_midi_override(recorder.amv, str(TD_DIR))
    return recorder


def child(recorder, parent_name, name):
    parent_comp = recorder.amv.children.get(parent_name)
    return None if parent_comp is None else parent_comp.children.get(name)


def test_the_midi_operators_have_the_right_types(midi_rig):
    assert midi_rig.optype_of("midi_in") == "midiinCHOP"
    assert child(midi_rig, "director", "midi_exec").opType == "chopexecuteDAT"


def test_the_midi_in_chop_is_set_to_one_device_and_one_channel(midi_rig):
    build_network = midi_rig.build_network
    assert midi_rig.par("midi_in", "device id") == build_network.MIDI_DEVICE
    assert midi_rig.par("midi_in", "channel channels") == build_network.MIDI_IN_CHANNEL
    assert build_network.MIDI_IN_CHANNEL == midi.MIDI_CHANNEL, (
        "the CHOP channel filter and the ch1c* names midi_override parses must agree"
    )
    assert midi_rig.par("midi_in", "active") is False


def test_the_exec_dat_watches_the_midi_chop_from_inside_the_director(midi_rig):
    """``midi_exec`` lives beside ``kick_exec``, so its source is a relative
    path out of the director COMP — the same shape as ``../kick``."""
    assert midi_rig.par("midi_exec", "chops chop") == "../midi_in"
    assert midi_rig.par("midi_exec", "valuechange") is True
    assert midi_rig.par("midi_exec", "offtoon") is False


def test_the_exec_dat_text_imports_and_calls_midi_override(midi_rig):
    text = child(midi_rig, "director", "midi_exec").text
    assert "import midi_override" in text
    assert "midi_override.parse_channel" in text
    assert "midi_override.on_cc" in text
    assert "midi_override.on_note" in text
    assert str(TD_DIR) in text, "the DAT must put td/ on sys.path after a TD restart"
    ast.parse(text)


def test_the_exec_dat_body_is_valid_python_on_its_own():
    import build_network

    ast.parse(build_network.MIDI_EXEC_BODY)
    ast.parse(build_network.bootstrap_code(str(TD_DIR), "midi_override")
              + build_network.MIDI_EXEC_BODY)


def test_nothing_on_the_midi_path_marks_its_writes_as_script_writes():
    """A marker anywhere on this path would cancel the freeze — and calling
    ``note_touch`` here instead would fake it, hiding a broken ``par_exec``."""
    import build_network

    assert "note_script_write" not in build_network.MIDI_EXEC_BODY
    tree = ast.parse((TD_DIR / "midi_override.py").read_text(encoding="utf-8"))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    assert "note_script_write" not in called
    assert "note_touch" not in called


def test_a_missing_midi_in_chop_class_is_skipped_not_fatal(monkeypatch, capsys):
    """No MIDI In CHOP in this build (or no controller) must cost a log line,
    not the network — Phase 5's rule, applied again."""
    rec = Recorder(monkeypatch, missing={"midiinCHOP"})
    td_stub.Comp("director", rec.amv)
    rec.build_network.build_midi_override(rec.amv, str(TD_DIR))   # must not raise
    assert rec.node("midi_in") is None
    assert child(rec, "director", "midi_exec") is not None
    assert "[amv] SKIP midi_in" in capsys.readouterr().out


def test_the_chop_execute_class_falls_back_like_kick_exec(monkeypatch):
    rec = Recorder(monkeypatch, missing={"chopexecuteDAT"})
    td_stub.Comp("director", rec.amv)
    rec.build_network.build_midi_override(rec.amv, str(TD_DIR))
    assert child(rec, "director", "midi_exec").opType == "chopexecDAT"


def test_build_director_wires_the_midi_override_in(monkeypatch):
    """The one call site: building the director builds the controller with it."""
    rec = Recorder(monkeypatch)
    specs = parspec.pars_from_schema(parspec.load_default_schema())
    rec.build_network.build_director(rec.amv, specs, str(TD_DIR))
    assert rec.node("midi_in") is not None
    assert child(rec, "director", "midi_exec") is not None


# --------------------------------------------------------------------------
# documentation
# --------------------------------------------------------------------------


def test_describe_map_lists_every_knob_and_pad():
    text = midi.describe_map()
    for cc, name in midi.DEFAULT_CC_MAP.items():
        assert midi.midi_channel_name(cc) in text
        assert name in text
    for note, mode in midi.DEFAULT_NOTE_MAP.items():
        assert midi.note_channel_name(note) in text
        assert mode in text


@pytest.mark.parametrize(
    "phrase",
    [
        "Phase 6",
        "midi_override",
        "ch1c1",
        "script_write",         # why the freeze happens at all
        "手動一動就凍結該欄位 30 秒",   # SPEC 6 Phase 6 的原句
        "DEFAULT_CC_MAP",       # how to change the map
        "midi_exec",
    ],
)
def test_the_readme_documents_the_override(phrase):
    assert phrase in (TD_DIR / "README.md").read_text(encoding="utf-8")


def test_the_readme_carries_the_default_map():
    readme = (TD_DIR / "README.md").read_text(encoding="utf-8")
    for cc, name in midi.DEFAULT_CC_MAP.items():
        assert "| %d |" % cc in readme, f"CC {cc} is not in the README table"
        assert name in readme
