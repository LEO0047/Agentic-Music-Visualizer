"""Phase 5 — the projectM sidechain, checked without TouchDesigner.

projectM, Syphoner, OBS and the NDI runtime are all missing on this machine
(``uv run python tools/projectm_check.py`` says so), and TD is missing too, so
what can be pinned here is exactly what ``build_network.py`` *asks TD for*:
which operator classes, in which order, wired to what, driven by which
expression. That is the same trick ``tests/test_td_build_compiles.py`` plays,
and its ``Recorder`` harness is reused verbatim.

What this file deliberately cannot prove: that ``syphonspoutinTOP`` and
``ndiinTOP`` are the real class names, that ``fitmode`` is the real Fit TOP
parameter, or that a single frame of MilkDrop ever reaches the Composite TOP.
Those are the ``# VERIFY`` rows in ``td/README.md`` and the manual checklist in
``docs/phase5-projectm.md``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TD_DIR = REPO / "td"
if str(TD_DIR) not in sys.path:
    sys.path.insert(0, str(TD_DIR))

import parspec  # noqa: E402
import td_stub  # noqa: E402

from test_td_build_compiles import Recorder  # noqa: E402  (the shared harness)


@pytest.fixture
def recorder(monkeypatch):
    return Recorder(monkeypatch)


@pytest.fixture
def sidechain(recorder):
    """Just the projectM input selector."""
    recorder.build_network.build_projectm_input(recorder.amv)
    return recorder


@pytest.fixture
def whole_post(recorder):
    """The full post chain, so the composite wiring can be inspected too."""
    build_network = recorder.build_network
    scenes = build_network.build_scenes(recorder.amv)
    palette_switch, _ = build_network.build_palettes(recorder.amv)
    build_network.build_post(recorder.amv, scenes, palette_switch)
    return recorder


def inputs_of(recorder, name):
    """``[(source, index), ...]`` for every connection into *name*."""
    return sorted(
        [(source, index) for source, target, index in recorder.connections if target == name],
        key=lambda pair: pair[1],
    )


# -- the three capture paths exist, with the right operator classes ----------


@pytest.mark.parametrize(
    ("name", "optype"),
    [
        ("pm_syphon", "syphonspoutinTOP"),
        ("pm_ndi", "ndiinTOP"),
        ("pm_black", "constantTOP"),
        ("projectm_in", "switchTOP"),
    ],
)
def test_the_projectm_operators_have_the_right_types(sidechain, name, optype):
    assert sidechain.optype_of(name) == optype


def test_projectm_in_is_no_longer_a_null_top(sidechain):
    """Phase 2 left a Null TOP placeholder; Phase 5 must have replaced it."""
    assert sidechain.optype_of("projectm_in") != "nullTOP"


def test_the_switch_inputs_are_black_syphon_ndi_in_that_order(sidechain):
    """Input order *is* the menu order: none / syphon / ndi."""
    assert inputs_of(sidechain, "projectm_in") == [
        ("pm_black", 0),
        ("pm_syphon", 1),
        ("pm_ndi", 2),
    ]


def test_the_switch_index_comes_from_the_projectmsource_par(sidechain):
    expressions = [
        (names, expr) for owner, names, expr in sidechain.exprs if owner == "projectm_in"
    ]
    assert len(expressions) == 1, "the switch index is the switch's only expression"
    names, expression = expressions[0]
    assert names == "index"
    assert "Projectmsource" in expression
    assert expression == "op('director').par.Projectmsource.menuIndex % 3"


def test_the_index_wraps_so_a_longer_menu_cannot_dangle(sidechain):
    """``% len(PROJECTM_SOURCES)``, the same guard scene_switch uses."""
    build_network = sidechain.build_network
    expression = next(e for owner, _, e in sidechain.exprs if owner == "projectm_in")
    assert expression.endswith("%% %d" % len(build_network.PROJECTM_SOURCES))
    assert len(build_network.PROJECTM_SOURCES) == 3


# -- the fallback really is black and really is opaque ----------------------


def test_pm_black_is_opaque_black_at_the_canvas_size(sidechain):
    canvas = sidechain.build_network.PROJECTM_CANVAS
    assert sidechain.par("pm_black", "colorr color1r") == 0.0
    assert sidechain.par("pm_black", "colorg color1g") == 0.0
    assert sidechain.par("pm_black", "colorb color1b") == 0.0
    assert sidechain.par("pm_black", "alpha color1a") == 1.0
    assert sidechain.par("pm_black", "resolutionw") == canvas == 1280
    assert sidechain.par("pm_black", "resolutionh") == canvas


def test_the_capture_tops_are_pointed_at_the_projectm_names(sidechain):
    build_network = sidechain.build_network
    assert sidechain.par("pm_syphon", "sender sendername syphonsender") == (
        build_network.PROJECTM_SYPHON_SENDER
    )
    assert sidechain.par("pm_ndi", "name sourcename ndiname") == build_network.PROJECTM_NDI_SOURCE


# -- a missing plugin must not cost the build -------------------------------


@pytest.mark.parametrize(
    ("missing", "gone"),
    [({"syphonspoutinTOP"}, "pm_syphon"), ({"ndiinTOP"}, "pm_ndi")],
)
def test_a_missing_capture_plugin_is_skipped_not_fatal(monkeypatch, missing, gone):
    rec = Recorder(monkeypatch, missing=missing)
    rec.build_network.build_projectm_input(rec.amv)      # must not raise
    assert rec.node(gone) is None
    assert rec.node("projectm_in") is not None, "the switch still gets built"
    assert rec.node("pm_black") is not None, "the black fallback still gets built"
    assert [name for name, _ in rec.build_network.SKIPPED] == [gone]


def test_with_no_plugins_at_all_the_black_fallback_still_composites(monkeypatch):
    """Neither plugin installed: index 0 is still real, 1 and 2 are just holes.

    ``connect()`` is asked for all three either way — a ``None`` placeholder is
    logged and skipped, which is what keeps the switch itself intact.
    """
    rec = Recorder(monkeypatch, missing={"syphonspoutinTOP", "ndiinTOP"})
    rec.build_network.build_projectm_input(rec.amv)
    assert inputs_of(rec, "projectm_in") == [("pm_black", 0), (None, 1), (None, 2)]
    assert rec.node("projectm_in") is not None


# -- the chain into the composite -------------------------------------------


def test_a_fit_top_sits_between_the_switch_and_the_level(whole_post):
    assert whole_post.optype_of("pm_fit") == "fitTOP"
    assert whole_post.downstream_of("projectm_in") == ["pm_fit"]
    assert whole_post.downstream_of("pm_fit") == ["projectm_level"]


def test_the_fit_top_fills_the_canvas(whole_post):
    canvas = whole_post.build_network.PROJECTM_CANVAS
    assert whole_post.par("pm_fit", "fit fitmode") == "fill"
    assert whole_post.par("pm_fit", "resolutionw") == canvas
    assert whole_post.par("pm_fit", "resolutionh") == canvas


def test_projectm_in_still_feeds_the_composite(whole_post):
    assert whole_post.downstream_of("projectm_level") == ["composite"]
    assert inputs_of(whole_post, "composite") == [("fb_mix", 0), ("projectm_level", 1)]


def test_the_level_opacity_is_the_lagged_projectm_mix(whole_post):
    """SPEC §3.3: /director/projectm_mix → Composite opacity → Lag."""
    build_network = whole_post.build_network
    expression = next(e for owner, _, e in whole_post.exprs if owner == "projectm_level")
    assert expression == build_network.lagged("projectm_mix")
    assert expression == "op('lag_params')['projectm_mix']"
    assert ("projectm_mix", "Projectmmix") in build_network.LAGGED


def test_the_composite_operand_is_still_over(whole_post):
    assert whole_post.par("composite", "operand") == "over"


def test_the_whole_post_chain_skips_nothing(whole_post):
    assert whole_post.build_network.SKIPPED == []


# -- the Projectmsource custom par ------------------------------------------


@pytest.fixture(scope="module")
def specs():
    return parspec.pars_from_schema(parspec.load_default_schema())


@pytest.fixture(scope="module")
def director(specs):
    return td_stub.director_comp(specs)


def test_the_director_comp_carries_projectmsource(director):
    assert "Projectmsource" in director.par.names()
    par = director.par.Projectmsource
    assert par.style == "Menu"
    assert par.menuNames == ["none", "syphon", "ndi"]
    assert par.eval() == "none"


def test_the_menu_names_match_the_switch_input_order(specs):
    import build_network

    spec = parspec.specs_by_name(specs)["Projectmsource"]
    assert list(spec.menu_names) == list(parspec.PROJECTM_SOURCES)
    assert list(spec.menu_names) == list(build_network.PROJECTM_SOURCES)
    assert spec.menu_names[0] == "none", "index 0 must be the black fallback"


def test_projectmsource_defaults_to_none(specs):
    """A machine with no projectM running must still build a valid frame."""
    spec = parspec.specs_by_name(specs)["Projectmsource"]
    assert spec.default == "none"
    assert spec.coerce("syphon") == "syphon"
    with pytest.raises(ValueError):
        spec.coerce("spout")


def test_projectmsource_is_a_rig_par_not_a_director_par(specs):
    """SPEC §3.3 gives the director one projectM control: projectm_mix."""
    spec = parspec.specs_by_name(specs)["Projectmsource"]
    assert spec.page == parspec.PAGE_RUNTIME
    assert spec.address is None
    assert "/director/projectm_source" not in parspec.specs_by_address(specs)


def test_the_par_name_is_reachable_by_the_expression_the_switch_uses(director):
    """The Switch TOP evaluates ``op('director').par.Projectmsource.menuIndex``."""
    import build_network

    par = getattr(director.par, build_network.PROJECTM_SOURCE_PAR)
    assert par.menuIndex == 0
    par.val = "ndi"
    assert par.menuIndex == 2
    par.val = "none"


# -- documentation ----------------------------------------------------------


def test_the_new_guesses_are_flagged_for_the_reviewer():
    source = (TD_DIR / "build_network.py").read_text(encoding="utf-8")
    section = source.split("def build_projectm_input")[1]
    assert section.count("# VERIFY") >= 3


def test_the_td_readme_has_a_phase_5_section():
    readme = (TD_DIR / "README.md").read_text(encoding="utf-8")
    assert "Phase 5" in readme
    for phrase in ("pm_syphon", "pm_ndi", "pm_black", "pm_fit", "Projectmsource"):
        assert phrase in readme, phrase
    assert "docs/phase5-projectm.md" in readme


def test_the_phase_5_doc_exists_and_covers_both_capture_paths():
    doc = (REPO / "docs" / "phase5-projectm.md").read_text(encoding="utf-8")
    for phrase in ("BlackHole", "Syphoner", "OBS", "NDI", "Projectmsource", "projectm_mix"):
        assert phrase in doc, phrase
    assert "Metal" in doc and "OpenGL" in doc, "the V3 deferral reason must be written down"
