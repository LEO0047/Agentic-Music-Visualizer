"""Director schema → TouchDesigner custom parameter specs (pure Python).

This module is the single place that decides *what* custom parameters the
``/project1/amv/director`` COMP carries. It imports nothing from TouchDesigner
so it can be unit-tested from pytest, and it is the only description of the
parameter surface that ``build_network.py`` (inside TD) and
``osc_in_callbacks.py`` (inside a TD DAT) both read.

Design notes
------------
* The schema (``director_schema.json``, SPEC §3.2) is the source of truth.
  Adding a scene or a palette means editing the JSON, not this file.
* TouchDesigner custom parameter names are constrained: alphanumeric only,
  first character upper case, every following character lower case. So
  ``camera_speed`` becomes ``Cameraspeed`` — see :func:`par_name`.
* ``transition`` is nested in the schema but flat in TD: it becomes
  ``Transitionmode`` (menu) + ``Transitionbeats`` (int).
* ``on_drop`` is nested too, but the reflex layer wants it *verbatim*: it
  becomes a single string parameter ``Ondrop`` holding the JSON, which
  ``drop_executor.on_kick`` parses on the frame a drop lands.
* Non-schema parameters (``Bpm``, ``Mode``, ``Heartbeat``, ``Heartbeatage``,
  ``Record``, ``Section``, ``Projectmsource``) live here too, because the reflex
  layer needs them and the manual/rule modes (SPEC §5) have to work with the
  director offline.
"""

from __future__ import annotations

import json
import os
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "ParSpec",
    "APPEND_METHODS",
    "PAGE_DIRECTOR",
    "PAGE_RUNTIME",
    "DEFAULT_BPM",
    "DEFAULTS",
    "SECTIONS",
    "MODES",
    "PROJECTM_SOURCES",
    "par_name",
    "is_valid_par_name",
    "menu_label",
    "pars_from_schema",
    "load_default_schema",
    "specs_by_name",
    "specs_by_address",
    "lag_seconds",
]


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

PAGE_DIRECTOR = "Director"
"""Custom page holding everything the director decides (SPEC §3.2)."""

PAGE_RUNTIME = "Runtime"
"""Custom page holding show-runtime state: BPM, mode, heartbeat, record."""

DEFAULT_BPM = 145.0
"""Psytrance-ish default so glide lengths are sane before anyone sets a BPM."""

SECTIONS: tuple[str, ...] = ("build", "drop", "breakdown", "steady")
"""Values the sidecar sends back on ``/feat/section`` (SPEC §4)."""

MODES: tuple[str, ...] = ("gpt", "rule", "manual")
"""SPEC §5. Default is ``rule``: the show must run with the director offline."""

PROJECTM_SOURCES: tuple[str, ...] = ("none", "syphon", "ndi")
"""Where the projectM layer is captured from (SPEC Phase 5 細節), in Switch TOP
input order: ``none`` is a black Constant TOP, ``syphon`` a Syphon Spout In TOP
fed by Syphoner, ``ndi`` an NDI In TOP fed by OBS. This is a *rig* choice, not
a director decision — the director only ever writes ``Projectmmix`` — so it
lives on the Runtime page with no OSC address.
"""

APPEND_METHODS: dict[str, str] = {
    "Menu": "appendMenu",
    "Float": "appendFloat",
    "Int": "appendInt",
    "Str": "appendStr",
    "Toggle": "appendToggle",
}
"""ParSpec.style → the ``appendXxx`` method on a TD custom page."""

DEFAULTS: dict[str, Any] = {
    # Schema-derived. Chosen so a freshly built network already renders
    # something calm without a single OSC message arriving.
    "Scene": "tunnel",
    "Palette": "violet_cyan",
    "Feedback": 0.0,
    "Symmetry": 1,
    "Cameraspeed": 0.0,
    "Particlemode": "none",
    "Projectmmix": 0.0,
    "Transitionmode": "glide",
    "Transitionbeats": 2,
    "Ondrop": "",
    "Intent": "",
    # Runtime.
    "Bpm": DEFAULT_BPM,
    "Mode": "rule",
    "Heartbeat": 0,
    "Heartbeatage": 0.0,
    "Record": 0,
    "Section": "steady",
    "Projectmsource": "none",
}

_ALNUM = frozenset(string.ascii_letters + string.digits)


