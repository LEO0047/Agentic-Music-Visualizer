"""OSC In DAT callbacks for the ``director`` COMP (SPEC §3.3).

``build_network.py`` copies this file's text into
``/project1/amv/director/osc_in_callbacks`` and points the OSC In DAT (port
9001) at it. The same file imports and runs outside TouchDesigner, which is how
:mod:`tests.test_td_callbacks` exercises the routing without TD.

Contract
--------
Every message is ``/director/<key>`` (or ``/feat/section``) with one argument:

===============================  ==========================================
``/director/scene``              menu par ``Scene``
``/director/palette``            menu par ``Palette``
``/director/feedback``           float par ``Feedback``
``/director/symmetry``           int par ``Symmetry``
``/director/camera_speed``       float par ``Cameraspeed``
``/director/particle_mode``      menu par ``Particlemode``
``/director/projectm_mix``       float par ``Projectmmix``
``/director/transition_mode``    menu par ``Transitionmode``
``/director/transition_beats``   int par ``Transitionbeats``
``/director/transition``         JSON ``{"mode":…,"beats":…}`` → both of those
``/director/intent``             str par ``Intent``
``/director/on_drop``            JSON → Text DAT ``on_drop_dat`` + par ``Ondrop``
``/director/heartbeat``          int par ``Heartbeat`` + ``last_heartbeat``
``/feat/section``                menu par ``Section``
===============================  ==========================================

Anything else is ignored with one log line — a stray address must never raise
inside a DAT callback, because TD would keep re-raising it 60 times a second.

The callback only *writes parameters*. No visual logic lives here: that is the
whole point of SPEC §3.3's "callback 只把值寫進 Custom Parameter".

Freeze (SPEC §5)
----------------
SPEC §5: *"任何參數被手動一動，該欄位凍結 30 s"* — in **every** mode. So a
parameter a human touched less than 30 s ago is frozen in ``gpt`` and ``rule``
too, not only in ``manual``; the hand on the fader always wins for half a
minute. ``manual`` mode is the stronger case: *every* director write is
skipped, touched or not.

Two things are never frozen, because they are show state rather than director
decisions: ``/director/heartbeat`` (the watchdog must keep seeing it, or a
manual-mode show would start warning that the director is gone) and
``/feat/section`` (the sidecar's own section detection, which the drop executor
needs). See :data:`NEVER_FROZEN`.

The bookkeeping is a dict in the COMP's storage (``comp.store``/``comp.fetch``),
fed by :func:`note_touch` — wired up in TD by the ``par_exec`` Parameter
Execute DAT.

Discrete parameters wait for the next kick (SPEC §3.3)
------------------------------------------------------
SPEC §3.3: *"整數與字串類參數只在下一個 kick 切換"*. Floats (``Feedback``,
``Cameraspeed``, ``Projectmmix``) go straight to the par — the Lag CHOPs glide
them. The discrete four (:data:`DISCRETE_PARS`) are validated immediately but
*queued* in ``comp.store('pending_discrete', ...)`` unless ``Transitionmode``
is ``cut``; ``drop_executor.on_kick`` applies the queue on the next kick, and
``drop_executor.flush_pending`` applies anything still waiting after 2 s so a
kick-less breakdown cannot strand a decision.
"""

from __future__ import annotations

import json
import time
import traceback
from typing import Any, Callable

__all__ = [
    "FREEZE_SECONDS",
    "TOUCH_STORE_KEY",
    "HEARTBEAT_STORE_KEY",
    "PENDING_STORE_KEY",
    "DISCRETE_PARS",
    "NEVER_FROZEN",
    "address_map",
    "route",
    "is_frozen",
    "note_touch",
    "clear_touches",
    "note_script_write",
    "claim_script_write",
    "pending_discrete",
    "queue_discrete",
    "director_from",
    "onReceiveOSC",
    "onValueChange",
]

FREEZE_SECONDS = 30.0
"""SPEC §5: a manually touched parameter is frozen for 30 seconds, in every mode."""

TOUCH_STORE_KEY = "manual_touch"
HEARTBEAT_STORE_KEY = "last_heartbeat"

PENDING_STORE_KEY = "pending_discrete"
"""Storage key shared with :mod:`drop_executor` (which applies the queue)."""

