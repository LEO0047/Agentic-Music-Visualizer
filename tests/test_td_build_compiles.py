"""Static guarantees about td/ — it must compile and import without TD.

``build_network.py`` is the one file that genuinely needs TouchDesigner, so the
rule is: every TD-only name is touched from inside a function, and the auto-run
block at the bottom is guarded by ``try: op / except NameError``. That keeps the
whole folder importable here, which is what makes the other test modules
possible at all.
"""

from __future__ import annotations

import ast
import importlib
import py_compile
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TD_DIR = REPO / "td"
if str(TD_DIR) not in sys.path:
    sys.path.insert(0, str(TD_DIR))

TD_FILES = sorted(TD_DIR.glob("*.py"))
TD_ONLY_NAMES = {"op", "parent", "me", "ui", "project", "absTime", "mod", "parent"}


def test_td_folder_has_the_expected_modules():
    assert {path.name for path in TD_FILES} >= {
        "parspec.py",
        "osc_in_callbacks.py",
        "drop_executor.py",
        "build_network.py",
        "td_stub.py",
    }


@pytest.mark.parametrize("path", TD_FILES, ids=lambda p: p.name)
def test_every_td_file_compiles(path, tmp_path):
    py_compile.compile(str(path), cfile=str(tmp_path / (path.stem + ".pyc")), doraise=True)


@pytest.mark.parametrize(
    "module", ["parspec", "td_stub", "osc_in_callbacks", "drop_executor", "build_network"]
)
def test_every_td_module_imports_outside_touchdesigner(module):
    assert importlib.import_module(module) is not None


def _module_level_calls(tree: ast.Module) -> list[ast.Call]:
    """Every Call node that is not inside a function or lambda."""
    found: list[ast.Call] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):  # noqa: N802 - ast API
            return

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node):  # noqa: N802 - ast API
            return

        def visit_Call(self, node):  # noqa: N802 - ast API
            found.append(node)
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def test_build_network_only_touches_td_names_inside_functions():
    tree = ast.parse((TD_DIR / "build_network.py").read_text(encoding="utf-8"))
    offenders = [
        _call_name(call) for call in _module_level_calls(tree) if _call_name(call) in TD_ONLY_NAMES
    ]
    assert offenders == [], f"module-level TD calls in build_network.py: {offenders}"


def test_the_op_guard_is_present():
    source = (TD_DIR / "build_network.py").read_text(encoding="utf-8")
    assert "except NameError" in source, "the TD-only auto-run block must be guarded"


def test_build_network_exposes_the_expected_entry_points():
    build_network = importlib.import_module("build_network")
    for name in ("build", "find_td_dir", "summary", "LAG_EXPR"):
        assert hasattr(build_network, name)
    assert callable(build_network.build)


def test_the_lag_expression_matches_the_spec():
    """SPEC §3.3: lag seconds = beats × 60 / BPM, read off the director pars."""
    build_network = importlib.import_module("build_network")
    assert build_network.LAG_EXPR == (
        "op('director').par.Transitionbeats * 60 / op('director').par.Bpm"
    )


def test_find_td_dir_finds_this_repo():
    build_network = importlib.import_module("build_network")
    assert Path(build_network.find_td_dir()).resolve() == TD_DIR.resolve()


def test_find_td_dir_accepts_an_explicit_folder():
    build_network = importlib.import_module("build_network")
    assert Path(build_network.find_td_dir(str(TD_DIR))).resolve() == TD_DIR.resolve()


def test_find_td_dir_rejects_a_folder_without_parspec(tmp_path, monkeypatch):
    build_network = importlib.import_module("build_network")
    monkeypatch.setattr(build_network, "DEFAULT_TD_DIR", str(tmp_path))
    monkeypatch.delenv("AMV_TD_DIR", raising=False)
    monkeypatch.setattr(build_network, "__file__", str(tmp_path / "build_network.py"))
    with pytest.raises(RuntimeError):
        build_network.find_td_dir(str(tmp_path))


def test_building_without_touchdesigner_fails_loudly():
    """``build()`` must not silently pretend to work outside TD."""
    build_network = importlib.import_module("build_network")
    with pytest.raises(NameError):
        build_network.build(td_dir=str(TD_DIR))


# --------------------------------------------------------------------------
# driving the builders against td_stub
#
# ``build_network`` needs TouchDesigner for the *operators*, but its wiring is
# ordinary Python: which operator types it asks for, which parameters it sets
# and what it connects to what. Faking ``optype`` and recording the helper
# calls pins all three without TD.
# --------------------------------------------------------------------------


