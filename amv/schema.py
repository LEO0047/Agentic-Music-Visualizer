"""Director decision schema: loading, constants and defensive coercion.

The single source of truth is ``director_schema.json`` at the repo root — the
same file we hand to ``codex exec --output-schema``. Everything in this module
is derived from it, so adding a scene or a palette only means editing the JSON.

``validate_and_clamp`` is the airbag between an LLM and the TouchDesigner
reflex layer: it repairs what is repairable (numeric strings, out-of-range
numbers, float where an int belongs, an over-long ``intent``) and raises
:class:`DecisionError` for anything that cannot be repaired without inventing
a creative choice (unknown enum values, missing keys).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import jsonschema

__all__ = [
    "DecisionError",
    "SCHEMA_PATH",
    "load_schema",
    "SCENES",
    "PALETTES",
    "PARTICLE_MODES",
    "TRANSITION_MODES",
    "INTENT_MAX_CHARS",
    "validate_and_clamp",
]


class DecisionError(ValueError):
    """A director decision could not be repaired into a valid one."""


def _find_schema() -> Path:
    """Locate ``director_schema.json``.

    Order: ``$AMV_SCHEMA`` override, repo root (one level above the package),
    then next to this module (for a flattened install).
    """
    env = os.environ.get("AMV_SCHEMA")
    if env:
        return Path(env).expanduser()
    here = Path(__file__).resolve().parent
    for candidate in (here.parent / "director_schema.json", here / "director_schema.json"):
        if candidate.is_file():
            return candidate
    # Nothing found: return the canonical location so the error message is useful.
    return here.parent / "director_schema.json"


SCHEMA_PATH: Path = _find_schema()

_SCHEMA_CACHE: dict[str, Any] | None = None


def load_schema(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Return the decision JSON Schema as a dict (cached for the default path)."""
    global _SCHEMA_CACHE
    if path is not None:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    if _SCHEMA_CACHE is None:
        try:
            with open(SCHEMA_PATH, encoding="utf-8") as fh:
                _SCHEMA_CACHE = json.load(fh)
        except FileNotFoundError as exc:  # pragma: no cover - install accident
            raise DecisionError(f"director schema not found at {SCHEMA_PATH}") from exc
    return _SCHEMA_CACHE


_SCHEMA = load_schema()
_PROPS = _SCHEMA["properties"]

SCENES: tuple[str, ...] = tuple(_PROPS["scene"]["enum"])
PALETTES: tuple[str, ...] = tuple(_PROPS["palette"]["enum"])
PARTICLE_MODES: tuple[str, ...] = tuple(_PROPS["particle_mode"]["enum"])
TRANSITION_MODES: tuple[str, ...] = tuple(
    _PROPS["transition"]["properties"]["mode"]["enum"]
)
INTENT_MAX_CHARS: int = int(_PROPS["intent"]["maxLength"])


def _coerce_number(value: Any, *, where: str, want_int: bool) -> float | int:
    if isinstance(value, bool):
        raise DecisionError(f"{where}: expected a number, got boolean {value!r}")
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        try:
            num = float(value.strip())
        except ValueError as exc:
            raise DecisionError(f"{where}: cannot read {value!r} as a number") from exc
    else:
        raise DecisionError(f"{where}: expected a number, got {type(value).__name__}")
    if num != num or num in (float("inf"), float("-inf")):
        raise DecisionError(f"{where}: {value!r} is not a finite number")
    return int(round(num)) if want_int else num


def _clamp(num: float, node: dict[str, Any]) -> float:
    lo = node.get("minimum")
    hi = node.get("maximum")
    if lo is not None and num < lo:
        num = lo
    if hi is not None and num > hi:
        num = hi
    return num


def _coerce_object(value: Any, node: dict[str, Any], *, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DecisionError(f"{where}: expected an object, got {type(value).__name__}")
    props: dict[str, Any] = node["properties"]
    out: dict[str, Any] = {}
    for key in node.get("required", list(props)):
        if key not in value:
            raise DecisionError(f"{where}.{key}: missing required key".lstrip("."))
        out[key] = _coerce(value[key], props[key], where=f"{where}.{key}".lstrip("."))
    # Extra keys are dropped rather than fatal: additionalProperties is false in
    # the schema, and during a show a stray key is not worth losing a decision.
    return out


def _coerce(value: Any, node: dict[str, Any], *, where: str) -> Any:
    kind = node.get("type")
    if kind == "object":
        return _coerce_object(value, node, where=where)
    if "enum" in node:
        if not isinstance(value, str):
            raise DecisionError(f"{where}: expected one of {node['enum']}, got {value!r}")
        text = value.strip()
        if text not in node["enum"]:
            raise DecisionError(f"{where}: unknown value {value!r}; expected one of {node['enum']}")
        return text
    if kind == "integer":
        return int(_clamp(_coerce_number(value, where=where, want_int=True), node))
    if kind == "number":
        return float(_clamp(_coerce_number(value, where=where, want_int=False), node))
    if kind == "string":
        if not isinstance(value, str):
            raise DecisionError(f"{where}: expected a string, got {type(value).__name__}")
        limit = node.get("maxLength")
        return value[:limit] if limit is not None else value
    return value  # pragma: no cover - no other types in this schema


def validate_and_clamp(d: dict) -> dict:
    """Repair and validate one director decision.

    Coerces numeric strings, clamps numbers into their schema range, rounds
    integers, truncates ``intent`` to 120 characters and drops unknown keys.
    Raises :class:`DecisionError` for missing keys or unknown enum values, and
    validates the repaired dict against the schema before returning it.
    """
    if not isinstance(d, dict):
        raise DecisionError(f"decision must be an object, got {type(d).__name__}")
    schema = load_schema()
    out = _coerce_object(d, schema, where="")
    try:
        jsonschema.validate(out, schema)
    except jsonschema.ValidationError as exc:  # pragma: no cover - belt and braces
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise DecisionError(f"decision failed schema validation at {path}: {exc.message}") from exc
    return out
