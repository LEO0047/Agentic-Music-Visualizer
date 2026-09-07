"""Build the whole `/project1/amv` reflex-layer network inside TouchDesigner.

Run it from the TD Textport::

    exec(open('/Users/leohuang/Repos/Agentic-Music-Visualizer/td/build_network.py').read())

or, if the folder is already on ``sys.path``::

    import build_network; build_network.build()

Idempotent: it destroys ``/project1/amv`` if it exists and rebuilds it from
scratch, so re-running after an edit is the normal workflow.

What it builds (SPEC §1, §3, Phase 2 細節)::

    /project1/amv
      audio_in ─ spectrum ─┬─ bass_trim ─ bass_avg ─ bass_lag ─ bass_norm ─ bass_clamp ─┐
                           ├─ mid_*                                                     │
                           └─ high_*                                                    ├─ features
      audio_in ─ energy_rms ─ energy_lag ─ energy_norm ─ energy_clamp ──────────────────┤    │
      bass ─ kick_slope ─ kick_gate ─ kick_clamp ─ kick_logic ─ kick ───────────────────┤    │
      centroid (placeholder, Phase 3 Script CHOP) ──────────────────────────────────────┘    │
                                       feat_names ─ feat_rate (10 Hz) ─ osc_out ─────────────┘
      director            custom pars from director_schema.json
        osc_in            OSC In DAT, port 9001
        osc_in_callbacks  text of td/osc_in_callbacks.py
        on_drop_dat       Text DAT holding the pending on_drop JSON
        watchdog          Execute DAT → drop_executor.heartbeat_watchdog
        kick_exec         CHOP Execute DAT on kick → drop_executor.on_kick
        par_exec          Parameter Execute DAT → manual-mode freeze
        midi_exec         CHOP Execute DAT on ../midi_in → midi_override
      midi_in             MIDI In CHOP, channel 1 (Phase 6 override)
      dir_vals ─ lag_params        float targets, lagged beats*60/BPM
      tunnel_* / ft_* / pf_*  ─ scene_switch ─ palette ─ fb_mix ─ composite ─ out ─ window
                                                                    ↑         └─ record
      pm_black / pm_syphon / pm_ndi ─ projectm_in ─ pm_fit ─ projectm_level

Nothing in this file runs outside TouchDesigner: every TD name (``op``,
``baseCOMP``, …) is referenced from inside a function, and the auto-run block
at the bottom is guarded by ``try: op / except NameError``. That is what makes
``tests/test_td_build_compiles.py`` able to import it.

**This script was written on a machine with no TouchDesigner installed. It has
never been executed inside TD.** Every parameter name marked ``# VERIFY`` is a
best guess from the TD documentation and must be checked on first run; the
helpers below log and continue instead of raising, so a wrong name costs you a
warning line, not the build. The same is true of *operator class* names: a
class this build does not have is logged as ``[amv] SKIP ...``, leaves a
``None`` placeholder that every helper tolerates, and is listed again by
:func:`summary` — one bad guess never leaves a half-built network.
"""

import os
import sys
import traceback

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

PROJECT = "/project1"
ROOT_NAME = "amv"

AUDIO_DEVICE = "BlackHole 2ch"   # SPEC Phase 1
SAMPLE_RATE = 48000

OSC_OUT_HOST = "127.0.0.1"
OSC_OUT_PORT = 9000              # TD → sidecar, SPEC §3.1
OSC_IN_PORT = 9001               # sidecar → TD, SPEC §3.3
FEATURE_RATE = 10                # Hz, SPEC §3.1

BANDS = (
    ("bass", 20.0, 150.0),
    ("mid", 150.0, 2000.0),
    ("high", 2000.0, 16000.0),
)
BAND_LAG = 0.05                  # seconds, SPEC §3.1
ENERGY_LAG = 1.0                 # seconds, SPEC §3.1
KICK_THRESHOLD = 0.25            # bass slope above this counts as a kick

OSC_CHANNEL_PREFIX = "feat"      # channel 'feat/bass' → address '/feat/bass'
FEATURE_CHANNELS = ("bass", "mid", "high", "energy", "kick", "centroid")

#: ``{name: ((pos, (r, g, b)), ...)}`` — three keys per palette (low / mid /
#: high), positions 0–1 and colours 0–1, in ``director_schema.json`` enum order.
#: A Ramp TOP has **no per-key parameters**: its gradient lives in a Table DAT
#: with a ``pos r g b a`` header, one row per key, pointed at by the ramp's
#: ``dat`` parameter. :func:`build_palettes` writes one such DAT per palette.
#: Three keys rather than two is what makes the five gradients read as
#: different palettes rather than five two-stop fades.
PALETTES = {
    "violet_cyan": (
        (0.0, (0.05, 0.01, 0.14)),
        (0.5, (0.45, 0.08, 0.78)),
        (1.0, (0.20, 0.95, 0.98)),
    ),
    "acid_lime": (
        (0.0, (0.02, 0.06, 0.01)),
        (0.5, (0.25, 0.62, 0.05)),
        (1.0, (0.82, 1.00, 0.16)),
    ),
    "amber_dusk": (
        (0.0, (0.10, 0.02, 0.06)),
        (0.5, (0.72, 0.20, 0.05)),
        (1.0, (1.00, 0.78, 0.30)),
    ),
    "mono_white": (
        (0.0, (0.00, 0.00, 0.00)),
        (0.5, (0.45, 0.45, 0.47)),
        (1.0, (1.00, 1.00, 1.00)),
    ),
    "infrared": (
        (0.0, (0.03, 0.00, 0.05)),
        (0.5, (0.62, 0.02, 0.12)),
        (1.0, (1.00, 0.72, 0.10)),
    ),
}

PALETTE_KEYS_HEADER = ("pos", "r", "g", "b", "a")
"""Header row of a Ramp TOP's key table. TD's own default DAT uses these."""

#: Scenes this build actually implements, in ``director_schema.json`` enum
#: order. ``kaleido_mesh`` and ``projectm_blend`` are Phase 5/6; the Switch TOP
#: index wraps with ``%`` so a menu index of 3 or 4 can never dangle.
IMPLEMENTED_SCENES = ("fractal_temple", "tunnel", "particle_field")