SCRIPT_WRITE_KEY = "script_write"
"""Storage key shared with :mod:`drop_executor`; see :func:`note_script_write`."""

DISCRETE_PARS: tuple[str, ...] = ("Scene", "Palette", "Symmetry", "Particlemode")
"""SPEC §3.3: integer and string parameters only switch on the next kick."""

NEVER_FROZEN: frozenset[str] = frozenset({"Heartbeat", "Heartbeatage", "Section"})
"""Show state, not director decisions: these land even in ``manual`` mode."""

CUT_MODE = "cut"
"""``Transitionmode`` value that means "no waiting" — apply discretes now."""

ON_DROP_ADDRESS = "/director/on_drop"
HEARTBEAT_ADDRESS = "/director/heartbeat"
TRANSITION_ADDRESS = "/director/transition"
ON_DROP_DAT = "on_drop_dat"

#: Used only if ``parspec`` cannot be imported (e.g. this file was pasted into
#: a DAT with no sys.path set up). Names must match ``parspec.par_name``.
FALLBACK_ADDRESSES: dict[str, str] = {
    "/director/scene": "Scene",
    "/director/palette": "Palette",
    "/director/feedback": "Feedback",
    "/director/symmetry": "Symmetry",
    "/director/camera_speed": "Cameraspeed",
    "/director/particle_mode": "Particlemode",
    "/director/projectm_mix": "Projectmmix",
    "/director/transition_mode": "Transitionmode",
    "/director/transition_beats": "Transitionbeats",
    "/director/on_drop": "Ondrop",
    "/director/intent": "Intent",
    "/director/heartbeat": "Heartbeat",
    "/feat/section": "Section",
}


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


def _log(message: str) -> None:
    """One line, prefixed. TD's textport is the only console we get."""
    print("[amv osc] " + str(message))


def _now() -> float:
    """Wall clock. Deliberately the same source in every reflex-layer module."""
    return time.time()


_PARSPEC: Any = None
_PARSPEC_TRIED = False


def _parspec() -> Any:
    """Import ``parspec`` however this module happens to have been loaded.

    Inside TD, ``build_network.py`` puts the ``td/`` folder on ``sys.path``
    before creating the DAT, so a plain ``import parspec`` works. Under pytest
    the same import works because the test adds ``td/`` to ``sys.path``. The
    last resort loads it by file path next to this module.
    """
    global _PARSPEC, _PARSPEC_TRIED
    if _PARSPEC is not None or _PARSPEC_TRIED:
        return _PARSPEC
    _PARSPEC_TRIED = True
    try:
        import parspec as module

        _PARSPEC = module
        return _PARSPEC
    except Exception:
        pass
    try:
        import importlib.util
        import os

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parspec.py")
        spec = importlib.util.spec_from_file_location("parspec", path)
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _PARSPEC = module
    except Exception:
        _log("parspec not importable; falling back to the built-in address map")
    return _PARSPEC


_ADDRESS_MAP: dict[str, tuple[str, Any]] | None = None


def address_map(force: bool = False) -> dict[str, tuple[str, Any]]:
    """``{'/director/scene': ('Scene', ParSpec|None), ...}``."""
    global _ADDRESS_MAP
    if _ADDRESS_MAP is not None and not force:
        return _ADDRESS_MAP
    module = _parspec()
    built: dict[str, tuple[str, Any]] = {}
    if module is not None:
        try:
            specs = module.pars_from_schema(module.load_default_schema())
            built = {spec.address: (spec.name, spec) for spec in specs if spec.address}
        except Exception as exc:
            _log(f"could not build the address map from the schema ({exc}); using the fallback")
            built = {}
    if not built:
        built = {address: (name, None) for address, name in FALLBACK_ADDRESSES.items()}
    _ADDRESS_MAP = built
    return _ADDRESS_MAP


# --------------------------------------------------------------------------
# freeze (SPEC §5): touched for 30 s in every mode, everything in manual
# --------------------------------------------------------------------------


def _mode(comp: Any) -> str:
    par = _par(comp, "Mode")
    if par is None:
        return "rule"
    try:
        return str(par.eval())
    except Exception:  # pragma: no cover - TD par that refuses to evaluate
        return "rule"