# --------------------------------------------------------------------------
# name helpers
# --------------------------------------------------------------------------


def par_name(key: str) -> str:
    """Turn a schema key into a legal TD custom parameter name.

    TD custom parameter names must be alphanumeric, start with an upper case
    letter and continue in lower case::

        >>> par_name("camera_speed")
        'Cameraspeed'
        >>> par_name("transition.beats")
        'Transitionbeats'
    """
    cleaned = "".join(ch for ch in str(key) if ch in _ALNUM)
    if not cleaned:
        raise ValueError(f"{key!r} has no alphanumeric characters to build a par name from")
    if cleaned[0] in string.digits:
        raise ValueError(f"{key!r} would produce a par name starting with a digit")
    return cleaned[0].upper() + cleaned[1:].lower()


def is_valid_par_name(name: str) -> bool:
    """True if *name* obeys the TD custom parameter naming rules."""
    if not name or name[0] not in string.ascii_uppercase:
        return False
    return all(ch in _ALNUM and ch not in string.ascii_uppercase for ch in name[1:])


def menu_label(value: str) -> str:
    """``fractal_temple`` → ``Fractal Temple`` for the TD menu UI."""
    return " ".join(word.capitalize() for word in str(value).replace("-", "_").split("_") if word)


def _labels(names: Sequence[str]) -> tuple[str, ...]:
    return tuple(menu_label(n) for n in names)


# --------------------------------------------------------------------------
# the spec
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParSpec:
    """One TouchDesigner custom parameter.

    ``style`` is the TD par style (``Menu`` / ``Float`` / ``Int`` / ``Str`` /
    ``Toggle``); :attr:`append_method` gives the matching page method.
    ``address`` is the OSC address that writes this parameter, or ``None`` for
    parameters only a human (or TD itself) touches.
    """

    name: str
    style: str
    label: str
    default: Any = None
    min: float | None = None
    max: float | None = None
    clamp: bool = False
    menu_names: tuple[str, ...] = ()
    menu_labels: tuple[str, ...] = ()
    readonly: bool = False
    page: str = PAGE_DIRECTOR
    address: str | None = None
    source: str | None = None
    help: str = ""

    def __post_init__(self) -> None:
        if self.style not in APPEND_METHODS:
            raise ValueError(f"{self.name}: unknown par style {self.style!r}")
        if not is_valid_par_name(self.name):
            raise ValueError(
                f"{self.name!r} is not a legal TD custom par name "
                "(alphanumeric, first char upper, rest lower)"
            )
        if self.style == "Menu" and not self.menu_names:
            raise ValueError(f"{self.name}: a Menu par needs menu names")
        if self.menu_labels and len(self.menu_labels) != len(self.menu_names):
            raise ValueError(f"{self.name}: menu label/name length mismatch")

    @property
    def append_method(self) -> str:
        """Name of the TD page method that creates this par."""
        return APPEND_METHODS[self.style]

    @property
    def osc_key(self) -> str | None:
        """Last path element of :attr:`address` (``/director/scene`` → ``scene``)."""
        return self.address.rsplit("/", 1)[-1] if self.address else None

    def clamp_value(self, value: float) -> float:
        """Clamp *value* into this par's schema range (no-op without bounds)."""
        if self.min is not None and value < self.min:
            value = self.min
        if self.max is not None and value > self.max:
            value = self.max
        return value

    def coerce(self, value: Any) -> Any:
        """Coerce an incoming OSC argument to what this par accepts.

        Menus validate against :attr:`menu_names` and raise ``ValueError`` for
        anything else — an unknown scene name must never reach a TD par.
        """
        if self.style == "Menu":
            text = str(value).strip()
            if text not in self.menu_names:
                raise ValueError(f"{self.name}: {text!r} is not one of {list(self.menu_names)}")
            return text
        if self.style == "Str":
            return str(value)
        if self.style == "Toggle":
            if isinstance(value, str):
                text = value.strip().lower()
                if text in ("", "0", "off", "false", "no"):
                    return 0
                return 1
            return 1 if value else 0
        if self.style == "Int":
            return int(self.clamp_value(int(round(float(value)))))
        # Float
        return float(self.clamp_value(float(value)))


# --------------------------------------------------------------------------
# schema → specs
# --------------------------------------------------------------------------