DEFAULT_TD_DIR = "/Users/leohuang/Repos/Agentic-Music-Visualizer/td"

RECORD_FILE = "amv_$(YYYY)$(MM)$(DD)_$(HH)$(mm)$(SS).mov"

CREATED = []
SKIPPED = []
"""``[(name, [candidate types])]`` for every operator this build could not create."""


# --------------------------------------------------------------------------
# defensive TD helpers
# --------------------------------------------------------------------------


def log(message):
    """One prefixed line to the Textport."""
    print("[amv build] " + str(message))


def _candidates(names):
    return names.split() if isinstance(names, str) else list(names)


def optype(names):
    """Return the first TD operator class that exists, or ``None``.

    Operator class names differ between TD builds (``audiodevinCHOP`` vs
    ``audiodeviceinCHOP``), so callers pass a space-separated list of
    candidates, best guess first. Returning ``None`` rather than raising is
    deliberate: see :func:`create`.
    """
    import builtins

    modules = []
    try:
        import td as td_module

        modules.append(td_module)
    except Exception:
        pass
    modules.append(builtins)
    for candidate in _candidates(names):
        for module in modules:
            found = getattr(module, candidate, None)
            if found is not None:
                return found
    return None


def create(parent_comp, type_names, name, x=0, y=0):
    """Create a child operator and record it for the summary.

    A class name this TD build does not have costs one log line and a ``None``
    placeholder, not the build: :func:`set_par`, :func:`set_expr`,
    :func:`set_text` and :func:`connect` all tolerate ``None``. One wrong guess
    out of ~60 operators must never leave a half-built network behind — the
    skipped names are listed again by :func:`summary`.
    """
    candidates = _candidates(type_names)
    kind = optype(candidates)
    if kind is None:
        SKIPPED.append((name, candidates))
        print("[amv] SKIP %s: none of %s exist in this TD build" % (name, " ".join(candidates)))
        return None
    try:
        node = parent_comp.create(kind, name)
    except Exception as exc:
        SKIPPED.append((name, candidates))
        print("[amv] SKIP %s: create(%s) failed: %s" % (name, candidates[0], exc))
        return None
    try:
        node.nodeX, node.nodeY = int(x), int(y)
    except Exception:
        pass
    CREATED.append(node.path)
    return node


def set_par(node, names, value):
    """Set a parameter by the first candidate name that exists.

    Logs and continues when no candidate exists — a TD version difference must
    not abort a 60-operator build halfway through.
    """
    candidates = _candidates(names)
    if node is None:
        log("WARN skipped par [%s] = %r: its operator was not created" % (" ".join(candidates), value))
        return False
    for candidate in candidates:
        par = getattr(node.par, candidate, None)
        if par is None:
            continue
        try:
            par.val = value
            return True
        except Exception as exc:
            log("WARN %s.%s = %r failed: %s" % (node.path, candidate, value, exc))
            return False
    log("WARN %s has no par [%s]; skipped (value %r)" % (node.path, " ".join(candidates), value))
    return False


def set_expr(node, names, expression):
    """Put a parameter into expression mode."""
    candidates = _candidates(names)
    if node is None:
        log("WARN skipped expr [%s]: its operator was not created" % " ".join(candidates))
        return False
    for candidate in candidates:
        par = getattr(node.par, candidate, None)
        if par is None:
            continue
        try:
            par.expr = str(expression)
            return True
        except Exception as exc:
            log("WARN %s.%s expr failed: %s" % (node.path, candidate, exc))
            return False
    log("WARN %s has no par [%s]; skipped (expr %r)" % (node.path, " ".join(candidates), expression))
    return False


def set_attr(par, name, value):
    """Set an attribute on a Par object (menuNames, min, readOnly, ...)."""
    try:
        setattr(par, name, value)
        return True
    except Exception as exc:
        log("WARN cannot set %s.%s = %r: %s" % (getattr(par, "name", "?"), name, value, exc))
        return False


def set_text(node, text):
    """Fill a Text/Execute DAT, tolerating a placeholder from :func:`create`."""
    if node is None:
        log("WARN skipped DAT text: its operator was not created")
        return False
    try:
        node.text = text
        return True
    except Exception as exc:
        log("WARN cannot set text on %s: %s" % (getattr(node, "path", node), exc))
        return False


def table_rows(node, header, rows):
    """Rewrite a Table DAT as *header* plus *rows* (used for the ramp keys)."""
    if node is None:
        log("WARN skipped table rows: its operator was not created")
        return False
    try:
        node.clear()
        node.appendRow(list(header))
        for row in rows:
            node.appendRow(list(row))
        return True
    except Exception as exc:
        log("WARN cannot fill the table %s: %s" % (getattr(node, "path", node), exc))
        return False


def connect(source, target, index=0):
    """Wire ``source`` into ``target``'s input *index*."""
    if source is None or target is None:
        log("WARN skipped a connection: %s -> %s[%d] (an operator was not created)"
            % (getattr(source, "path", source), getattr(target, "path", target), index))
        return False
    try:
        target.inputConnectors[index].connect(source)
        return True
    except Exception as exc:
        log("WARN cannot connect %s -> %s[%d]: %s" % (source.path, target.path, index, exc))
        return False


def first_par(par_group):
    """TD's ``appendXxx`` returns a ParGroup; this takes its single Par."""
    try:
        return par_group[0]
    except Exception:
        return par_group


# --------------------------------------------------------------------------
# locating this repo from inside TD
# --------------------------------------------------------------------------


def find_td_dir(explicit=None):
    """Locate the ``td/`` folder holding parspec/osc_in_callbacks/drop_executor.

    ``exec(open(...).read())`` gives the script no ``__file__``, so the order
    is: explicit argument, ``$AMV_TD_DIR``, a module global ``AMV_TD_DIR`` (set
    it in the Textport before exec'ing), ``__file__`` when imported normally,
    then :data:`DEFAULT_TD_DIR`.
    """
    candidates = [explicit, os.environ.get("AMV_TD_DIR"), globals().get("AMV_TD_DIR")]
    if "__file__" in globals():
        candidates.append(os.path.dirname(os.path.abspath(globals()["__file__"])))
    candidates.append(DEFAULT_TD_DIR)
    for candidate in candidates:
        if candidate and os.path.isfile(os.path.join(str(candidate), "parspec.py")):
            return os.path.abspath(str(candidate))
    raise RuntimeError(
        "cannot find the repo's td/ folder; set AMV_TD_DIR or call build(td_dir='...'). Tried: "
        + ", ".join(repr(c) for c in candidates)
    )


