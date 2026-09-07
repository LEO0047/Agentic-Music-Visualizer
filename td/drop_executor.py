"""Drop execution and heartbeat watchdog — the reflex layer's own decisions.

SPEC §3.2: ``on_drop`` is the key field. The director decides *ahead of time*
what the next drop should look like; the reflex layer executes it on the frame
the drop lands, with no round trip and no glide. This module is that execution,
kept as pure logic so it can be tested without TouchDesigner.

Wiring inside TD (see ``build_network.py``):

* ``kick_exec`` — a CHOP Execute DAT on the ``kick`` channel, Off→On calls
  :func:`on_kick`. One call per rising edge, so one drop fires once.
* ``watchdog`` — an Execute DAT calling :func:`flush_pending` and
  :func:`heartbeat_watchdog` about once a second. Over 45 s without a heartbeat
  it warns and *keeps the current state* (SPEC §3.3) — it must never blank the
  output.

The kick also carries SPEC §3.3's other rule — *"整數與字串類參數只在下一個
kick 切換"*. ``osc_in_callbacks`` parks discrete values in
``comp.store('pending_discrete')``; :func:`on_kick` applies the queue right
after the ``on_drop`` payload (which wins any collision, because the director
decided that one specifically for this drop), and :func:`flush_pending` applies
whatever is still waiting after :data:`PENDING_MAX_WAIT` so a breakdown with no
kicks in it cannot strand a decision.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

__all__ = [
    "HEARTBEAT_TIMEOUT",
    "PENDING_MAX_WAIT",
    "DROP_SECTION",
    "ON_DROP_DAT",
    "DROP_KEYS",
    "PENDING_STORE_KEY",
    "on_kick",
    "drop_pending",
    "flag_drop",
    "apply_pending",
    "flush_pending",
    "heartbeat_watchdog",
]

HEARTBEAT_TIMEOUT = 45.0
"""SPEC §3.3: 45 s without a heartbeat means the director is gone."""

PENDING_MAX_WAIT = 2.0
"""Longest a queued discrete value waits for a kick before it lands anyway."""

DROP_SECTION = "drop"
ON_DROP_DAT = "on_drop_dat"

DROP_KEYS: tuple[tuple[str, str], ...] = (
    ("scene", "Scene"),
    ("palette", "Palette"),
    ("particle_mode", "Particlemode"),
)
"""The three fields ``on_drop`` carries, and the pars they land on."""

DROP_FLAG_KEY = "drop_event"
HEARTBEAT_STORE_KEY = "last_heartbeat"
HEARTBEAT_EPOCH_KEY = "heartbeat_epoch"

PENDING_STORE_KEY = "pending_discrete"
"""Written by ``osc_in_callbacks.queue_discrete``, drained here."""

SCRIPT_WRITE_KEY = "script_write"
"""Written here, claimed by ``osc_in_callbacks.onValueChange``.