class Recorder:
    """A td_stub network plus every set_par / set_expr / connect call made."""

    def __init__(self, monkeypatch, missing=()):
        import build_network
        import td_stub

        self.build_network = build_network
        self.missing = set(missing)
        self.pars: list[tuple] = []
        self.exprs: list[tuple] = []
        self.connections: list[tuple] = []
        self.amv = td_stub.Comp("amv")
        del build_network.CREATED[:]
        del build_network.SKIPPED[:]
        monkeypatch.setattr(build_network, "optype", self._optype)
        monkeypatch.setattr(build_network, "set_par", self._set_par)
        monkeypatch.setattr(build_network, "set_expr", self._set_expr)
        monkeypatch.setattr(build_network, "connect", self._connect)

    def _optype(self, names):
        candidates = names.split() if isinstance(names, str) else list(names)
        for candidate in candidates:
            if candidate not in self.missing:
                return type(candidate, (), {})
        return None

    def _set_par(self, node, names, value):
        self.pars.append((getattr(node, "name", None), names, value))
        return node is not None

    def _set_expr(self, node, names, expression):
        self.exprs.append((getattr(node, "name", None), names, expression))
        return node is not None

    def _connect(self, source, target, index=0):
        self.connections.append(
            (getattr(source, "name", None), getattr(target, "name", None), index)
        )
        return source is not None and target is not None

    # -- queries ----------------------------------------------------------

    def node(self, name):
        return self.amv.children.get(name)

    def optype_of(self, name):
        node = self.node(name)
        return None if node is None else node.opType

    def par(self, name, candidates):
        for owner, names, value in self.pars:
            if owner == name and names == candidates:
                return value
        raise AssertionError(f"no set_par({name!r}, {candidates!r}, ...) was made")

    def par_names_for(self, name):
        return [names for owner, names, _ in self.pars if owner == name]

    def downstream_of(self, name):
        return [target for source, target, _ in self.connections if source == name]


@pytest.fixture
def recorder(monkeypatch):
    return Recorder(monkeypatch)


# -- finding 2: a Math CHOP cannot clamp; a Limit CHOP does -----------------

CLAMPED = ["bass_clamp", "mid_clamp", "high_clamp", "energy_clamp", "kick_clamp"]


@pytest.mark.parametrize("name", CLAMPED)
def test_every_normaliser_is_followed_by_a_limit_chop(recorder, name):
    recorder.build_network.build_audio(recorder.amv)
    assert recorder.optype_of(name) == "limitCHOP"
    assert recorder.par(name, "type") == "clamp"
    assert recorder.par(name, "min") == 0.0
    assert recorder.par(name, "max") == 1.0


def test_the_limit_chop_sits_between_the_normaliser_and_the_feature(recorder):
    recorder.build_network.build_audio(recorder.amv)
    assert recorder.downstream_of("bass_norm") == ["bass_clamp"]
    assert recorder.downstream_of("bass_clamp") == ["bass"]
    assert recorder.downstream_of("energy_norm") == ["energy_clamp"]
    assert recorder.downstream_of("kick_gate") == ["kick_clamp"]
    assert recorder.downstream_of("kick_clamp") == ["kick_logic"]


def test_nothing_asks_a_math_chop_to_clamp(recorder):
    """``postclamp`` is silently dropped by TD — it is not a Math CHOP par."""
    recorder.build_network.build_audio(recorder.amv)
    assert not [names for _, names, _ in recorder.pars if "clamp" in str(names)]
    assert "postclamp" not in (TD_DIR / "build_network.py").read_text(encoding="utf-8")


# -- finding 3: the 10 Hz feature rate comes from a Resample CHOP -----------


def test_a_resample_chop_sets_the_osc_send_rate(recorder):
    build_network = recorder.build_network
    build_network.build_audio(recorder.amv)
    assert recorder.optype_of("feat_rate") == "resampleCHOP"
    assert recorder.par("feat_rate", "rate") == build_network.FEATURE_RATE == 10
    assert recorder.downstream_of("feat_names") == ["feat_rate"]
    assert recorder.downstream_of("feat_rate") == ["osc_out"]