def import_parspec(td_dir):
    """Put ``td_dir`` on ``sys.path`` and import :mod:`parspec` from it."""
    if td_dir not in sys.path:
        sys.path.insert(0, td_dir)
    import parspec

    try:
        import importlib

        importlib.reload(parspec)  # pick up edits without restarting TD
    except Exception:
        pass
    return parspec


def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def bootstrap_code(td_dir, module):
    """Header for a generated DAT so it can import from ``td/`` after a restart."""
    return (
        "# generated by td/build_network.py - edit the repo file, not this DAT\n"
        "import sys\n"
        "_TD_DIR = %r\n"
        "if _TD_DIR not in sys.path:\n"
        "    sys.path.insert(0, _TD_DIR)\n"
        "import %s\n\n" % (td_dir, module)
    )


# --------------------------------------------------------------------------
# audio → features (SPEC §3.1)
# --------------------------------------------------------------------------


def build_audio(amv):
    """Audio Device In → spectrum → bass/mid/high/energy/kick → OSC Out."""
    feats = {}

    audio_in = create(amv, "audiodevinCHOP audiodeviceinCHOP", "audio_in", -1050, 400)
    set_par(audio_in, "device", AUDIO_DEVICE)        # VERIFY par name and that the
    set_par(audio_in, "rate samplerate", SAMPLE_RATE)  # VERIFY device menu accepts a plain string
    set_par(audio_in, "active", True)
    feats["audio_in"] = audio_in

    spectrum = create(amv, "audiospectrumCHOP", "spectrum", -900, 400)
    connect(audio_in, spectrum)
    feats["spectrum"] = spectrum

    merge_inputs = []
    for index, (name, low, high) in enumerate(BANDS):
        y = 600 - index * 150
        # Select CHOP selects *channels*; picking a frequency range means
        # picking a sample range, which is the Trim CHOP's job. SPEC's "Select"
        # is read as "select the band" rather than as an operator name.
        trim = create(amv, "trimCHOP", "%s_trim" % name, -700, y)
        connect(spectrum, trim)
        # Trim has independent units for each endpoint (2025.33230 docs).
        set_par(trim, "relative", "abs")
        set_par(trim, "startunit", "samples")
        set_par(trim, "endunit", "samples")
        set_expr(trim, "start startsample", _bin_expr(low))
        set_expr(trim, "end endsample", _bin_expr(high))

        avg = create(amv, "analyzeCHOP", "%s_avg" % name, -550, y)
        connect(trim, avg)
        set_par(avg, "function", "average")  # VERIFY menu value ('average' vs 'avg')

        lag = create(amv, "lagCHOP", "%s_lag" % name, -400, y)
        connect(avg, lag)
        set_par(lag, "lag1", BAND_LAG)
        set_par(lag, "lag2", BAND_LAG)

        norm = create(amv, "mathCHOP", "%s_norm" % name, -250, y)
        connect(lag, norm)
        _normalise(norm, 0.0, 0.25 if name == "bass" else 0.15)

        clamp = _clamp01(amv, norm, "%s_clamp" % name, -100, y)

        rename = create(amv, "renameCHOP", name, 50, y)
        connect(clamp, rename)
        set_par(rename, "renamefrom", "*")
        set_par(rename, "renameto", name)  # VERIFY renamefrom/renameto pattern pars
        feats[name] = rename
        merge_inputs.append(rename)

    # energy: RMS over the raw audio, 1 s window (SPEC §3.1)
    energy_rms = create(amv, "analyzeCHOP", "energy_rms", -700, 150)
    connect(audio_in, energy_rms)
    set_par(energy_rms, "function", "rmspower")
    energy_lag = create(amv, "lagCHOP", "energy_lag", -550, 150)
    connect(energy_rms, energy_lag)
    set_par(energy_lag, "lag1", ENERGY_LAG)
    set_par(energy_lag, "lag2", ENERGY_LAG)
    energy_norm = create(amv, "mathCHOP", "energy_norm", -400, 150)
    connect(energy_lag, energy_norm)
    _normalise(energy_norm, 0.0, 0.2)
    energy_clamp = _clamp01(amv, energy_norm, "energy_clamp", -250, 150)
    energy = create(amv, "renameCHOP", "energy", -100, 150)
    connect(energy_clamp, energy)
    set_par(energy, "renamefrom", "*")
    set_par(energy, "renameto", "energy")
    feats["energy"] = energy
    merge_inputs.append(energy)

    # kick: slope of the bass envelope over a threshold, as a 0/1 pulse
    kick_slope = create(amv, "slopeCHOP", "kick_slope", -400, 0)
    connect(feats["bass"], kick_slope)
    kick_gate = create(amv, "mathCHOP", "kick_gate", -250, 0)
    connect(kick_slope, kick_gate)
    # A steep range map plus the Limit CHOP below turns "> threshold" into a
    # hard 0/1 step, so the Logic CHOP only has to convert non-zero to on.
    set_par(kick_gate, "fromrange1", KICK_THRESHOLD)
    set_par(kick_gate, "fromrange2", KICK_THRESHOLD + 0.001)
    set_par(kick_gate, "torange1", 0.0)
    set_par(kick_gate, "torange2", 1.0)
    kick_clamp = _clamp01(amv, kick_gate, "kick_clamp", -100, 0)
    kick_logic = create(amv, "logicCHOP", "kick_logic", 50, 0)
    connect(kick_clamp, kick_logic)
    set_par(kick_logic, "convert preop", "offwhenzero")  # VERIFY Logic CHOP convert-input menu
    kick = create(amv, "renameCHOP", "kick", 200, 0)
    connect(kick_logic, kick)
    set_par(kick, "renamefrom", "*")
    set_par(kick, "renameto", "kick")
    feats["kick"] = kick
    merge_inputs.append(kick)

    # centroid: Phase 3 replaces this with a Script CHOP doing a numpy spectral
    # centroid (SPEC §3.1 marks it optional). A constant keeps the OSC contract
    # complete so the sidecar never has to special-case a missing channel.
    centroid = create(amv, "constantCHOP", "centroid", 50, -150)
    set_par(centroid, "const0name name0", "centroid")  # VERIFY const0name vs name0
    set_par(centroid, "const0value value0", 0.0)
    feats["centroid"] = centroid
    merge_inputs.append(centroid)

    features = create(amv, "mergeCHOP", "features", 200, 200)
    for index, node in enumerate(merge_inputs):
        connect(node, features, index)
    feats["features"] = features

    # OSC Out CHOP sends one message per channel and uses the channel name as
    # the OSC address, so a channel called 'feat/bass' is sent to '/feat/bass'.
    # VERIFY: if this TD build rejects '/' inside a channel name, drop the
    # rename and set the OSC Out CHOP's address/prefix par to '/feat' instead.
    feat_names = create(amv, "renameCHOP", "feat_names", 350, 200)
    connect(features, feat_names)
    set_par(feat_names, "renamefrom", " ".join(FEATURE_CHANNELS))
    set_par(feat_names, "renameto", " ".join("%s/%s" % (OSC_CHANNEL_PREFIX, c) for c in FEATURE_CHANNELS))

    # SPEC §3.1 wants 10 Hz. An OSC Out CHOP sends whenever it *cooks*, which
    # is every frame (60 Hz), and it has no dependable "send rate" parameter —
    # so the rate has to come from upstream. A Resample CHOP at 10 Hz makes the
    # OSC Out cook 10 times a second, which is the actual mechanism.
    feat_rate = create(amv, "resampleCHOP", "feat_rate", 500, 200)
    connect(feat_names, feat_rate)
    set_par(feat_rate, "rate", FEATURE_RATE)
    set_par(feat_rate, "method", "linear")  # VERIFY interpolation menu value spelling
    feats["feat_rate"] = feat_rate

    osc_out = create(amv, "oscoutCHOP", "osc_out", 650, 200)
    connect(feat_rate, osc_out)
    set_par(osc_out, "netaddress address", OSC_OUT_HOST)  # VERIFY netaddress vs address
    set_par(osc_out, "port", OSC_OUT_PORT)
    # Harmless extra: if this build *does* have a rate par it agrees with the
    # Resample CHOP; if it does not, one WARN line and the resample still rules.
    set_par(osc_out, "rate samplerate", FEATURE_RATE)     # VERIFY send-rate par name
    set_par(osc_out, "active", True)
    feats["osc_out"] = osc_out
    return feats