Both keys are spelled out in each module rather than imported, the same way
``last_heartbeat`` already is: inside TD these two files are separate DATs and
either one has to keep working if the other fails to import.
"""


def _log(message: str) -> None:
    print("[amv drop] " + str(message))


def _now() -> float:
    return time.time()


def _par(comp: Any, name: str) -> Any:
    if comp is None:
        return None
    try:
        return getattr(comp.par, name)
    except AttributeError:
        return None
    except Exception:  # pragma: no cover - defensive
        return None


def _read(comp: Any, name: str, default: Any = None) -> Any:
    par = _par(comp, name)
    if par is None:
        return default
    try:
        return par.eval()
    except Exception:  # pragma: no cover - defensive
        return default


def _note_script_write(comp: Any, name: str, value: Any) -> None:
    """Mark this write as ours, by value, so the Parameter Execute DAT does not
    read the echo as a hand on the fader and freeze the par for 30 s (SPEC §5).
    ``osc_in_callbacks.claim_script_write`` is the other half."""
    try:
        marks = dict(comp.fetch(SCRIPT_WRITE_KEY, {}) or {})
    except Exception:  # pragma: no cover - defensive
        return
    marks[str(name)] = value
    comp.store(SCRIPT_WRITE_KEY, marks)


def _write(comp: Any, name: str, value: Any, log: Callable[[str], None]) -> bool:
    """Set a par, hard. Menus and strings are cuts by definition — no Lag CHOP
    sits on them — which is exactly what a drop wants."""
    par = _par(comp, name)
    if par is None:
        log(f"no custom par named {name!r}; skipped")
        return False
    try:
        names = list(getattr(par, "menuNames", None) or [])
        if names and str(value) not in names:
            log(f"{name}: {value!r} is not one of {names}; skipped")
            return False
        _note_script_write(comp, name, value)
        par.val = value
    except Exception as exc:
        log(f"{name}: cannot accept {value!r} ({exc}); skipped")
        return False
    return True


# --------------------------------------------------------------------------
# drop
# --------------------------------------------------------------------------


def flag_drop(comp: Any, now: float | None = None) -> float:
    """Mark that a drop is happening, independently of the ``Section`` par.

    The sidecar's section detection (SPEC §4) runs at 10 Hz; a local detector
    inside TD can call this to arm the next kick without waiting for it.
    """
    stamp = _now() if now is None else float(now)
    comp.store(DROP_FLAG_KEY, stamp)
    return stamp


def drop_pending(comp: Any) -> bool:
    """True if the next kick should execute ``on_drop``."""
    if str(_read(comp, "Section", "")) == DROP_SECTION:
        return True
    return bool(comp.fetch(DROP_FLAG_KEY, None))


def _on_drop_text(comp: Any) -> tuple[Any, str]:
    dat = None
    try:
        dat = comp.op(ON_DROP_DAT)
    except Exception:  # pragma: no cover - defensive
        dat = None
    if dat is not None:
        return dat, str(getattr(dat, "text", "") or "").strip()
    # No Text DAT (or it was renamed): the par holds the same JSON.
    return None, str(_read(comp, "Ondrop", "") or "").strip()


def _clear(comp: Any, dat: Any) -> None:
    """Clear both stores of the pending drop so it can only fire once."""
    if dat is not None:
        try:
            dat.text = ""
        except Exception:  # pragma: no cover - defensive
            pass
    par = _par(comp, "Ondrop")
    if par is not None:
        try:
            par.val = ""
        except Exception:  # pragma: no cover - defensive
            pass
    comp.unstore(DROP_FLAG_KEY)


def _run_drop(comp: Any, moment: float, log: Callable[[str], None]) -> dict | None:
    """The ``on_drop`` half of :func:`on_kick`. Returns what it applied."""
    if not drop_pending(comp):
        return None
    dat, text = _on_drop_text(comp)
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        log(f"on_drop payload is not JSON ({exc}); clearing it")
        _clear(comp, dat)
        return None
    if not isinstance(payload, dict):
        log(f"on_drop payload must be an object, got {type(payload).__name__}; clearing it")
        _clear(comp, dat)
        return None
    applied: dict[str, Any] = {}
    for key, parname in DROP_KEYS:
        if key not in payload:
            continue
        if _write(comp, parname, payload[key], log):
            applied[parname] = payload[key]
    # Cleared whether or not anything applied: a drop is a one-shot, and a
    # payload we could not use must not fire again on the next kick.
    _clear(comp, dat)
    comp.store("last_drop", moment)
    log(f"drop executed: {applied}" if applied else "drop payload had nothing to apply")
    return applied or None


def on_kick(comp: Any, now: float | None = None, log: Callable[[str], None] = _log) -> dict | None:
    """Execute the pre-decided ``on_drop`` look, then any queued discretes.

    Called on every kick (CHOP Execute, Off→On). Returns the dict of parameters
    it applied, or ``None`` when there was nothing to do — no drop pending, no
    stored JSON, no queue. Never raises: this runs inside a per-frame TD
    callback.

    The ``on_drop`` payload goes first and wins: it is what the director chose
    *for this drop*, so a queued ``scene`` waiting for a kick is discarded
    rather than applied a microsecond later on top of it.
    """
    moment = _now() if now is None else float(now)
    try:
        dropped = _run_drop(comp, moment, log)
        queued = apply_pending(comp, moment, log=log, skip=set(dropped or ()))
    except Exception as exc:  # pragma: no cover - last line of defence in TD
        log(f"on_kick failed: {exc}")
        return None
    applied = dict(dropped or {})
    applied.update(queued)
    return applied or None


# --------------------------------------------------------------------------
# discrete parameters queued for the next kick (SPEC §3.3)
# --------------------------------------------------------------------------


def _pending(comp: Any) -> dict:
    try:
        return dict(comp.fetch(PENDING_STORE_KEY, {}) or {})
    except Exception:  # pragma: no cover - defensive
        return {}


def apply_pending(
    comp: Any,
    now: float | None = None,
    log: Callable[[str], None] = _log,
    skip: Any = (),
    older_than: float | None = None,
) -> dict:
    """Write every queued discrete value. Returns ``{par: value}``.

    *skip* names parameters to drop from the queue without writing (the
    ``on_drop`` payload already set them). *older_than* limits the drain to
    entries that have waited at least that long — that is what makes
    :func:`flush_pending` a fallback rather than a second code path.
    """
    queue = _pending(comp)
    if not queue:
        return {}
    moment = _now() if now is None else float(now)
    skipped = set(skip or ())
    applied: dict[str, Any] = {}
    remaining: dict[str, Any] = {}
    for name, entry in queue.items():
        if name in skipped:
            continue
        try:
            value = entry["value"]
            waited = moment - float(entry.get("t", moment))
        except (TypeError, KeyError, ValueError):
            log(f"pending {name!r} is malformed; dropping it")
            continue
        if older_than is not None and waited < float(older_than):
            remaining[name] = entry
            continue
        if _write(comp, name, value, log):
            applied[name] = value
    if remaining:
        comp.store(PENDING_STORE_KEY, remaining)
    else:
        comp.unstore(PENDING_STORE_KEY)
    if applied:
        log(f"discrete applied: {applied}")
    return applied


def flush_pending(
    comp: Any,
    now: float | None = None,
    max_wait_s: float = PENDING_MAX_WAIT,
    log: Callable[[str], None] = _log,
) -> dict:
    """Apply queued discretes that have waited longer than *max_wait_s*.

    Called from the watchdog's ~1 s tick. SPEC §3.3 says discrete parameters
    switch on the next kick; a breakdown can have no kick for many bars, and a
    director decision that never lands is worse than one that lands a beat off
    the grid, so 2 s is the ceiling. Never raises.
    """
    try:
        return apply_pending(comp, now, log=log, older_than=max_wait_s)
    except Exception as exc:  # pragma: no cover - last line of defence in TD
        log(f"flush_pending failed: {exc}")
        return {}


# --------------------------------------------------------------------------
# heartbeat watchdog
# --------------------------------------------------------------------------


def heartbeat_watchdog(comp: Any, now: float | None = None, timeout: float = HEARTBEAT_TIMEOUT) -> bool:
    """Update ``Heartbeatage`` and report whether the director has gone quiet.

    Returns ``True`` when the last heartbeat is older than *timeout* seconds.
    If no heartbeat has ever arrived, the age is measured from the first call,
    so a director that never starts still trips the warning after 45 s.

    The TD side reacts by showing a warning and keeping the current state —
    SPEC §3.3, and SPEC §5's "畫面不得閃".
    """
    moment = _now() if now is None else float(now)
    last = comp.fetch(HEARTBEAT_STORE_KEY, None)
    if last is None:
        last = comp.fetch(HEARTBEAT_EPOCH_KEY, None)
        if last is None:
            last = moment
            comp.store(HEARTBEAT_EPOCH_KEY, moment)
    try:
        age = max(0.0, float(moment) - float(last))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        age = 0.0
    par = _par(comp, "Heartbeatage")
    if par is not None:
        try:
            par.val = round(age, 3)
        except Exception:  # pragma: no cover - defensive
            pass
    return age > float(timeout)