def note_touch(comp: Any, parname: str, now: float | None = None) -> float:
    """Record that a human just moved *parname*; starts its 30 s freeze."""
    stamp = _now() if now is None else float(now)
    touched = dict(comp.fetch(TOUCH_STORE_KEY, {}) or {})
    touched[str(parname)] = stamp
    comp.store(TOUCH_STORE_KEY, touched)
    return stamp


def clear_touches(comp: Any) -> None:
    """Forget every recorded touch (used when leaving manual mode)."""
    comp.store(TOUCH_STORE_KEY, {})


def note_script_write(comp: Any, parname: str, value: Any) -> None:
    """Record that *this code* wrote *value* to *parname*, not a human.

    TD's Parameter Execute DAT cannot tell a script write from a hand on the
    fader — ``onValueChange`` fires for both. Now that SPEC §5's 30 s freeze
    applies in ``gpt`` and ``rule`` too, an unguarded echo would mean every
    director write froze the parameter it had just set, and the director would
    go quiet one message into the show. So each write leaves a single-use
    marker that :func:`claim_script_write` matches *by value*: the echo carries
    the value we wrote, a human's move carries a different one.
    """
    try:
        marks = dict(comp.fetch(SCRIPT_WRITE_KEY, {}) or {})
    except Exception:  # pragma: no cover - defensive
        return
    marks[str(parname)] = value
    comp.store(SCRIPT_WRITE_KEY, marks)


def _same_value(a: Any, b: Any) -> bool:
    if isinstance(a, str) or isinstance(b, str):
        return str(a) == str(b)
    try:
        return abs(float(a) - float(b)) <= 1e-9
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return a == b


def claim_script_write(comp: Any, parname: str, value: Any) -> bool:
    """Consume a :func:`note_script_write` marker. True if this echo is ours."""
    try:
        marks = dict(comp.fetch(SCRIPT_WRITE_KEY, {}) or {})
    except Exception:  # pragma: no cover - defensive
        return False
    name = str(parname)
    if name not in marks:
        return False
    written = marks.pop(name)
    if not _same_value(written, value):
        # A human got there first: leave the marker for the echo still to come.
        return False
    comp.store(SCRIPT_WRITE_KEY, marks)
    return True


def is_frozen(comp: Any, parname: str, now: float | None = None) -> bool:
    """True if a director write to *parname* must be skipped right now.

    SPEC §5 has two rules, and this is both of them:

    * a parameter a human touched under :data:`FREEZE_SECONDS` ago is frozen in
      **every** mode — ``gpt`` and ``rule`` included;
    * ``manual`` mode freezes everything, touched or not.

    :data:`NEVER_FROZEN` is the exception in both cases: the heartbeat and the
    detected section are show state, and blocking them would make manual mode
    look like a dead director.
    """
    name = str(parname)
    if name in NEVER_FROZEN:
        return False
    if _mode(comp) == "manual":
        return True
    touched = comp.fetch(TOUCH_STORE_KEY, {}) or {}
    stamp = touched.get(name)
    if stamp is None:
        return False
    moment = _now() if now is None else float(now)
    return (moment - float(stamp)) < FREEZE_SECONDS


# --------------------------------------------------------------------------
# discrete parameters wait for the next kick (SPEC §3.3)
# --------------------------------------------------------------------------


def pending_discrete(comp: Any) -> dict[str, dict]:
    """The queue of discrete values waiting for a kick, ``{par: {...}}``."""
    try:
        return dict(comp.fetch(PENDING_STORE_KEY, {}) or {})
    except Exception:  # pragma: no cover - defensive
        return {}


def queue_discrete(comp: Any, parname: str, value: Any, now: float | None = None) -> dict:
    """Park *value* until the next kick (or the 2 s flush). Returns the entry."""
    stamp = _now() if now is None else float(now)
    entry = {"value": value, "t": stamp}
    queue = pending_discrete(comp)
    queue[str(parname)] = entry
    comp.store(PENDING_STORE_KEY, queue)
    return entry


def _transition_mode(comp: Any) -> str:
    par = _par(comp, "Transitionmode")
    if par is None:
        return ""
    try:
        return str(par.eval())
    except Exception:  # pragma: no cover - defensive
        return ""