def _bin_expr(frequency):
    """Spectrum sample index for *frequency*, as a TD expression string.

    The Audio Spectrum CHOP spans 0 .. rate/2 over its sample count, so the
    index is a simple proportion. Written as an expression so it stays correct
    if the FFT size or the device rate changes.
    """
    return "%r / (%d / 2.0) * op('spectrum').numSamples" % (float(frequency), SAMPLE_RATE)


def _normalise(math_chop, low, high):
    """Map [low, high] onto [0, 1] (SPEC's "Math 正規化").

    The Math CHOP maps but does **not** clamp — it has no clamp parameter at
    all — so every normaliser is followed by :func:`_clamp01`.
    """
    set_par(math_chop, "fromrange1", low)
    set_par(math_chop, "fromrange2", high)
    set_par(math_chop, "torange1", 0.0)
    set_par(math_chop, "torange2", 1.0)


def _clamp01(amv, source, name, x, y):
    """Limit CHOP clamping *source* into 0–1 (SPEC §3.1's "範圍 0–1")."""
    limit = create(amv, "limitCHOP", name, x, y)
    connect(source, limit)
    set_par(limit, "type", "clamp")  # VERIFY Limit CHOP type menu value spelling
    set_par(limit, "min", 0.0)
    set_par(limit, "max", 1.0)
    return limit


# --------------------------------------------------------------------------
# director COMP
# --------------------------------------------------------------------------


def build_director(amv, specs, td_dir):
    """The Base COMP carrying every custom parameter, plus its OSC plumbing."""
    director = create(amv, "baseCOMP", "director", 300, 600)
    pages = {}
    for spec in specs:
        page = pages.get(spec.page)
        if page is None:
            page = director.appendCustomPage(spec.page)
            pages[spec.page] = page
        try:
            par = first_par(getattr(page, spec.append_method)(spec.name, label=spec.label))
        except Exception as exc:
            log("WARN could not append %s (%s): %s" % (spec.name, spec.style, exc))
            continue
        if spec.style == "Menu":
            set_attr(par, "menuNames", list(spec.menu_names))
            set_attr(par, "menuLabels", list(spec.menu_labels))
        if spec.min is not None:
            set_attr(par, "min", spec.min)
            set_attr(par, "normMin", spec.min)
            set_attr(par, "clampMin", bool(spec.clamp))
        if spec.max is not None:
            set_attr(par, "max", spec.max)
            set_attr(par, "normMax", spec.max)
            set_attr(par, "clampMax", bool(spec.clamp))
        if spec.readonly:
            set_attr(par, "readOnly", True)
        if spec.help:
            set_attr(par, "help", spec.help)
        if spec.default is not None:
            set_attr(par, "default", spec.default)
            set_attr(par, "val", spec.default)
    log("director: %d custom pars on pages %s" % (len(specs), sorted(pages)))

    # --- OSC In DAT + callbacks -------------------------------------------
    callbacks = create(director, "textDAT", "osc_in_callbacks", 0, 0)
    set_text(callbacks, read_text(os.path.join(td_dir, "osc_in_callbacks.py")) + (
        "\n\n# --- appended by td/build_network.py so the DAT can import parspec\n"
        "import sys as _sys\n"
        "_TD_DIR = %r\n"
        "if _TD_DIR not in _sys.path:\n"
        "    _sys.path.insert(0, _TD_DIR)\n" % td_dir
    ))

    osc_in = create(director, "oscinDAT", "osc_in", 0, 200)
    set_par(osc_in, "port", OSC_IN_PORT)
    set_par(osc_in, "active", True)
    set_par(osc_in, "callbacks", "osc_in_callbacks")  # VERIFY callbacks par name/format
    set_par(osc_in, "clear", True)                    # VERIFY 'clear each frame' toggle

    on_drop = create(director, "textDAT", "on_drop_dat", 0, -200)
    set_text(on_drop, "")

    watchdog = create(director, "executeDAT", "watchdog", 250, 0)
    set_text(watchdog, bootstrap_code(td_dir, "drop_executor") + WATCHDOG_BODY)
    set_par(watchdog, "framestart", True)  # VERIFY Execute DAT callback toggles

    kick_exec = create(director, "chopexecuteDAT", "kick_exec", 250, -200)
    set_text(kick_exec, bootstrap_code(td_dir, "drop_executor") + KICK_EXEC_BODY)
    set_par(kick_exec, "chops chop", "../kick")   # VERIFY CHOP Execute source par
    set_par(kick_exec, "offtoon", True)
    set_par(kick_exec, "valuechange", False)

    par_exec = create(director, "parameterexecuteDAT", "par_exec", 250, 200)
    set_text(par_exec, bootstrap_code(td_dir, "osc_in_callbacks") + PAR_EXEC_BODY)
    set_par(par_exec, "op ops", ".")        # VERIFY Parameter Execute source par
    set_par(par_exec, "pars", "*")
    set_par(par_exec, "valuechange", True)

    # SPEC Phase 6: the MIDI controller. Built here rather than in build(),
    # because it belongs to the same cluster of Execute DATs as the watchdog
    # and par_exec above — and it only works if par_exec exists.
    build_midi_override(amv, td_dir)
    return director


