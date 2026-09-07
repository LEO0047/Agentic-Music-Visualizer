"""MIDI override — a knob on a controller beats the director for 30 s.

SPEC §6 Phase 6: *"MIDI 覆寫：一台小控制器接 TD，任何參數你手動一動就凍結該欄位
30 秒"*, which is SPEC §5's freeze rule reached through hardware instead of
through the Parameter dialog.

The whole trick is what this module deliberately **does not** do
--------------------------------------------------------------
``osc_in_callbacks.write_par`` and ``drop_executor._write`` both leave a
single-use "this write was mine, and its value is X" marker
(:func:`osc_in_callbacks.note_script_write`) before touching a parameter, so
the Parameter Execute DAT's ``onValueChange`` can tell a script echo from a
hand on the fader (td/README.md 依賴的 TD 行為 #6).

:func:`on_cc` writes the parameter **without** that marker. That is the entire
mechanism: TD fires ``onValueChange`` exactly as it would for a mouse drag,
``osc_in_callbacks.claim_script_write`` finds nothing to claim, ``note_touch``
runs, and the parameter is frozen for ``FREEZE_SECONDS`` — so the director's
next ``/director/<that field>`` is skipped, in ``gpt`` and ``rule`` alike,
while every other field keeps updating. No new freeze bookkeeping is added
here, and none of it is duplicated: a knob is a human, and the human path
already existed.

Signal path inside TD (built by ``build_network.build_midi_override``)::

    midi_in   MIDI In CHOP, channel 1, one CHOP channel per CC
      └ midi_exec  CHOP Execute DAT (inside the director COMP)
            onValueChange(channel, ...) → parse_channel(channel.name)
                                        → on_cc / on_note

Two conscious departures from how OSC writes behave:

* **No waiting for the next kick.** ``osc_in_callbacks`` parks ``Scene`` /
  ``Palette`` / ``Symmetry`` / ``Particlemode`` in ``pending_discrete`` until a
  kick (SPEC §3.3). A hand on a knob is not a director decision on a musical
  grid — it is someone reacting *now* — so :func:`on_cc` writes straight
  through. Turning the Scene knob cuts the scene on that frame.
* **No Lag.** The float pars go to the par directly; the Lag CHOP downstream
  (``lag_params``) still smooths what the visuals see, exactly as it does when
  a human drags the slider in the Parameter dialog.

Nothing here imports TouchDesigner, so pytest exercises the same code the DAT
runs (``tests/test_td_midi.py``).
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

__all__ = [
    "CC_MAX",
    "MIDI_CHANNEL",
    "MIDI_STORE_KEY",
    "MODE_PAR",
    "DEFAULT_CC_MAP",
    "DEFAULT_NOTE_MAP",
    "cc_to_par_value",
    "midi_channel_name",
    "note_channel_name",
    "parse_channel",
    "on_cc",
    "on_note",
    "describe_map",
]

CC_MAX = 127.0
"""A MIDI controller value is 7 bits: 0–127 inclusive."""

MIDI_CHANNEL = 1
"""The MIDI channel ``build_network`` sets on the MIDI In CHOP."""

MODE_PAR = "Mode"
"""Runtime page par the note buttons drive (SPEC §5: gpt / rule / manual)."""

MIDI_STORE_KEY = "midi_touch"
"""``comp.store`` key holding ``{par: {'value':…, 't':…}}`` — diagnostics only.

The freeze itself is *not* recorded here: it lives in
``osc_in_callbacks``'s ``manual_touch`` store, written by the Parameter Execute
DAT. This is a breadcrumb trail for "did the controller actually reach TD?",
which is the first question when a knob does nothing.
"""

DEFAULT_CC_MAP: dict[int, str] = {
    1: "Feedback",       # CC 1 is the modulation wheel on essentially every
    2: "Cameraspeed",    # controller, so the most-reached-for knob drives the
    3: "Projectmmix",    # most-reached-for parameter.
    4: "Symmetry",
    5: "Scene",
    6: "Palette",
    7: "Particlemode",
}
"""CC number → director custom parameter name.