def _menu_spec(name: str, node: dict, *, address: str, source: str, page: str = PAGE_DIRECTOR) -> ParSpec:
    names = tuple(node["enum"])
    return ParSpec(
        name=name,
        style="Menu",
        label=menu_label(source.rsplit(".", 1)[-1]),
        default=DEFAULTS.get(name, names[0]),
        menu_names=names,
        menu_labels=_labels(names),
        page=page,
        address=address,
        source=source,
        help=str(node.get("description", "")),
    )


def _number_spec(name: str, node: dict, *, address: str, source: str) -> ParSpec:
    style = "Int" if node.get("type") == "integer" else "Float"
    lo = node.get("minimum")
    hi = node.get("maximum")
    return ParSpec(
        name=name,
        style=style,
        label=menu_label(source.rsplit(".", 1)[-1]),
        default=DEFAULTS.get(name, lo if lo is not None else 0),
        min=lo,
        max=hi,
        clamp=lo is not None or hi is not None,
        page=PAGE_DIRECTOR,
        address=address,
        source=source,
        help=str(node.get("description", "")),
    )


def _runtime_specs() -> list[ParSpec]:
    """Parameters the reflex layer needs that the schema does not describe."""
    return [
        ParSpec(
            name="Bpm",
            style="Float",
            label="BPM",
            default=DEFAULTS["Bpm"],
            min=40.0,
            max=220.0,
            clamp=True,
            page=PAGE_RUNTIME,
            help="Beats per minute; lag seconds = Transitionbeats * 60 / Bpm.",
        ),
        ParSpec(
            name="Mode",
            style="Menu",
            label="Mode",
            default=DEFAULTS["Mode"],
            menu_names=MODES,
            menu_labels=_labels(MODES),
            page=PAGE_RUNTIME,
            help="SPEC 5: gpt / rule / manual. In manual a touched par freezes for 30 s.",
        ),
        ParSpec(
            name="Section",
            style="Menu",
            label="Section",
            default=DEFAULTS["Section"],
            menu_names=SECTIONS,
            menu_labels=_labels(SECTIONS),
            page=PAGE_RUNTIME,
            address="/feat/section",
            help="Section the sidecar detected; sent back on /feat/section.",
        ),
        ParSpec(
            name="Heartbeat",
            style="Int",
            label="Heartbeat",
            default=DEFAULTS["Heartbeat"],
            min=0,
            max=None,
            page=PAGE_RUNTIME,
            address="/director/heartbeat",
            help="Increments once per director decision (SPEC 3.3).",
        ),
        ParSpec(
            name="Heartbeatage",
            style="Float",
            label="Heartbeat Age",
            default=DEFAULTS["Heartbeatage"],
            min=0.0,
            max=120.0,
            readonly=True,
            page=PAGE_RUNTIME,
            help="Seconds since the last heartbeat; > 45 s means the director is gone.",
        ),
        ParSpec(
            name="Record",
            style="Toggle",
            label="Record",
            default=DEFAULTS["Record"],
            page=PAGE_RUNTIME,
            help="Drives the Movie File Out TOP record par.",
        ),
        ParSpec(
            name="Projectmsource",
            style="Menu",
            label="ProjectM Source",
            default=DEFAULTS["Projectmsource"],
            menu_names=PROJECTM_SOURCES,
            menu_labels=("None (black)", "Syphon", "NDI"),
            page=PAGE_RUNTIME,
            help=(
                "Phase 5 capture path, and the projectm_in Switch TOP index. "
                "No OSC address on purpose: the director only drives Projectmmix."
            ),
        ),
    ]