WATCHDOG_BODY = '''
WARN_EVERY = 5.0
_last_warn = [0.0]


def onFrameStart(frame):
    if frame % 60:
        return
    comp = me.parent()
    # A breakdown can run for bars with no kick at all; SPEC 3.3's "next kick"
    # must not mean "never", so anything waiting longer than 2 s lands here.
    drop_executor.flush_pending(comp)
    if drop_executor.heartbeat_watchdog(comp, None):
        import time
        now = time.time()
        if now - _last_warn[0] > WARN_EVERY:
            _last_warn[0] = now
            print("[amv] director silent for %.0fs - holding current state"
                  % comp.par.Heartbeatage.eval())
    return
'''

KICK_EXEC_BODY = '''
def offToOn(channel, sampleIndex, val, prev):
    drop_executor.on_kick(me.parent())
    return
'''

PAR_EXEC_BODY = '''
def onValueChange(par, prev):
    # Must go through the module-level handler: it claims the director's own
    # script writes (echo marker) so only a human touch starts the 30 s freeze.
    osc_in_callbacks.onValueChange(par, prev)
    return
'''


# --------------------------------------------------------------------------
# MIDI override (SPEC §6 Phase 6)
# --------------------------------------------------------------------------

MIDI_DEVICE = 1
"""Row of TD's **MIDI Device Mapper** (Dialogs → MIDI Device Mapper) to listen
on, *not* the controller's name. Row 1 is the first mapped device; open the
mapper once, map the controller to a row, and put that row number here."""

MIDI_IN_CHANNEL = 1
"""MIDI channel to accept. Matches ``midi_override.MIDI_CHANNEL``, which is
what makes the CHOP channel names come out as ``ch1c*`` / ``ch1n*``."""


def build_midi_override(amv, td_dir=None):
    """A MIDI In CHOP and the CHOP Execute DAT that turns it into par writes.

    SPEC §6 Phase 6: *"MIDI 覆寫：一台小控制器接 TD，任何參數你手動一動就凍結該
    欄位 30 秒"*.

    Two operators, and the interesting part is what is *missing* between them::

        midi_in (MIDI In CHOP, channel 1)      ← at the amv level, like audio_in
          └ midi_exec (CHOP Execute DAT)       ← inside director, like kick_exec
                onValueChange → midi_override.on_cc / on_note

    ``midi_override.on_cc`` writes the custom parameter **without** the
    ``script_write`` echo marker every other writer leaves behind (td/README.md
    依賴的 TD 行為 #6). So ``par_exec`` — the Parameter Execute DAT built just
    above — sees an unclaimed change, calls ``note_touch``, and SPEC §5's 30 s
    freeze starts on that field exactly as if the knob had been the mouse.
    There is no MIDI-specific freeze code anywhere: turning a knob *is* a
    manual touch.

    ``midi_in`` sits next to ``audio_in`` at the ``amv`` level because it is a
    device input; ``midi_exec`` sits inside ``director`` next to ``kick_exec``
    because that is where the parameters are, which is why its source CHOP is
    the relative path ``../midi_in`` (same shape as ``kick_exec``'s
    ``../kick``).

    Nothing here is fatal: no controller plugged in means a MIDI In CHOP with
    no channels, no ``onValueChange`` and a show that behaves exactly as it did
    in Phase 5. *td_dir* is only needed for the generated DAT's ``sys.path``
    header; it is resolved the usual way when the caller does not pass one.
    """
    td_dir = find_td_dir(td_dir)
    midi_in = create(amv, "midiinCHOP", "midi_in", 0, 340)  # VERIFY operator class name
    # VERIFY: the MIDI In CHOP takes a *device index* (its Device par is a
    # menu backed by the MIDI Device Mapper), not a device name string. If this
    # build spells the par 'id' instead, the second candidate catches it.
    set_par(midi_in, "device id", MIDI_DEVICE)          # VERIFY par name + index vs name
    set_par(midi_in, "channel channels", MIDI_IN_CHANNEL)  # VERIFY channel filter par
    set_par(midi_in, "active", True)

    director = amv.op("director")
    if director is None:  # pragma: no cover - build_director always runs first
        log("WARN no director COMP yet; midi_exec goes on %s instead" % getattr(amv, "path", amv))
    parent_comp = amv if director is None else director
    source_chop = "midi_in" if director is None else "../midi_in"

    midi_exec = create(parent_comp, "chopexecuteDAT chopexecDAT", "midi_exec", 250, 400)
    set_text(midi_exec, bootstrap_code(td_dir, "midi_override") + MIDI_EXEC_BODY)
    set_par(midi_exec, "chops chop", source_chop)  # VERIFY CHOP Execute source par
    # A CC knob and a note pad both arrive as a channel whose value changed, so
    # one callback covers both and Off→On would only double-fire the notes.
    set_par(midi_exec, "valuechange", True)   # VERIFY CHOP Execute callback toggles
    set_par(midi_exec, "offtoon", False)      # VERIFY
    return midi_in, midi_exec


