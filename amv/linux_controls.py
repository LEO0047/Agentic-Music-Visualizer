"""Frame-timed controls for the standalone Linux renderer, with no TD runtime.

This is a bounded port of the director's *control contract*, not a TouchDesigner
emulator. It owns no socket, thread, GL object, audio device, or wall clock. The
renderer drains OSC messages and calls :meth:`LinuxControlState.update` on its
own render thread, using its actual frame timestamp and an audio kick pulse.

Complete director batches must follow ``DIRECTOR_ADDRESSES`` exactly. Targets
become visible only on the final heartbeat; malformed or incomplete batches
cannot partially change a look. A heartbeat by itself is allowed for liveness.
UDP has no transaction identifier, so this cannot detect a perfectly ordered
splice from multiple senders: use one director sender, as the existing client
does. This adapter does not add reliability to UDP.

Floats glide over ``beats * 60 / bpm`` seconds (smoothstep, independent of frame
rate). Discrete glide transitions wait for a kick, or at most two seconds, then
crossfade. ``on_next_kick`` instead cuts discretes at that boundary. ``cut`` is
immediate for everything. Scene/palette/particle/symmetry weight distributions
preserve the exact image mixture when a glide is interrupted. The renderer must
use these weights, not interpolate enum indices. The endpoint convenience
fields describe an ordinary two-way glide; weights are authoritative for a
multi-way interrupted glide.

A manual edit cuts its field immediately, cancels its outstanding AI targets,
and blocks subsequent AI writes to that field for 30 seconds. Entering whole
manual mode additionally stops all active glides at their current values and
discards pending targets/drop plans. Leaving it never replays suppressed AI.
A drop plan cuts once, on a kick during the ``drop`` section, taking precedence
over queued changes while respecting every manual freeze.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from .osc_io import DIRECTOR_ADDRESSES
from .schema import DecisionError, load_schema, validate_and_clamp
from .sections import SECTIONS

__all__ = ["LinuxControlState", "FrameState", "FREEZE_SECONDS", "PENDING_MAX_WAIT"]

FREEZE_SECONDS = 30.0
PENDING_MAX_WAIT = 2.0
HEARTBEAT_TIMEOUT = 45.0
DISCRETE_FIELDS = ("scene", "palette", "symmetry", "particle_mode")
CONTINUOUS_FIELDS = ("feedback", "camera_speed", "projectm_mix")
MODES = ("gpt", "rule", "manual")
_PROPS = load_schema()["properties"]
_DEFAULTS = {
    "scene": "tunnel", "palette": "violet_cyan", "feedback": 0.0,
    "symmetry": 6, "camera_speed": 0.25, "particle_mode": "spiral",
    "projectm_mix": 0.0, "transition_mode": "glide", "transition_beats": 2,
}
Weight = tuple[str | int, float]


def _smoothstep(elapsed: float, duration: float) -> float:
    p = 1.0 if duration <= 0 else max(0.0, min(1.0, elapsed / duration))
    return p * p * (3.0 - 2.0 * p)


def _coerce(value: Any, node: dict[str, Any], field: str) -> Any:
    """Use schema enums/bounds and the same defensive numeric policy as core."""
    if "enum" in node:
        if not isinstance(value, str) or value.strip() not in node["enum"]:
            raise ValueError(f"{field}: unknown enum value {value!r}")
        return value.strip()
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{field}: expected a finite number")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{field}: expected a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field}: expected a finite number")
    if node.get("type") == "integer":
        number = round(number)
    number = min(node.get("maximum", number), max(node.get("minimum", number), number))
    return int(number) if node.get("type") == "integer" else float(number)


def _drop(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        if len(value) > 4096:
            raise ValueError("on_drop: JSON is too large")
        try:
            value = json.loads(value)
        except (ValueError, RecursionError) as exc:
            raise ValueError("on_drop: expected a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("on_drop: expected an object")
    props = _PROPS["on_drop"]["properties"]
    if any(key not in value for key in props):
        raise ValueError("on_drop: missing required fields")
    return {key: _coerce(value[key], node, f"on_drop.{key}") for key, node in props.items()}


def _wire_value(field: str, value: Any) -> Any:
    if field == "on_drop":
        return _drop(value)
    if field in ("transition_mode", "transition_beats"):
        return _coerce(value, _PROPS["transition"]["properties"][field[11:]], field)
    if field not in DISCRETE_FIELDS + CONTINUOUS_FIELDS:
        raise ValueError(f"unknown control field {field!r}")
    return _coerce(value, _PROPS[field], field)


@dataclass(frozen=True)
class FrameState:
    """Immutable render snapshot; enum weights are nonnegative and sum to one."""

    scene_from: str
    scene_to: str
    scene_mix: float
    scene_weights: tuple[Weight, ...]
    palette_from: str
    palette_to: str
    palette_mix: float
    palette_weights: tuple[Weight, ...]
    particle_mode_from: str
    particle_mode_to: str
    particle_mode_mix: float
    particle_mode_weights: tuple[Weight, ...]
    symmetry_from: int
    symmetry_to: int
    symmetry_mix: float
    symmetry_weights: tuple[Weight, ...]
    feedback: float
    camera_speed: float
    projectm_mix: float
    mode: str
    section: str
    heartbeat: int | None
    heartbeat_age: float
    heartbeat_stale: bool
    pending_fields: tuple[str, ...]
    frozen_fields: tuple[str, ...]
    drop_armed: bool
    drop_count: int

    @property
    def scene(self) -> str:
        return str(max(self.scene_weights, key=lambda item: item[1])[0])

    @property
    def palette(self) -> str:
        return str(max(self.palette_weights, key=lambda item: item[1])[0])

    @property
    def particle_mode(self) -> str:
        return str(max(self.particle_mode_weights, key=lambda item: item[1])[0])

    @property
    def symmetry(self) -> int:
        return int(max(self.symmetry_weights, key=lambda item: item[1])[0])


@dataclass
class _FloatGlide:
    start: float
    target: float
    at: float
    duration: float

    def value(self, now: float) -> float:
        return self.start + (self.target - self.start) * _smoothstep(now - self.at, self.duration)


@dataclass
class _Blend:
    start: tuple[Weight, ...]
    target: str | int | None = None
    at: float = 0.0
    duration: float = 0.0

    def weights(self, now: float) -> tuple[Weight, ...]:
        if self.target is None:
            return self.start
        mix = _smoothstep(now - self.at, self.duration)
        if mix >= 1.0:
            return ((self.target, 1.0),)
        weights = {value: weight * (1.0 - mix) for value, weight in self.start}
        weights[self.target] = weights.get(self.target, 0.0) + mix
        return tuple((value, weight) for value, weight in weights.items() if weight > 0.0)

    def frame(self, now: float) -> tuple[Any, Any, float, tuple[Weight, ...]]:
        weights = self.weights(now)
        if len(weights) == 1:
            value = weights[0][0]
            # At the exact start, keep the upcoming endpoint visible to renderers.
            if self.target is not None and self.target != value and now <= self.at:
                return value, self.target, 0.0, weights
            return value, value, 1.0, weights
        source = max(self.start, key=lambda item: item[1])[0]
        target = self.target if self.target is not None else max(weights, key=lambda item: item[1])[0]
        mix = _smoothstep(now - self.at, self.duration) if self.target is not None else 0.0
        return source, target, mix, weights


@dataclass(frozen=True)
class _Pending:
    value: str | int
    at: float
    mode: str
    duration: float


class LinuxControlState:
    """Single-render-thread, deterministic OSC control state.

    ``receive`` returns whether one message was accepted (staging counts).
    Malformed network messages return False and set ``last_error`` without
    throwing. ``update`` and the explicit manual methods reject invalid caller
    arguments with ValueError. All methods take finite, monotonic seconds from
    the same caller-owned clock; identical timestamps are valid.
    """

    def __init__(self, bpm: float = 145.0) -> None:
        if isinstance(bpm, bool):
            raise ValueError("bpm must be finite and positive")
        try:
            bpm = float(bpm)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("bpm must be finite and positive") from exc
        if not math.isfinite(bpm) or bpm <= 0.0 or not math.isfinite(960.0 / bpm):
            raise ValueError("bpm must be finite and positive")
        self.bpm = bpm
        self.mode = "rule"
        self.section = "steady"
        self.transition_mode = str(_DEFAULTS["transition_mode"])
        self.transition_beats = int(_DEFAULTS["transition_beats"])
        self.heartbeat: int | None = None
        self.accepted_batches = 0
        self.applied_batches = 0
        self.rejected_batches = 0
        self.ignored_messages = 0
        self.drop_count = 0
        self.last_error: str | None = None
        self._last_time: float | None = None
        self._epoch: float | None = None
        self._heartbeat_at: float | None = None
        self._staged: dict[str, Any] = {}
        self._next_address = 0
        self._continuous = {key: float(_DEFAULTS[key]) for key in CONTINUOUS_FIELDS}
        self._glides: dict[str, _FloatGlide] = {}
        self._discrete = {key: _Blend(((_DEFAULTS[key], 1.0),)) for key in DISCRETE_FIELDS}
        self._pending: dict[str, _Pending] = {}
        self._touches: dict[str, float] = {}
        self._drop_plan: dict[str, str] | None = None
        self._kick_high = False
        self._mode_before_manual = "rule"

    def _time(self, now: float) -> float:
        if isinstance(now, bool):
            raise ValueError("now must be finite monotonic seconds")
        try:
            value = float(now)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("now must be finite monotonic seconds") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError("now must be finite nonnegative seconds")
        if self._last_time is not None and value < self._last_time:
            raise ValueError("now must not move backwards")
        self._last_time = value
        if self._epoch is None:
            self._epoch = value
        return value

    def _reset_batch(self) -> None:
        self._staged.clear()
        self._next_address = 0

    def _fail(self, message: str, *, abort_batch: bool = True) -> bool:
        self.last_error = message
        self.ignored_messages += 1
        if abort_batch and self._staged:
            self.rejected_batches += 1
            self._reset_batch()
        return False

    def is_frozen(self, field: str, now: float) -> bool:
        return self.mode == "manual" or now < self._touches.get(field, -math.inf) + FREEZE_SECONDS

    def receive(self, address: str, value: Any, now: float) -> bool:
        """Route one queued OSC message on the main render thread."""
        try:
            now = self._time(now)
            if not isinstance(address, str):
                return self._fail("address must be a string")
            if address == "/feat/section":
                self._set_section(value)
                return True
            if address == "/manual/mode":
                self.set_manual_mode(value, now)
                return True
            if address.startswith("/manual/"):
                self.set_manual(address[len("/manual/"):], value, now)
                return True
            if address not in DIRECTOR_ADDRESSES:
                return self._fail(f"unknown OSC address {address!r}", abort_batch=False)
            if address == DIRECTOR_ADDRESSES[0]:
                if self._staged:
                    self.rejected_batches += 1
                self._reset_batch()
            if address == DIRECTOR_ADDRESSES[-1]:
                return self._heartbeat(value, now)
            if address != DIRECTOR_ADDRESSES[self._next_address]:
                return self._fail(f"out-of-order director message {address!r}")
            field = address.rsplit("/", 1)[-1]
            self._staged[field] = _wire_value(field, value)
            self._next_address += 1
            return True
        except (ValueError, TypeError, OverflowError) as exc:
            return self._fail(str(exc))

    def _heartbeat(self, value: Any, now: float) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return self._fail("heartbeat must be a nonnegative integer")
        if not math.isfinite(value) or value < 0 or int(value) != value:
            return self._fail("heartbeat must be a nonnegative integer")
        # Health telemetry is permitted even while all AI controls are frozen.
        self.heartbeat = int(value)
        self._heartbeat_at = now
        if not self._staged:
            return True
        if self._next_address != len(DIRECTOR_ADDRESSES) - 1:
            return self._fail("heartbeat received before complete director batch")
        staged = dict(self._staged)
        self._reset_batch()
        try:
            decision = validate_and_clamp({
                **{key: staged[key] for key in DISCRETE_FIELDS + CONTINUOUS_FIELDS},
                "transition": {"mode": staged["transition_mode"], "beats": staged["transition_beats"]},
                "on_drop": staged["on_drop"], "intent": "",
            })
        except DecisionError as exc:  # Additional protection if the schema grows.
            self.rejected_batches += 1
            return self._fail(str(exc))
        self.accepted_batches += 1
        if self.mode != "manual":
            self._apply_decision(decision, now)
            self.applied_batches += 1
        return True

    def _apply_decision(self, decision: dict[str, Any], now: float) -> None:
        transition = decision["transition"]
        if not self.is_frozen("transition_mode", now):
            self.transition_mode = transition["mode"]
        if not self.is_frozen("transition_beats", now):
            self.transition_beats = transition["beats"]
        duration = self.transition_beats * 60.0 / self.bpm
        for field in CONTINUOUS_FIELDS:
            if self.is_frozen(field, now):
                continue
            current = self._float_value(field, now)
            target = decision[field]
            if self.transition_mode == "cut":
                self._continuous[field] = target
                self._glides.pop(field, None)
            else:
                self._glides[field] = _FloatGlide(current, target, now, duration)
        for field in DISCRETE_FIELDS:
            if self.is_frozen(field, now):
                continue
            if self.transition_mode == "cut":
                self._set_discrete(field, decision[field], now, 0.0)
                self._pending.pop(field, None)
            else:
                # A fresh target supersedes the older queued target for this field.
                # Keep the original deadline: frequent director updates during
                # a kickless passage must not postpone the fallback forever.
                at = self._pending[field].at if field in self._pending else now
                self._pending[field] = _Pending(decision[field], at, self.transition_mode, duration)
        if not self.is_frozen("on_drop", now):
            self._drop_plan = {key: value for key, value in decision["on_drop"].items()
                               if not self.is_frozen(key, now)} or None

    def _float_value(self, field: str, now: float) -> float:
        glide = self._glides.get(field)
        if glide is None:
            return self._continuous[field]
        if now >= glide.at + glide.duration:
            self._continuous[field] = glide.target
            self._glides.pop(field, None)
            return glide.target
        return glide.value(now)

    def _set_discrete(self, field: str, value: str | int, now: float, duration: float) -> None:
        if duration <= 0:
            self._discrete[field] = _Blend(((value, 1.0),))
        else:
            self._discrete[field] = _Blend(self._discrete[field].weights(now), value, now, duration)

    def set_manual(self, field: str, value: Any, now: float) -> None:
        """Cut one user-edited field now; do not resurrect earlier AI targets."""
        value = _wire_value(field, value)
        now = self._time(now)
        self._touches[field] = now
        self._pending.pop(field, None)
        if self._drop_plan is not None:
            self._drop_plan.pop(field, None)
        if field in CONTINUOUS_FIELDS:
            self._continuous[field] = value
            self._glides.pop(field, None)
        elif field in DISCRETE_FIELDS:
            self._set_discrete(field, value, now, 0.0)
        elif field == "transition_mode":
            self.transition_mode = value
        elif field == "transition_beats":
            self.transition_beats = value
        elif field == "on_drop":
            self._drop_plan = value

    def set_manual_mode(self, enabled: bool | int | str, now: float) -> None:
        """Select gpt/rule/manual; booleans toggle manual vs the previous mode."""
        if isinstance(enabled, str) and enabled in MODES:
            mode = enabled
        elif enabled is True or type(enabled) is int and enabled == 1:
            mode = "manual"
        elif enabled is False or type(enabled) is int and enabled == 0:
            mode = self._mode_before_manual
        else:
            raise ValueError("manual mode must be a boolean, 0/1, or gpt/rule/manual")
        now = self._time(now)
        if mode == self.mode:
            return
        if mode == "manual":
            self._mode_before_manual = self.mode
            for field in CONTINUOUS_FIELDS:
                self._continuous[field] = self._float_value(field, now)
            self._glides.clear()
            for field in DISCRETE_FIELDS:
                self._discrete[field] = _Blend(self._discrete[field].weights(now))
            self._pending.clear()
            self._drop_plan = None
        self.mode = mode
        # A transaction spanning a mode switch cannot silently be replayed.
        self._reset_batch()

    def _set_section(self, value: Any) -> None:
        if not isinstance(value, str) or value not in SECTIONS:
            raise ValueError(f"unknown section {value!r}")
        self.section = value

    def update(self, now: float, kick: bool = False, section: str | None = None) -> FrameState:
        """Advance controls at an actual audio/render timestamp and take a snapshot.

        ``kick`` is a boolean pulse or held detector level; only its rising edge
        triggers queued changes/drop execution. Supplying None for section keeps
        the last section received from OSC. There is no hidden frame-count clock.
        """
        if kick not in (False, True, 0, 1):
            raise ValueError("kick must be a boolean pulse")
        if section is not None and (not isinstance(section, str) or section not in SECTIONS):
            raise ValueError(f"unknown section {section!r}")
        now = self._time(now)
        if section is not None:
            self._set_section(section)
        edge = bool(kick) and not self._kick_high
        self._kick_high = bool(kick)
        if self.mode != "manual":
            if edge and self.section == "drop" and self._drop_plan:
                plan, self._drop_plan = self._drop_plan, None
                for field, value in plan.items():
                    self._pending.pop(field, None)
                    if not self.is_frozen(field, now):
                        self._set_discrete(field, value, now, 0.0)
                self.drop_count += 1
            for field, pending in list(self._pending.items()):
                if self.is_frozen(field, now):
                    self._pending.pop(field, None)
                elif edge or now - pending.at >= PENDING_MAX_WAIT:
                    duration = pending.duration if pending.mode == "glide" else 0.0
                    self._set_discrete(field, pending.value, now, duration)
                    self._pending.pop(field, None)
        values: dict[str, Any] = {}
        for field in DISCRETE_FIELDS:
            source, target, mix, weights = self._discrete[field].frame(now)
            values.update({f"{field}_from": source, f"{field}_to": target,
                           f"{field}_mix": mix, f"{field}_weights": weights})
        values.update({field: self._float_value(field, now) for field in CONTINUOUS_FIELDS})
        origin = self._heartbeat_at if self._heartbeat_at is not None else self._epoch
        age = max(0.0, now - (origin if origin is not None else now))
        return FrameState(
            **values, mode=self.mode, section=self.section, heartbeat=self.heartbeat,
            heartbeat_age=age, heartbeat_stale=age > HEARTBEAT_TIMEOUT,
            pending_fields=tuple(self._pending),
            frozen_fields=tuple(field for field in self._touches if self.is_frozen(field, now)),
            drop_armed=bool(self._drop_plan), drop_count=self.drop_count,
        )