def pars_from_schema(schema: dict) -> list[ParSpec]:
    """Map ``director_schema.json`` onto TouchDesigner custom parameters.

    String enums become Menu pars whose menu *names* are the enum values,
    numbers become clamped Float pars, integers become Int pars, ``transition``
    expands to ``Transitionmode`` + ``Transitionbeats``, ``on_drop`` collapses
    to a single ``Ondrop`` string par holding the JSON, and ``intent`` becomes
    a string par. The runtime pars (BPM, mode, section, heartbeat, record) are
    appended after the schema-derived ones.
    """
    props: dict[str, Any] = dict(schema.get("properties", {}))
    specs: list[ParSpec] = []

    for key, node in props.items():
        address = f"/director/{key}"
        if key == "transition":
            sub = node["properties"]
            specs.append(
                _menu_spec(
                    par_name("transition_mode"),
                    sub["mode"],
                    address="/director/transition_mode",
                    source="transition.mode",
                )
            )
            specs.append(
                _number_spec(
                    par_name("transition_beats"),
                    sub["beats"],
                    address="/director/transition_beats",
                    source="transition.beats",
                )
            )
            continue
        if key == "on_drop":
            # Kept verbatim as JSON: drop_executor parses it on the drop frame.
            specs.append(
                ParSpec(
                    name=par_name(key),
                    style="Str",
                    label=menu_label(key),
                    default=DEFAULTS.get(par_name(key), ""),
                    page=PAGE_DIRECTOR,
                    address=address,
                    source=key,
                    help="JSON object {scene, palette, particle_mode} applied on the drop frame.",
                )
            )
            continue
        if "enum" in node:
            specs.append(_menu_spec(par_name(key), node, address=address, source=key))
            continue
        kind = node.get("type")
        if kind in ("number", "integer"):
            specs.append(_number_spec(par_name(key), node, address=address, source=key))
            continue
        if kind == "string":
            specs.append(
                ParSpec(
                    name=par_name(key),
                    style="Str",
                    label=menu_label(key),
                    default=DEFAULTS.get(par_name(key), ""),
                    page=PAGE_DIRECTOR,
                    address=address,
                    source=key,
                    help=str(node.get("description", "")),
                )
            )
            continue
        raise ValueError(f"schema property {key!r}: cannot map type {kind!r} to a TD par")

    specs.extend(_runtime_specs())

    seen: dict[str, str] = {}
    for spec in specs:
        if spec.name in seen:
            raise ValueError(f"duplicate par name {spec.name!r} (from {seen[spec.name]} and {spec.source})")
        seen[spec.name] = str(spec.source)
    return specs


# --------------------------------------------------------------------------
# schema location (works from pytest and from a TD DAT)
# --------------------------------------------------------------------------


def load_default_schema(path: str | os.PathLike[str] | None = None) -> dict:
    """Load ``director_schema.json``.

    Order: explicit *path*, ``$AMV_SCHEMA``, the repo root next to this file,
    this folder. Kept independent of ``amv.schema`` on purpose — inside
    TouchDesigner the ``amv`` package is not importable.
    """
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path).expanduser())
    env = os.environ.get("AMV_SCHEMA")
    if env:
        candidates.append(Path(env).expanduser())
    try:
        here = Path(__file__).resolve().parent
    except NameError:  # pragma: no cover - loaded as raw DAT text
        here = None
    if here is not None:
        candidates.append(here.parent / "director_schema.json")
        candidates.append(here / "director_schema.json")
    for candidate in candidates:
        if candidate.is_file():
            with open(candidate, encoding="utf-8") as fh:
                return json.load(fh)
    raise FileNotFoundError(
        "director_schema.json not found; tried " + ", ".join(str(c) for c in candidates)
    )


def specs_by_name(specs: Iterable[ParSpec]) -> dict[str, ParSpec]:
    """``{'Scene': ParSpec(...), ...}``."""
    return {spec.name: spec for spec in specs}


def specs_by_address(specs: Iterable[ParSpec]) -> dict[str, ParSpec]:
    """``{'/director/scene': ParSpec(...), ...}``, skipping unaddressed pars."""
    return {spec.address: spec for spec in specs if spec.address}


# --------------------------------------------------------------------------
# timing
# --------------------------------------------------------------------------


def lag_seconds(beats: float, bpm: float, *, fallback_bpm: float = DEFAULT_BPM) -> float:
    """Glide length in seconds for *beats* at *bpm* (SPEC §3.3).

    A non-positive, NaN or unreadable BPM falls back to :data:`DEFAULT_BPM`
    rather than raising: this expression runs every frame inside TD and a
    division by zero there would stall the show.
    """
    try:
        rate = float(bpm)
    except (TypeError, ValueError):
        rate = fallback_bpm
    if not rate > 0 or rate != rate or rate == float("inf"):
        rate = fallback_bpm
    try:
        count = float(beats)
    except (TypeError, ValueError):
        count = 0.0
    if count != count:  # NaN
        count = 0.0
    return max(0.0, count) * 60.0 / rate