MIDI_EXEC_BODY = '''
def _director():
    """This DAT lives inside the director COMP, so me.parent() is it; the
    op('director') fallback is there for when someone drags the DAT out."""
    comp = me.parent()
    if comp is not None and hasattr(comp.par, 'Scene'):
        return comp
    return op('director')


def onValueChange(channel, sampleIndex, val, prev):
    kind, number = midi_override.parse_channel(channel.name)
    if kind == 'cc':
        midi_override.on_cc(_director(), number, val)
    elif kind == 'note':
        midi_override.on_note(_director(), number, val)
    return
'''


# --------------------------------------------------------------------------
# lagged float targets (SPEC §3.3: lag seconds = beats * 60 / BPM)
# --------------------------------------------------------------------------

LAG_EXPR = "op('director').par.Transitionbeats * 60 / op('director').par.Bpm"

LAGGED = (
    ("feedback", "Feedback"),
    ("camera_speed", "Cameraspeed"),
    ("projectm_mix", "Projectmmix"),
)


def build_lag(amv):
    """Constant CHOP of director targets → Lag CHOP glide over N beats."""
    dir_vals = create(amv, "constantCHOP", "dir_vals", 500, 600)
    for index, (channel, parname) in enumerate(LAGGED):
        set_par(dir_vals, "const%dname name%d" % (index, index), channel)
        set_expr(dir_vals, "const%dvalue value%d" % (index, index), "op('director').par.%s" % parname)

    lag_params = create(amv, "lagCHOP", "lag_params", 650, 600)
    connect(dir_vals, lag_params)
    set_expr(lag_params, "lag1", LAG_EXPR)
    set_expr(lag_params, "lag2", LAG_EXPR)
    return lag_params


def feat(channel):
    """Expression reading one feature channel, for use in TOP parameters."""
    return "op('features')['%s']" % channel


def lagged(channel):
    """Expression reading one lagged director target."""
    return "op('lag_params')['%s']" % channel


# --------------------------------------------------------------------------
# scenes
# --------------------------------------------------------------------------


def build_scenes(amv):
    """Three scenes from stock operators, all driven by bass/mid/high.

    Deliberately cheap: Phase 2's acceptance is "60 fps and the parameters
    glide", not final artwork. Each scene is a TOP chain ending in a Null TOP
    so the Switch TOP inputs stay stable while the insides are reworked.
    """
    scenes = {}

    # --- tunnel: radial ramp displaced by noise, transformed by camera speed
    ramp = create(amv, "rampTOP", "tunnel_ramp", -400, 1200)
    set_par(ramp, "type", "radial")  # VERIFY ramp type menu value
    set_par(ramp, "resolutionw", 1280)
    set_par(ramp, "resolutionh", 1280)
    noise = create(amv, "noiseTOP", "tunnel_noise", -400, 1050)
    set_par(noise, "resolutionw", 1280)
    set_par(noise, "resolutionh", 1280)
    set_par(noise, "type", "sparse")  # VERIFY noise type menu value
    set_expr(noise, "period", "0.4 + %s * 1.5" % feat("bass"))
    set_expr(noise, "translatez tz", "absTime.seconds * (0.05 + %s)" % lagged("camera_speed"))
    disp = create(amv, "displaceTOP", "tunnel_disp", -250, 1150)
    connect(ramp, disp, 0)
    connect(noise, disp, 1)
    set_expr(disp, "uvweightx displaceweightx", "0.05 + %s * 0.3" % feat("mid"))  # VERIFY
    set_expr(disp, "uvweighty displaceweighty", "0.05 + %s * 0.3" % feat("mid"))  # VERIFY
    xform = create(amv, "transformTOP", "tunnel_xform", -100, 1150)
    connect(disp, xform)
    set_expr(xform, "rotate", "absTime.seconds * 20 * (0.1 + %s)" % lagged("camera_speed"))
    set_expr(xform, "scale scalex", "1 + %s * 0.4" % feat("bass"))
    tunnel = create(amv, "nullTOP", "tunnel", 50, 1150)
    connect(xform, tunnel)
    scenes["tunnel"] = tunnel

    # --- fractal_temple: fractal noise mirrored by a Transform TOP + feedback
    ft_noise = create(amv, "noiseTOP", "ft_noise", -400, 900)
    set_par(ft_noise, "resolutionw", 1280)
    set_par(ft_noise, "resolutionh", 1280)
    set_par(ft_noise, "type", "sparse")
    set_par(ft_noise, "harmon harmonics", 5)   # VERIFY harmonics/gain par names
    set_expr(ft_noise, "gain", "0.6 + %s * 0.8" % feat("high"))
    set_expr(ft_noise, "period", "0.2 + %s" % feat("mid"))
    set_expr(ft_noise, "translatex tx", "absTime.seconds * 0.05")
    ft_mirror = create(amv, "transformTOP", "ft_mirror", -250, 900)
    connect(ft_noise, ft_mirror)
    # Transform TOP "Extend" set to mirror is the stock kaleidoscope trick:
    # scaling past the edge folds the image back on itself.
    for extend in ("extendleft", "extendright", "extendtop", "extendbottom"):
        set_par(ft_mirror, extend, "mirror")  # VERIFY extend menu value spelling
    set_expr(ft_mirror, "scale scalex", "1.0 + op('director').par.Symmetry / 8.0")
    set_expr(ft_mirror, "rotate", "op('director').par.Symmetry * 11.25")
    ft_level = create(amv, "levelTOP", "ft_level", -100, 900)
    connect(ft_mirror, ft_level)
    set_expr(ft_level, "brightness1 brightness", "0.7 + %s * 0.6" % feat("bass"))
    fractal_temple = create(amv, "nullTOP", "fractal_temple", 50, 900)
    connect(ft_level, fractal_temple)
    scenes["fractal_temple"] = fractal_temple

    # --- particle_field: noise → threshold → blur (a stand-in for a real
    #     Particle GPU system; Phase 6 replaces it, particle_mode selects the
    #     force preset there).
    pf_noise = create(amv, "noiseTOP", "pf_noise", -400, 750)
    set_par(pf_noise, "resolutionw", 1280)
    set_par(pf_noise, "resolutionh", 1280)
    set_par(pf_noise, "type", "random")  # VERIFY noise type menu value
    set_expr(pf_noise, "seed", "op('director').par.Particlemode.menuIndex")
    set_expr(pf_noise, "period", "0.02 + %s * 0.1" % feat("high"))
    pf_thresh = create(amv, "thresholdTOP", "pf_thresh", -250, 750)
    connect(pf_noise, pf_thresh)
    set_expr(pf_thresh, "threshold", "0.9 - %s * 0.35" % feat("mid"))  # VERIFY threshold par
    pf_blur = create(amv, "blurTOP", "pf_blur", -100, 750)
    connect(pf_thresh, pf_blur)
    set_expr(pf_blur, "size filtersize", "1 + %s * 24" % feat("bass"))  # VERIFY blur size par
    particle_field = create(amv, "nullTOP", "particle_field", 50, 750)
    connect(pf_blur, particle_field)
    scenes["particle_field"] = particle_field
    return scenes


