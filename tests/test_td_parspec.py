"""Tests for td/parspec.py — the director schema → TD custom parameter map.

TouchDesigner is not installed here, so ``td/`` is put on ``sys.path`` the same
way ``build_network.py`` does inside TD, and the modules are imported by their
bare names exactly as a TD DAT would import them.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TD_DIR = REPO / "td"
if str(TD_DIR) not in sys.path:
    sys.path.insert(0, str(TD_DIR))

import parspec  # noqa: E402
import td_stub  # noqa: E402

TD_PAR_NAME = re.compile(r"[A-Z][a-z0-9]*\Z")


@pytest.fixture(scope="module")
def schema() -> dict:
    with open(REPO / "director_schema.json", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def specs(schema) -> list:
    return parspec.pars_from_schema(schema)


@pytest.fixture(scope="module")
def by_name(specs) -> dict:
    return parspec.specs_by_name(specs)


# -- par name rules ---------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("scene", "Scene"),
        ("camera_speed", "Cameraspeed"),
        ("projectm_mix", "Projectmmix"),
        ("particle_mode", "Particlemode"),
        ("transition_mode", "Transitionmode"),
        ("transition.beats", "Transitionbeats"),
        ("on_drop", "Ondrop"),
        ("BPM", "Bpm"),
    ],
)
def test_par_name_follows_td_rules(key, expected):
    assert parspec.par_name(key) == expected
    assert TD_PAR_NAME.match(expected)


@pytest.mark.parametrize("key", ["", "___", "1st", "3"])
def test_par_name_rejects_what_td_would_reject(key):
    with pytest.raises(ValueError):
        parspec.par_name(key)


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("Scene", True),
        ("Cameraspeed", True),
        ("Heartbeat2", True),
        ("scene", False),
        ("CameraSpeed", False),
        ("Camera_speed", False),
        ("", False),
        ("2scene", False),
    ],
)
def test_is_valid_par_name(name, ok):
    assert parspec.is_valid_par_name(name) is ok


def test_every_generated_name_is_legal(specs):
    for spec in specs:
        assert TD_PAR_NAME.match(spec.name), spec.name
        assert parspec.is_valid_par_name(spec.name)


def test_names_are_unique(specs):
    names = [spec.name for spec in specs]
    assert len(names) == len(set(names))


# -- schema coverage --------------------------------------------------------


def test_every_schema_field_is_covered(schema, specs):
    sources = {spec.source for spec in specs if spec.source}
    for key in schema["properties"]:
        if key == "transition":
            assert {"transition.mode", "transition.beats"} <= sources
        else:
            assert key in sources, f"schema field {key} has no par"


def test_schema_order_is_preserved(specs):
    schema_specs = [s for s in specs if s.page == parspec.PAGE_DIRECTOR]
    assert [s.name for s in schema_specs] == [
        "Scene",
        "Palette",
        "Feedback",
        "Symmetry",
        "Cameraspeed",
        "Particlemode",
        "Projectmmix",
        "Transitionmode",
        "Transitionbeats",
        "Ondrop",
        "Intent",
    ]


@pytest.mark.parametrize(
    ("name", "schema_key"),
    [("Scene", "scene"), ("Palette", "palette"), ("Particlemode", "particle_mode")],
)
def test_string_enums_become_menus_with_enum_values_as_names(by_name, schema, name, schema_key):
    spec = by_name[name]
    assert spec.style == "Menu"
    assert spec.append_method == "appendMenu"
    assert list(spec.menu_names) == schema["properties"][schema_key]["enum"]
    assert len(spec.menu_labels) == len(spec.menu_names)


def test_menu_labels_are_human_readable(by_name):
    spec = by_name["Scene"]
    assert spec.menu_labels[spec.menu_names.index("fractal_temple")] == "Fractal Temple"


@pytest.mark.parametrize(
    ("name", "low", "high"),
    [("Feedback", 0, 0.98), ("Cameraspeed", 0, 1), ("Projectmmix", 0, 1)],
)
def test_numbers_become_clamped_floats(by_name, name, low, high):
    spec = by_name[name]
    assert spec.style == "Float"
    assert spec.append_method == "appendFloat"
    assert (spec.min, spec.max) == (low, high)
    assert spec.clamp is True


@pytest.mark.parametrize(("name", "low", "high"), [("Symmetry", 1, 16), ("Transitionbeats", 1, 16)])
def test_integers_become_int_pars(by_name, name, low, high):
    spec = by_name[name]
    assert spec.style == "Int"
    assert spec.append_method == "appendInt"
    assert (spec.min, spec.max) == (low, high)


def test_transition_becomes_two_pars(by_name, schema):
    mode = by_name["Transitionmode"]
    beats = by_name["Transitionbeats"]
    assert mode.style == "Menu"
    assert list(mode.menu_names) == schema["properties"]["transition"]["properties"]["mode"]["enum"]
    assert beats.style == "Int"
    assert mode.address == "/director/transition_mode"
    assert beats.address == "/director/transition_beats"


def test_on_drop_is_a_single_string_par(by_name):
    spec = by_name["Ondrop"]
    assert spec.style == "Str"
    assert spec.append_method == "appendStr"
    assert spec.address == "/director/on_drop"


def test_intent_is_a_string_par(by_name):
    assert by_name["Intent"].style == "Str"


# -- non-schema pars --------------------------------------------------------


def test_runtime_pars_exist(by_name):
    assert by_name["Bpm"].style == "Float"
    assert by_name["Bpm"].default == 145
    assert by_name["Mode"].style == "Menu"
    assert list(by_name["Mode"].menu_names) == ["gpt", "rule", "manual"]
    assert by_name["Heartbeat"].style == "Int"
    assert by_name["Heartbeatage"].style == "Float"
    assert by_name["Heartbeatage"].readonly is True
    assert by_name["Record"].style == "Toggle"
    assert by_name["Record"].append_method == "appendToggle"


def test_section_par_is_a_menu_fed_by_feat_section(by_name):
    spec = by_name["Section"]
    assert spec.style == "Menu"
    assert list(spec.menu_names) == ["build", "drop", "breakdown", "steady"]
    assert spec.address == "/feat/section"


def test_runtime_pars_are_on_their_own_page(by_name):
    for name in ("Bpm", "Mode", "Heartbeat", "Heartbeatage", "Record", "Section"):
        assert by_name[name].page == parspec.PAGE_RUNTIME


def test_mode_defaults_to_rule(by_name):
    """SPEC §5: the show must run with the director offline."""
    assert by_name["Mode"].default == "rule"


# -- addresses --------------------------------------------------------------


def test_addresses_cover_the_spec_table(specs):
    addresses = set(parspec.specs_by_address(specs))
    assert addresses == {
        "/director/scene",
        "/director/palette",
        "/director/feedback",
        "/director/symmetry",
        "/director/camera_speed",
        "/director/particle_mode",
        "/director/projectm_mix",
        "/director/transition_mode",
        "/director/transition_beats",
        "/director/on_drop",
        "/director/intent",
        "/director/heartbeat",
        "/feat/section",
    }


def test_osc_key_is_the_last_path_element(by_name):
    assert by_name["Cameraspeed"].osc_key == "camera_speed"
    assert by_name["Bpm"].osc_key is None


# -- defaults are inside their own range ------------------------------------


def test_defaults_are_valid_for_their_par(specs):
    for spec in specs:
        if spec.style == "Menu":
            assert spec.default in spec.menu_names, spec.name
        elif spec.style in ("Float", "Int"):
            assert spec.clamp_value(spec.default) == spec.default, spec.name


# -- coercion ---------------------------------------------------------------


def test_menu_coercion_rejects_unknown_values(by_name):
    assert by_name["Scene"].coerce(" tunnel ") == "tunnel"
    with pytest.raises(ValueError):
        by_name["Scene"].coerce("hyper_cube")


def test_number_coercion_clamps(by_name):
    assert by_name["Feedback"].coerce(5.0) == 0.98
    assert by_name["Feedback"].coerce(-1) == 0.0
    assert by_name["Symmetry"].coerce(99) == 16
    assert by_name["Symmetry"].coerce("6.6") == 7
    assert isinstance(by_name["Symmetry"].coerce(3.0), int)


def test_toggle_coercion(by_name):
    record = by_name["Record"]
    assert record.coerce(1) == 1
    assert record.coerce(0) == 0
    assert record.coerce("off") == 0
    assert record.coerce("on") == 1


# -- lag --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("beats", "bpm", "expected"),
    [(4, 120, 2.0), (1, 145, 60.0 / 145), (16, 145, 16 * 60.0 / 145), (0, 145, 0.0)],
)
def test_lag_seconds(beats, bpm, expected):
    assert parspec.lag_seconds(beats, bpm) == pytest.approx(expected)


@pytest.mark.parametrize("bpm", [0, -20, "nonsense", None])
def test_lag_seconds_survives_a_bad_bpm(bpm):
    assert parspec.lag_seconds(4, bpm) == pytest.approx(4 * 60.0 / parspec.DEFAULT_BPM)


def test_lag_seconds_never_negative():
    assert parspec.lag_seconds(-4, 145) == 0.0


# -- the specs really can drive TD's append API -----------------------------


def test_specs_can_build_every_par_on_a_stub_comp(specs, by_name):
    comp = td_stub.build_pars(td_stub.Comp("director"), specs)
    assert sorted(comp.par.names()) == sorted(s.name for s in specs)
    assert comp.par.Scene.menuNames == list(by_name["Scene"].menu_names)
    assert comp.par.Feedback.min == 0
    assert comp.par.Feedback.max == 0.98
    assert comp.par.Heartbeatage.readOnly is True
    assert {page.name for page in comp.customPages} == {
        parspec.PAGE_DIRECTOR,
        parspec.PAGE_RUNTIME,
    }


def test_append_methods_are_all_real_page_methods(specs):
    page = td_stub.Comp("director").appendCustomPage("Director")
    for spec in specs:
        assert hasattr(page, spec.append_method), spec.name


# -- schema loading ---------------------------------------------------------


def test_load_default_schema_finds_the_repo_schema(schema):
    assert parspec.load_default_schema() == schema


def test_load_default_schema_accepts_an_explicit_path(tmp_path, schema):
    path = tmp_path / "other.json"
    path.write_text(json.dumps(schema), encoding="utf-8")
    assert parspec.load_default_schema(path) == schema


def test_unmappable_schema_type_is_a_loud_error():
    with pytest.raises(ValueError):
        parspec.pars_from_schema({"properties": {"weird": {"type": "array"}}})