# --------------------------------------------------------------------------
# parameter writing
# --------------------------------------------------------------------------


def _par(comp: Any, name: str) -> Any:
    if comp is None:
        return None
    try:
        return getattr(comp.par, name)
    except AttributeError:
        return None
    except Exception:  # pragma: no cover - defensive: TD par collections vary
        return None


def _coerce(par: Any, value: Any, spec: Any) -> Any:
    """Coerce *value* for *par*, preferring the schema spec when we have one."""
    if spec is not None:
        return spec.coerce(value)
    names = list(getattr(par, "menuNames", None) or [])
    if names:
        text = str(value).strip()
        if text not in names:
            raise ValueError(f"{text!r} is not one of {names}")
        return text
    style = str(getattr(par, "style", "Float"))
    if style == "Str":
        return str(value)
    if style in ("Int", "Toggle"):
        return int(round(float(value)))
    return float(value)


def write_par(comp: Any, name: str, value: Any, spec: Any = None, log: Callable[[str], None] = _log) -> str:
    """Write one parameter. Returns ``set`` / ``ignored`` / ``error``."""
    par = _par(comp, name)
    if par is None:
        log(f"no custom par named {name!r} on {getattr(comp, 'name', comp)!r}; ignored")
        return "ignored"
    try:
        coerced = _coerce(par, value, spec)
    except Exception as exc:
        log(f"{name}: cannot accept {value!r} ({exc})")
        return "error"
    note_script_write(comp, name, coerced)
    try:
        par.val = coerced
    except Exception as exc:
        log(f"{name}: cannot accept {value!r} ({exc})")
        return "error"
    return "set"


def _write(comp: Any, name: str, value: Any, spec: Any, now: float, log: Callable[[str], None]) -> str:
    """Freeze check, then either write the par or queue it for the next kick."""
    if is_frozen(comp, name, now):
        log(f"{name} frozen (SPEC 5: manual mode or touched < {FREEZE_SECONDS:.0f}s ago); "
            f"skipping {value!r}")
        return "frozen"
    if name in DISCRETE_PARS and _transition_mode(comp) != CUT_MODE:
        # Validate now so a bad enum is an error the sidecar sees immediately,
        # not a surprise on the next kick.
        par = _par(comp, name)
        if par is None:
            log(f"no custom par named {name!r} on {getattr(comp, 'name', comp)!r}; ignored")
            return "ignored"
        try:
            coerced = _coerce(par, value, spec)
        except Exception as exc:
            log(f"{name}: cannot accept {value!r} ({exc})")
            return "error"
        queue_discrete(comp, name, coerced, now)
        return "pending"
    return write_par(comp, name, value, spec, log)


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------


def _first_arg(args: Any) -> Any:
    if args is None:
        return None
    if isinstance(args, (str, bytes, int, float)):
        return args
    try:
        return args[0]
    except (IndexError, KeyError, TypeError):
        return None


def _normalise(address: Any) -> str:
    text = str(address or "").strip()
    if not text:
        return ""
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/") or "/"


def _spec_for(name: str) -> Any:
    for parname, spec in address_map().values():
        if parname == name:
            return spec
    return None


def _handle_on_drop(comp: Any, value: Any, now: float, log: Callable[[str], None]) -> str:
    text = "" if value is None else str(value)
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        log(f"on_drop is not JSON ({exc}); ignored")
        return "error"
    if not isinstance(payload, dict):
        log(f"on_drop must be a JSON object, got {type(payload).__name__}; ignored")
        return "error"
    if is_frozen(comp, "Ondrop", now):
        log("Ondrop frozen (manual mode); skipping")
        return "frozen"
    dat = None
    try:
        dat = comp.op(ON_DROP_DAT)
    except Exception:  # pragma: no cover - defensive
        dat = None
    if dat is None:
        log(f"no {ON_DROP_DAT!r} Text DAT under {getattr(comp, 'name', comp)!r}; par only")
    else:
        dat.text = json.dumps(payload)
    write_par(comp, "Ondrop", json.dumps(payload), _spec_for("Ondrop"), log)
    return "set"