def test_the_osc_out_rate_attempt_is_kept_as_a_harmless_extra(recorder):
    recorder.build_network.build_audio(recorder.amv)
    assert recorder.par("osc_out", "rate samplerate") == recorder.build_network.FEATURE_RATE


# -- finding 1: Ramp TOP gradients live in a Table DAT ----------------------


def test_every_palette_gets_a_key_table_the_ramp_points_at(recorder):
    build_network = recorder.build_network
    build_network.build_palettes(recorder.amv)
    for name in build_network.PALETTES:
        keys = recorder.node("pal_%s_keys" % name)
        assert keys is not None, f"{name} has no key table"
        assert keys.opType == "tableDAT"
        assert keys.rows[0] == list(build_network.PALETTE_KEYS_HEADER)
        assert len(keys.rows) == 4, "header + three keys"
        assert recorder.par("pal_%s" % name, "dat") == "pal_%s_keys" % name


def test_the_key_rows_are_positions_and_colours_in_range(recorder):
    build_network = recorder.build_network
    build_network.build_palettes(recorder.amv)
    for name in build_network.PALETTES:
        rows = recorder.node("pal_%s_keys" % name).rows[1:]
        assert [row[0] for row in rows] == [0.0, 0.5, 1.0]
        for row in rows:
            assert len(row) == 5
            assert all(0.0 <= float(cell) <= 1.0 for cell in row)
            assert row[4] == 1.0


def test_no_ramp_top_is_given_per_key_parameters(recorder):
    """A Ramp TOP has no ``key1pos`` / ``key1color1``: that guess is what made
    all five palettes render as the same default gradient."""
    recorder.build_network.build_palettes(recorder.amv)
    assert not [names for _, names, _ in recorder.pars if "key" in str(names)]
    source = (TD_DIR / "build_network.py").read_text(encoding="utf-8")
    assert "key1pos" not in source and "key1color" not in source


def test_the_five_palettes_are_actually_different(recorder):
    build_network = recorder.build_network
    build_network.build_palettes(recorder.amv)
    gradients = {
        name: tuple(tuple(row) for row in recorder.node("pal_%s_keys" % name).rows[1:])
        for name in build_network.PALETTES
    }
    assert len(set(gradients.values())) == len(build_network.PALETTES)


def test_the_palette_names_are_the_schema_enum():
    import build_network
    import parspec

    specs = parspec.specs_by_name(parspec.pars_from_schema(parspec.load_default_schema()))
    assert list(build_network.PALETTES) == list(specs["Palette"].menu_names)


# -- finding 6: parameter-name candidates ----------------------------------


@pytest.mark.parametrize(
    ("node", "candidates"),
    [
        ("kick_logic", "convert preop"),
        ("audio_in", "rate samplerate"),
    ],
)
def test_the_reviewed_parameter_candidates_are_used(recorder, node, candidates):
    recorder.build_network.build_audio(recorder.amv)
    assert candidates in recorder.par_names_for(node)


def test_the_noise_and_execute_dat_candidates_are_used():
    source = (TD_DIR / "build_network.py").read_text(encoding="utf-8")
    assert '"harmon harmonics"' in source
    assert '"chops chop"' in source


# -- finding 7: a missing operator class must not abort the build -----------


def test_a_missing_operator_type_is_skipped_not_fatal(monkeypatch, capsys):
    rec = Recorder(monkeypatch, missing={"limitCHOP"})
    rec.build_network.build_audio(rec.amv)          # must not raise
    assert rec.node("bass_clamp") is None
    assert rec.node("osc_out") is not None, "the rest of the chain still got built"
    skipped = dict((name, tuple(c)) for name, c in rec.build_network.SKIPPED)
    assert skipped == {name: ("limitCHOP",) for name in CLAMPED}
    printed = capsys.readouterr().out
    assert "[amv] SKIP bass_clamp: none of limitCHOP exist in this TD build" in printed


def test_the_helpers_tolerate_a_none_placeholder(capsys):
    """set_par / set_expr / set_text / table_rows / connect all take ``None``."""
    import build_network
    import td_stub

    real = td_stub.Comp("real")
    assert build_network.set_par(None, "type", "clamp") is False
    assert build_network.set_expr(None, "index", "1") is False
    assert build_network.set_text(None, "x") is False
    assert build_network.table_rows(None, ("pos",), [[0.0]]) is False
    assert build_network.connect(None, real) is False
    assert build_network.connect(real, None) is False
    assert "Traceback" not in capsys.readouterr().out