def palette_keys_name(name):
    """Name of the Table DAT holding one palette's gradient keys."""
    return "pal_%s_keys" % name


def build_palettes(amv):
    """One Ramp TOP + one key Table DAT per palette, chosen by ``Palette``.

    A Ramp TOP has no per-key parameters. Its gradient is a *table*: a DAT with
    a ``pos r g b a`` header and one row per key, referenced by the ramp's
    ``dat`` parameter (TD's own default is an internal ``<ramp>_keys`` DAT, and
    this build simply supplies its own next to each ramp).
    """
    ramps = []
    palette_names = list(PALETTES)
    for index, name in enumerate(palette_names):
        y = 1300 - index * 100
        keys = create(amv, "tableDAT", palette_keys_name(name), 50, y)
        table_rows(keys, PALETTE_KEYS_HEADER, _ramp_rows(PALETTES[name]))

        ramp = create(amv, "rampTOP", "pal_%s" % name, 200, y)
        set_par(ramp, "type", "horizontal")  # VERIFY ramp type menu value
        set_par(ramp, "resolutionw", 256)
        set_par(ramp, "resolutionh", 2)
        set_par(ramp, "dat", palette_keys_name(name))
        ramps.append(ramp)
    switch = create(amv, "switchTOP", "palette_switch", 380, 1250)
    for index, ramp in enumerate(ramps):
        connect(ramp, switch, index)
    set_expr(switch, "index", "op('director').par.Palette.menuIndex %% %d" % len(ramps))
    return switch, palette_names


def _ramp_rows(keys):
    """``((pos, (r, g, b)), ...)`` → the Ramp TOP key table's data rows."""
    return [[pos, rgb[0], rgb[1], rgb[2], 1.0] for pos, rgb in keys]


# --------------------------------------------------------------------------
# Phase 5: the projectM sidechain
# --------------------------------------------------------------------------

#: Switch TOP input order for ``projectm_in``, and the menu names of the
#: ``Projectmsource`` director par. ``parspec.PROJECTM_SOURCES`` is the source
#: of truth; this copy exists because ``build_projectm_input`` only needs the
#: *count* and must keep working if the parspec import ever fails.
PROJECTM_SOURCES = ("none", "syphon", "ndi")

PROJECTM_SOURCE_PAR = "Projectmsource"

PROJECTM_SYPHON_SENDER = "projectM"
"""Syphon server name Syphoner publishes the projectM window under."""

PROJECTM_NDI_SOURCE = "projectM"
"""NDI source name OBS's NDI output announces (rename the OBS output to match)."""

PROJECTM_CANVAS = 1280
"""Non-Commercial TD tops out at 1280×1280; the Fit TOP normalises to this."""


def build_projectm_input(amv):
    """Three possible projectM capture paths → one Switch TOP (SPEC Phase 5).

    projectM runs as a *separate application* (audio in from BlackHole, exactly
    like TD), so its frames have to be carried across process boundaries. SPEC
    Phase 5 細節 names two ways and this builds both, side by side, so switching
    between them at showtime is one menu click and not a rebuild:

    * ``pm_syphon`` — Syphon Spout In TOP, fed by Syphoner pointed at the
      projectM window. ≈ 1 frame (SPEC §7), zero-copy on the GPU.
    * ``pm_ndi`` — NDI In TOP, fed by OBS window-capturing projectM with the
      NDI output plugin. 2–4 frames, but the most robust: OBS will happily
      keep sending when projectM is on another Space or another machine.
    * ``pm_black`` — a black Constant TOP. The *default*, and the reason
      nothing here is fatal: with no projectM running at all, ``projectm_in``
      still cooks a valid 1280² frame and the Composite TOP downstream stays
      well defined whatever ``Projectmmix`` says.

    Neither capture TOP exists on a machine without its plugin, so both go
    through :func:`create`'s tolerant path: a missing class costs one
    ``[amv] SKIP`` line and leaves the Switch input unconnected, never a
    half-built network.

    The index comes from the ``Projectmsource`` custom par (Runtime page), not
    from the director: SPEC §3.3 gives the director exactly one projectM
    control, ``/director/projectm_mix``. Which cable the pixels arrive on is a
    property of *this rig*, decided by the human who set it up.
    """
    pm_syphon = create(amv, "syphonspoutinTOP", "pm_syphon", 520, 700)
    # Syphon Spout In TOP: on macOS the mode is always Syphon (Spout is
    # Windows-only), so only the server name needs setting.
    set_par(pm_syphon, "sender sendername syphonsender", PROJECTM_SYPHON_SENDER)  # VERIFY

    pm_ndi = create(amv, "ndiinTOP", "pm_ndi", 520, 560)
    set_par(pm_ndi, "name sourcename ndiname", PROJECTM_NDI_SOURCE)  # VERIFY

    pm_black = create(amv, "constantTOP", "pm_black", 520, 840)
    set_par(pm_black, "colorr color1r", 0.0)  # VERIFY Constant TOP colour par names
    set_par(pm_black, "colorg color1g", 0.0)
    set_par(pm_black, "colorb color1b", 0.0)
    set_par(pm_black, "alpha color1a", 1.0)
    set_par(pm_black, "resolutionw", PROJECTM_CANVAS)
    set_par(pm_black, "resolutionh", PROJECTM_CANVAS)

    projectm_in = create(amv, "switchTOP", "projectm_in", 700, 700)
    for index, source in enumerate((pm_black, pm_syphon, pm_ndi)):
        connect(source, projectm_in, index)
    # ``% len`` for the same reason scene_switch does it: a menu that grows
    # before this function does must wrap, not dangle on a missing input.
    set_expr(
        projectm_in,
        "index",
        "op('director').par.%s.menuIndex %% %d" % (PROJECTM_SOURCE_PAR, len(PROJECTM_SOURCES)),
    )
    return projectm_in