def _handle_heartbeat(comp: Any, value: Any, now: float, log: Callable[[str], None]) -> str:
    comp.store(HEARTBEAT_STORE_KEY, float(now))
    try:
        count = int(round(float(value)))
    except (TypeError, ValueError):
        count = int(comp.fetch("heartbeat_count", 0)) + 1
    comp.store("heartbeat_count", count)
    write_par(comp, "Heartbeat", count, _spec_for("Heartbeat"), log)
    return "heartbeat"


def _handle_transition_json(comp: Any, value: Any, now: float, log: Callable[[str], None]) -> str:
    """Accept the nested ``transition`` object as one JSON string."""
    try:
        payload = json.loads("" if value is None else str(value))
    except (TypeError, ValueError) as exc:
        log(f"transition is not JSON ({exc}); ignored")
        return "error"
    if not isinstance(payload, dict):
        log("transition must be a JSON object; ignored")
        return "error"
    results = []
    if "mode" in payload:
        results.append(_write(comp, "Transitionmode", payload["mode"], _spec_for("Transitionmode"), now, log))
    if "beats" in payload:
        results.append(_write(comp, "Transitionbeats", payload["beats"], _spec_for("Transitionbeats"), now, log))
    if not results:
        log("transition JSON had neither mode nor beats; ignored")
        return "ignored"
    if "set" in results:
        return "set"
    return results[0]


def route(comp: Any, address: Any, args: Any, now: float | None = None, log: Callable[[str], None] = _log) -> str:
    """Route one OSC message onto the director COMP.

    Returns a short status so the caller (and the tests) can tell what
    happened: ``set``, ``pending`` (a discrete par queued for the next kick),
    ``heartbeat``, ``frozen``, ``ignored`` or ``error``. Never raises.
    """
    moment = _now() if now is None else float(now)
    if comp is None:
        log(f"no director COMP resolved for {address!r}; ignored")
        return "ignored"
    addr = _normalise(address)
    value = _first_arg(args)
    try:
        if addr == ON_DROP_ADDRESS:
            return _handle_on_drop(comp, value, moment, log)
        if addr == HEARTBEAT_ADDRESS:
            return _handle_heartbeat(comp, value, moment, log)
        if addr == TRANSITION_ADDRESS:
            return _handle_transition_json(comp, value, moment, log)
        entry = address_map().get(addr)
        if entry is None:
            log(f"unknown OSC address {addr!r}; ignored")
            return "ignored"
        name, spec = entry
        return _write(comp, name, value, spec, moment, log)
    except Exception as exc:  # pragma: no cover - last line of defence in TD
        log(f"{addr}: {exc}\n{traceback.format_exc()}")
        return "error"


def director_from(dat: Any) -> Any:
    """Find the director COMP from the DAT the callback fired on.

    The OSC In DAT lives *inside* the director COMP, so ``dat.parent()`` is
    normally the answer; the walk upwards is there so the DAT still works if
    someone moves it one level out.
    """
    if dat is None:
        return None
    try:
        comp = dat.parent()
    except Exception:  # pragma: no cover - defensive
        return None
    for _ in range(3):
        if comp is None:
            return None
        if _par(comp, "Scene") is not None:
            return comp
        try:
            child = comp.op("director")
        except Exception:  # pragma: no cover - defensive
            child = None
        if child is not None and _par(child, "Scene") is not None:
            return child
        try:
            comp = comp.parent()
        except Exception:  # pragma: no cover - defensive
            return None
    return None


# --------------------------------------------------------------------------
# TouchDesigner entry points
# --------------------------------------------------------------------------


def onReceiveOSC(dat, rowIndex, message, bytes, timeStamp, address, args, peer):  # noqa: A002
    """OSC In DAT callback. The signature is TD's, including ``bytes``."""
    try:
        return route(director_from(dat), address, args)
    except Exception:  # pragma: no cover - TD must never see an exception here
        _log(traceback.format_exc())
        return "error"


def onValueChange(par, prev):
    """Parameter Execute DAT callback: a director parameter changed.

    A change this code made is claimed and ignored; anything left is a human
    on the fader, and starts that parameter's 30 s freeze (SPEC §5).
    """
    try:
        owner = par.owner
        if claim_script_write(owner, par.name, par.eval()):
            return
        note_touch(owner, par.name)
    except Exception:  # pragma: no cover - defensive
        _log(traceback.format_exc())
    return