def test_optype_returns_none_instead_of_raising():
    import build_network

    assert build_network.optype("nosuchTOP alsoNotATOP") is None
    assert build_network.optype("dict") is dict          # builtins are reachable


def test_the_summary_lists_what_it_could_not_create(monkeypatch, capsys):
    rec = Recorder(monkeypatch, missing={"resampleCHOP"})
    rec.build_network.build_audio(rec.amv)
    capsys.readouterr()
    rec.build_network.summary()
    printed = capsys.readouterr().out
    assert "SKIPPED 1 operators" in printed
    assert "feat_rate (tried resampleCHOP)" in printed


def test_a_complete_build_reports_no_skips(recorder, capsys):
    recorder.build_network.build_audio(recorder.amv)
    recorder.build_network.build_palettes(recorder.amv)
    assert recorder.build_network.SKIPPED == []
    recorder.build_network.summary()
    assert "no operator types were missing." in capsys.readouterr().out


def test_verify_markers_exist_for_the_reviewer():
    source = (TD_DIR / "build_network.py").read_text(encoding="utf-8")
    count = source.count("# VERIFY")
    assert count >= 15, f"only {count} # VERIFY markers; TD par names need flagging"


def test_readme_documents_the_verify_markers():
    readme = (TD_DIR / "README.md").read_text(encoding="utf-8")
    assert "VERIFY" in readme
    assert "TouchDesigner" in readme


@pytest.mark.parametrize(
    "phrase",
    [
        "pal_<name>_keys",       # finding 1: the ramp gradient is a table
        "Limit CHOP",            # finding 2: Math CHOP cannot clamp
        "Resample CHOP",         # finding 3: where 10 Hz comes from
        "flush_pending",         # finding 5: the 2 s ceiling on "next kick"
        "[amv] SKIP",            # finding 7: a missing operator type
        "heartbeat_watchdog",    # finding 8: the watchdog's first-call baseline
    ],
)
def test_the_readme_explains_the_reviewed_mechanisms(phrase):
    assert phrase in (TD_DIR / "README.md").read_text(encoding="utf-8")


def test_the_readme_no_longer_claims_ramp_tops_have_key_parameters():
    readme = (TD_DIR / "README.md").read_text(encoding="utf-8")
    assert "key1pos" not in readme
    assert "postclamp" not in readme.split("## `# VERIFY` 清單")[1]


@pytest.mark.parametrize("path", TD_FILES, ids=lambda p: p.name)
def test_no_tabs_and_no_trailing_whitespace(path):
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        assert "\t" not in line, f"{path.name}:{number} has a tab"
        assert line == line.rstrip(), f"{path.name}:{number} has trailing whitespace"


def test_callbacks_module_has_the_touchdesigner_entry_points():
    """The OSC In DAT looks up these names by convention; a rename breaks TD
    silently, so pin them here."""
    module = importlib.import_module("osc_in_callbacks")
    assert callable(module.onReceiveOSC)
    signature = module.onReceiveOSC.__code__.co_varnames[
        : module.onReceiveOSC.__code__.co_argcount
    ]
    assert signature == (
        "dat",
        "rowIndex",
        "message",
        "bytes",
        "timeStamp",
        "address",
        "args",
        "peer",
    )


def test_generated_dat_bodies_are_valid_python():
    """The Execute DAT texts this script writes must parse on their own."""
    build_network = importlib.import_module("build_network")
    for name in ("WATCHDOG_BODY", "KICK_EXEC_BODY", "PAR_EXEC_BODY"):
        ast.parse(getattr(build_network, name))
    bootstrap = build_network.bootstrap_code(str(TD_DIR), "drop_executor")
    ast.parse(bootstrap)
    for name in ("WATCHDOG_BODY", "KICK_EXEC_BODY"):
        ast.parse(bootstrap + getattr(build_network, name))


def test_the_callbacks_dat_text_still_parses_with_its_appended_path_bootstrap():
    """build_director appends a sys.path block after the module source; that
    must stay legal Python (``from __future__`` forbids a *prefix*)."""
    source = (TD_DIR / "osc_in_callbacks.py").read_text(encoding="utf-8")
    appended = source + (
        "\n\nimport sys as _sys\n_TD_DIR = %r\n"
        "if _TD_DIR not in _sys.path:\n    _sys.path.insert(0, _TD_DIR)\n" % str(TD_DIR)
    )
    ast.parse(appended)