# --------------------------------------------------------------------------
# post chain
# --------------------------------------------------------------------------


def build_post(amv, scenes, palette_switch):
    """switch → lookup → feedback → composite → out → window / record."""
    scene_switch = create(amv, "switchTOP", "scene_switch", 200, 1000)
    for index, name in enumerate(IMPLEMENTED_SCENES):
        connect(scenes[name], scene_switch, index)
    # Scene menu order is the schema enum order; kaleido_mesh (3) and
    # projectm_blend (4) are not built yet, so wrap rather than dangle.
    set_expr(
        scene_switch,
        "index",
        "op('director').par.Scene.menuIndex %% %d" % len(IMPLEMENTED_SCENES),
    )

    palette = create(amv, "lookupTOP", "palette", 550, 1000)
    connect(scene_switch, palette, 0)      # index image
    connect(palette_switch, palette, 1)    # lookup table  # VERIFY input order

    fb_mix = create(amv, "crossTOP", "fb_mix", 850, 1000)
    feedback = create(amv, "feedbackTOP", "feedback", 700, 900)
    fb_xform = create(amv, "transformTOP", "fb_xform", 850, 880)
    connect(feedback, fb_xform)
    set_expr(fb_xform, "scale scalex", "1.01 + %s * 0.04" % lagged("camera_speed"))
    set_expr(fb_xform, "rotate", "%s * 0.5" % lagged("camera_speed"))
    connect(palette, fb_mix, 0)
    connect(fb_xform, fb_mix, 1)
    set_expr(fb_mix, "cross", lagged("feedback"))
    set_par(feedback, "top", "fb_mix")  # VERIFY Feedback TOP target par name

    # --- Phase 5: the projectM sidechain ----------------------------------
    projectm_in = build_projectm_input(amv)

    # projectM renders at whatever its own window is; the Fit TOP is what makes
    # a 640×480 SDL window and a full-screen capture both land on the 1280²
    # canvas instead of compositing as a small rectangle in one corner.
    pm_fit = create(amv, "fitTOP", "pm_fit", 860, 700)
    connect(projectm_in, pm_fit)
    set_par(pm_fit, "fit fitmode", "fill")  # VERIFY Fit TOP fit-mode menu value
    set_par(pm_fit, "outputresolution resolutionmenu", "custom")  # VERIFY
    set_par(pm_fit, "resolutionw", PROJECTM_CANVAS)
    set_par(pm_fit, "resolutionh", PROJECTM_CANVAS)

    projectm_level = create(amv, "levelTOP", "projectm_level", 1000, 700)
    connect(pm_fit, projectm_level)
    set_expr(projectm_level, "opacity", lagged("projectm_mix"))  # VERIFY opacity par

    composite = create(amv, "compositeTOP", "composite", 1150, 1000)
    connect(fb_mix, composite, 0)
    connect(projectm_level, composite, 1)
    set_par(composite, "operand", "over")  # VERIFY operand menu value

    out = create(amv, "nullTOP", "out", 1300, 1000)
    connect(composite, out)

    window = create(amv, "windowCOMP", "window", 1450, 1100)
    set_par(window, "op winop optop", "out")  # VERIFY Window COMP operator par
    set_par(window, "winw", 1280)
    set_par(window, "winh", 1280)

    record = create(amv, "moviefileoutTOP", "record", 1450, 900)
    connect(out, record)
    set_par(record, "file moviefile", RECORD_FILE)  # VERIFY output file par
    set_expr(record, "record", "op('director').par.Record")  # VERIFY record par
    return out


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def build(td_dir=None, schema_path=None):
    """Destroy and rebuild ``/project1/amv``. Returns the new Base COMP."""
    del CREATED[:]
    del SKIPPED[:]
    td_dir = find_td_dir(td_dir)
    parspec = import_parspec(td_dir)
    schema = parspec.load_default_schema(schema_path)
    specs = parspec.pars_from_schema(schema)
    log("td dir %s, %d pars from the schema" % (td_dir, len(specs)))

    root = op(PROJECT)
    if root is None:
        raise RuntimeError("%s does not exist; open a project first" % PROJECT)
    existing = root.op(ROOT_NAME)
    if existing is not None:
        log("destroying the existing %s" % existing.path)
        existing.destroy()

    base = optype("baseCOMP")
    if base is None:  # pragma: no cover - no TD build lacks baseCOMP
        raise RuntimeError("this TD build has no baseCOMP; nothing can be created")
    amv = root.create(base, ROOT_NAME)
    CREATED.append(amv.path)
    try:
        amv.nodeX, amv.nodeY = 0, 0
    except Exception:
        pass

    build_audio(amv)
    build_director(amv, specs, td_dir)
    build_lag(amv)
    scenes = build_scenes(amv)
    palette_switch, palettes = build_palettes(amv)
    build_post(amv, scenes, palette_switch)

    log("palettes: %s" % ", ".join(palettes))
    log("scenes wired to the Switch TOP: %s" % ", ".join(IMPLEMENTED_SCENES))
    summary()
    return amv


def summary():
    """Print every operator this run created, and every one it could not."""
    log("created %d operators:" % len(CREATED))
    for path in CREATED:
        print("    " + path)
    if SKIPPED:
        log("SKIPPED %d operators - this network is incomplete:" % len(SKIPPED))
        for name, candidates in SKIPPED:
            print("    %s (tried %s)" % (name, " ".join(candidates)))
    else:
        log("no operator types were missing.")
    log("done. Next: SPEC Phase 2 acceptance - drag the director custom pars "
        "and check 60 fps with a visible glide (see td/README.md).")


try:  # pragma: no cover - only true inside TouchDesigner
    op  # noqa: B018 - a bare reference: defined by TD, absent everywhere else
except NameError:
    pass
else:
    try:
        build()
    except Exception:
        traceback.print_exc()