Seven knobs, in the order a small controller (nanoKONTROL, Launch Control,
MIDI Fighter Twister…) usually lays them out: the three continuous ones first,
then the discrete ones. ``Ondrop``, ``Intent``, ``Heartbeat`` and
``Heartbeatage`` are absent on purpose — a knob cannot usefully write JSON or
prose, and the heartbeat is show state rather than a decision.
"""

DEFAULT_NOTE_MAP: dict[int, str] = {
    60: "gpt",       # middle C
    61: "rule",
    62: "manual",
}
"""Note number → ``Mode`` value (SPEC §5's 熱鍵切換, as three pads)."""

_CHANNEL_RE = re.compile(r"^ch(\d+)([cn])(\d+)$", re.IGNORECASE)
"""``ch1c1`` (channel 1, controller 1) / ``ch1n60`` (channel 1, note 60).

# VERIFY: the MIDI In CHOP's channel naming. TD names one CHOP channel per
# incoming message type, and the documented pattern is ``ch<midi channel><c|n>
# <number>``. If this build spells it differently (``c1``, ``ctrl1``,
# ``ch1cc1``…), fix the regex and :func:`midi_channel_name` together — they are
# the only two places that know the spelling, and
# ``tests/test_td_midi.py::test_the_channel_name_round_trips`` keeps them
# agreeing with each other.
"""


# --------------------------------------------------------------------------
# plumbing (kept identical in shape to osc_in_callbacks / drop_executor)
# --------------------------------------------------------------------------


def _log(message: str) -> None:
    print("[amv midi] " + str(message))


def _now() -> float:
    """Same clock as ``osc_in_callbacks`` and ``drop_executor`` (td/README.md)."""
    return time.time()


def _par(comp: Any, name: str) -> Any:
    if comp is None:
        return None
    try:
        return getattr(comp.par, name)
    except AttributeError:
        return None
    except Exception:  # pragma: no cover - defensive: TD par collections vary
        return None


_SPECS: dict[str, Any] | None = None
_SPECS_TRIED = False


def _specs() -> dict[str, Any]:
    """``{'Feedback': ParSpec(...)}`` from the schema, or ``{}``.

    Imported the same tolerant way ``osc_in_callbacks`` does it: inside TD the
    generated DAT puts ``td/`` on ``sys.path`` first, under pytest the test
    does, and a failure here is not fatal — :func:`cc_to_par_value` reads the
    range off the TD parameter itself instead.
    """
    global _SPECS, _SPECS_TRIED
    if _SPECS is not None or _SPECS_TRIED:
        return _SPECS or {}
    _SPECS_TRIED = True
    try:
        import parspec

        _SPECS = parspec.specs_by_name(parspec.pars_from_schema(parspec.load_default_schema()))
    except Exception as exc:  # pragma: no cover - only without parspec on sys.path
        _log(f"parspec not importable ({exc}); reading ranges off the TD pars instead")
        _SPECS = {}
    return _SPECS or {}


def _spec_for(name: str, par: Any = None) -> Any:
    """The schema spec for *name*, falling back to the live TD parameter."""
    return _specs().get(str(name)) or par


# --------------------------------------------------------------------------
# 0–127 → a parameter value
# --------------------------------------------------------------------------


def _menu_names(spec: Any) -> list:
    """Menu values off either a :class:`parspec.ParSpec` or a TD ``Par``."""
    for attribute in ("menu_names", "menuNames"):
        names = getattr(spec, attribute, None)
        if names:
            return list(names)
    return []


def _bound(spec: Any, name: str, default: float) -> float:
    value = getattr(spec, name, None)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return default


def cc_to_par_value(spec: Any, cc_value_0_127: Any) -> Any:
    """Map a 0–127 controller value onto what *spec*'s parameter accepts.

    *spec* is a :class:`parspec.ParSpec` when the schema is importable and a
    live TD ``Par`` otherwise; both carry the four things this needs (style,
    menu names, min, max) under one of two spellings.

    Three shapes, one per parameter style:

    * **Menu** (``Scene``, ``Palette``, ``Particlemode``) — the 128 values are
      cut into ``len(menu)`` equal buckets and the *menu name* for that bucket
      comes back, ready to assign to the par. Equal buckets, rather than
      ``value / 127 * (n - 1)`` rounded, so every entry gets the same amount of
      knob travel — with five scenes that is ~25 CC steps each instead of two
      half-width buckets at the ends.
    * **Int** / **Toggle** (``Symmetry``) — the same linear ramp across
      ``[min, max]``, rounded. ``Symmetry`` therefore sweeps 1 → 16 across the
      knob, and a Toggle (no bounds → 0–1) flips at CC 64.
    * **Float** (``Feedback``, ``Cameraspeed``, ``Projectmmix``) — linear
      across ``[min, max]``, so the knob's ceiling is the *schema's* ceiling:
      full clockwise on ``Feedback`` is 0.98, not 1.0.

    A ``Str`` parameter (``Ondrop``, ``Intent``) raises ``ValueError``: a knob
    has nothing meaningful to say there, and silently writing ``"0.42"`` into
    the on_drop JSON would be worse than refusing.
    """
    try:
        value = float(cc_value_0_127)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{cc_value_0_127!r} is not a MIDI value") from exc
    if value != value:  # NaN
        raise ValueError("a MIDI value cannot be NaN")
    value = max(0.0, min(CC_MAX, value))

    names = _menu_names(spec)
    if names:
        index = int(value * len(names) / (CC_MAX + 1.0))
        return names[min(index, len(names) - 1)]

    style = str(getattr(spec, "style", "Float"))
    if style == "Str":
        raise ValueError("a MIDI CC cannot drive a string parameter")

    low = _bound(spec, "min", 0.0)
    high = _bound(spec, "max", 1.0)
    scaled = low + (high - low) * (value / CC_MAX)
    if style in ("Int", "Toggle"):
        return int(round(scaled))
    return float(scaled)


# --------------------------------------------------------------------------
# CHOP channel names
# --------------------------------------------------------------------------


def midi_channel_name(cc: Any, channel: int = MIDI_CHANNEL) -> str:
    """The MIDI In CHOP channel carrying controller *cc* — ``ch1c1``.

    # VERIFY: see :data:`_CHANNEL_RE`. This is the one spelling
    ``build_network`` and the tests both quote, so changing it here is the
    whole fix if TD names its channels differently.
    """
    return "ch%dc%d" % (int(channel), int(cc))


def note_channel_name(note: Any, channel: int = MIDI_CHANNEL) -> str:
    """The MIDI In CHOP channel carrying note *note* — ``ch1n60``.  # VERIFY"""
    return "ch%dn%d" % (int(channel), int(note))


def parse_channel(name: Any) -> tuple:
    """``'ch1c4'`` → ``('cc', 4)``; ``'ch1n60'`` → ``('note', 60)``.

    Anything else — a pitch-bend channel, a renamed channel, ``None`` — comes
    back as ``(None, None)`` so the DAT callback can ignore it without a
    branch. The MIDI channel digit is parsed but not enforced: the CHOP is
    configured for channel 1, and refusing a message that reached us anyway
    would only make a mis-set controller silently dead.
    """
    match = _CHANNEL_RE.match(str(name or "").strip())
    if match is None:
        return (None, None)
    kind = "cc" if match.group(2).lower() == "c" else "note"
    return (kind, int(match.group(3)))


# --------------------------------------------------------------------------
# the two entry points the CHOP Execute DAT calls
# --------------------------------------------------------------------------


def _note_midi(comp: Any, parname: str, value: Any, now: Any = None) -> None:
    """Breadcrumb: this controller move reached this parameter, at this time."""
    stamp = _now() if now is None else float(now)
    try:
        marks = dict(comp.fetch(MIDI_STORE_KEY, {}) or {})
        marks[str(parname)] = {"value": value, "t": stamp}
        comp.store(MIDI_STORE_KEY, marks)
    except Exception:  # pragma: no cover - defensive
        return


def on_cc(
    comp: Any,
    cc: Any,
    value: Any,
    now: Any = None,
    cc_map: dict | None = None,
    log: Callable[[str], None] = _log,
) -> str | None:
    """Apply one control change. Returns the parameter name, or ``None``.

    **This writes the parameter as a human would**, with no
    ``note_script_write`` marker in front of it — that omission is the freeze
    (see the module docstring). Do not "fix" it by marking the write: the
    director would then keep overwriting the knob, and SPEC §6 Phase 6 would
    be a no-op.

    Unknown CC numbers, parameters this build does not have and values the
    parameter refuses all cost one log line and ``None``; nothing raises,
    because this runs inside a per-frame TD callback.
    """
    mapping = DEFAULT_CC_MAP if cc_map is None else cc_map
    try:
        number = int(cc)
    except (TypeError, ValueError):
        log(f"CC {cc!r} is not a number; ignored")
        return None
    name = mapping.get(number)
    if name is None:
        log(f"CC {number} is not mapped to a parameter; ignored")
        return None
    par = _par(comp, name)
    if par is None:
        log(f"no custom par named {name!r} on {getattr(comp, 'name', comp)!r}; ignored")
        return None
    try:
        coerced = cc_to_par_value(_spec_for(name, par), value)
    except Exception as exc:
        log(f"CC {number} → {name}: cannot use {value!r} ({exc})")
        return None
    try:
        # No note_script_write() here. See the module docstring.
        par.val = coerced
    except Exception as exc:
        log(f"{name}: cannot accept {coerced!r} ({exc})")
        return None
    _note_midi(comp, name, coerced, now)
    return name


def on_note(
    comp: Any,
    note: Any,
    velocity: Any = 127,
    now: Any = None,
    note_map: dict | None = None,
    log: Callable[[str], None] = _log,
) -> str | None:
    """Apply one note-on: switch ``Mode``. Returns the new mode, or ``None``.

    Note-*off* (velocity 0) is ignored, so a pad press switches the mode once
    rather than switching it and then re-switching it on release.

    Switching mode is itself a parameter write with no script marker, so
    ``Mode`` freezes for 30 s too. That costs nothing — no OSC address writes
    ``Mode`` (see ``parspec._runtime_specs``) — and it means the pads behave
    like every other control on the box.
    """
    mapping = DEFAULT_NOTE_MAP if note_map is None else note_map
    try:
        number = int(note)
    except (TypeError, ValueError):
        log(f"note {note!r} is not a number; ignored")
        return None
    mode = mapping.get(number)
    if mode is None:
        log(f"note {number} is not mapped to a mode; ignored")
        return None
    try:
        struck = float(velocity)
    except (TypeError, ValueError):
        struck = 0.0
    if struck <= 0.0:
        return None
    par = _par(comp, MODE_PAR)
    if par is None:
        log(f"no {MODE_PAR!r} par on {getattr(comp, 'name', comp)!r}; ignored")
        return None
    names = _menu_names(par) or _menu_names(_spec_for(MODE_PAR))
    if names and mode not in names:
        log(f"{mode!r} is not one of {names}; ignored")
        return None
    try:
        par.val = mode
    except Exception as exc:  # pragma: no cover - defensive
        log(f"{MODE_PAR}: cannot accept {mode!r} ({exc})")
        return None
    _note_midi(comp, MODE_PAR, mode, now)
    log(f"mode → {mode}")
    return mode


# --------------------------------------------------------------------------
# documentation
# --------------------------------------------------------------------------


def describe_map(cc_map: dict | None = None, note_map: dict | None = None) -> str:
    """The current mapping as text, for td/README.md and the Textport.

    ``print(midi_override.describe_map())`` in the Textport is the fastest way
    to answer "which knob is which" without opening this file.
    """
    mapping = DEFAULT_CC_MAP if cc_map is None else cc_map
    notes = DEFAULT_NOTE_MAP if note_map is None else note_map
    lines = ["CC  channel  parameter"]
    for cc in sorted(mapping):
        lines.append("%-3d %-8s %s" % (cc, midi_channel_name(cc), mapping[cc]))
    lines.append("")
    lines.append("note channel  Mode")
    for note in sorted(notes):
        lines.append("%-4d %-8s %s" % (note, note_channel_name(note), notes[note]))
    return "\n".join(lines)
