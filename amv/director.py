"""The director layer — rule first, then GPT (SPEC §2, §3.2, §5, Phase 4).

Three things live here, in the order they were built:

* :class:`RuleDirector` — a deterministic state machine that turns a section
  plus a feature summary into a schema-valid decision. It is written first
  because it is the fallback: if Codex is out of quota, offline, or slow, this
  is what keeps the screen moving, so it has to be good enough to run a set on
  its own rather than being an apology.
* :class:`GPTDirector` — the same signature, backed by one ``codex exec`` per
  decision. Any failure at all (``CodexError``, a stray ``OSError``, a bug in
  this file) falls through to the rule director and is *recorded* in
  ``_source`` rather than being swallowed, because "the GPT path quietly died
  forty minutes ago" is the failure mode that ruins a show.
* :class:`DirectorLoop` — the thing the sidecar actually calls. It owns the
  timing (period, event triggers, one-in-flight), the worker thread, the OSC
  publish, the visual history and the log.

Two rules shape everything else:

**The 10 Hz loop must never wait.** A decision costs 13 s of wall clock
(SPEC §7), the sidecar ticks every 100 ms, so :meth:`DirectorLoop.on_tick`
starts a worker thread and returns immediately. ``worker=False`` runs it inline
and exists for tests.

**Variety is enforced, not requested.** The prompt asks GPT not to repeat
itself; :func:`enforce_variety` makes sure it did not, on both paths. Anything
that reaches TouchDesigner has been through
:func:`~amv.schema.validate_and_clamp`.

Keys beginning with ``_`` (``_source``, ``_latency_s``) are this module's own
bookkeeping. They travel with the decision through the loop and are stripped by
:func:`strip_private` before anything is sent to TD or written to the log's
``decision`` field.
"""

from __future__ import annotations

import json
import random
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Iterable, Sequence, TextIO

from .osc_io import TDClient
from .schema import (
    PALETTES,
    PARTICLE_MODES,
    SCENES,
    validate_and_clamp,
)

__all__ = [
    "SYSTEM_PROMPT",
    "History",
    "RuleDirector",
    "GPTDirector",
    "DirectorLoop",
    "Hotkeys",
    "build_prompt",
    "enforce_variety",
    "strip_private",
    "REPEAT_WINDOW_S",
    "FORCE_SCENE_AFTER",
    "MAX_PROMPT_CHARS",
    "HISTORY_LEN",
    "MODES",
    "DEFAULT_BPM",
]

#: The same combination of scene + palette may not come back inside this many
#: seconds (SPEC §7, "決策風格單調" → 60 秒內不重複).
REPEAT_WINDOW_S = 60.0

#: After this many consecutive decisions on one scene, the next one is forced
#: onto a different scene whatever the director asked for.
FORCE_SCENE_AFTER = 10

#: Visual history handed to the prompt, newest first.
HISTORY_LEN = 8

#: Hard ceiling on the assembled prompt. The Codex system prompt already costs
#: ~25k tokens per call (SPEC §2); ours is not where the budget should go, and
#: a prompt that grows with the set length is a slow leak. Oldest history
#: entries are dropped until the prompt fits.
MAX_PROMPT_CHARS = 2000

#: Used when the feature stream has no usable kick rate to infer tempo from.
DEFAULT_BPM = 145.0

#: Director modes (SPEC §5). ``manual`` still heartbeats so TD stays quiet.
MODES: tuple[str, ...] = ("gpt", "rule", "manual")

#: Sections whose arrival is worth an out-of-band decision.
EVENT_SECTIONS: frozenset[str] = frozenset({"build", "drop", "breakdown"})

#: Particle modes ordered by how much energy they read as, for repairing a
#: repeated ``on_drop``. Every entry must be a schema enum value.
DROP_PARTICLE_ORDER: tuple[str, ...] = tuple(
    m for m in ("burst", "spiral", "orbit", "rain", "none") if m in PARTICLE_MODES
)


SYSTEM_PROMPT = """你是一場 Psytrance 現場的視覺導演，管理整場演出的構圖節奏。只輸出符合 schema 的 JSON，不要任何說明文字。
規則：
- 同一組 scene+palette 60 秒內不重複；連續三次決策至少換一次 scene。
- section=build 時逐步加 feedback 與 symmetry、壓低 camera_speed，把峰值留給 drop。
- section=drop 時給出全場最強的一擊：particle_mode=burst，transition 用 cut。
- section=breakdown 時降低複雜度：particle_mode=none 或 rain，feedback ≤ 0.5，projectm_mix ≤ 0.2。
- 每次都要填 on_drop，且不能跟上一次的 on_drop 相同策略。
- transition：段落內用 glide 8–16 beats；段落切換用 on_next_kick。
- intent 用 120 字以內說明這次決策為什麼，會被記錄。"""


# -- helpers ----------------------------------------------------------------


def strip_private(decision: dict) -> dict:
    """The decision without this module's ``_``-prefixed bookkeeping keys."""
    return {k: v for k, v in decision.items() if not k.startswith("_")}


def _private(decision: dict) -> dict:
    return {k: v for k, v in decision.items() if k.startswith("_")}


def _on_drop_key(on_drop: dict | None) -> tuple[str, str, str]:
    """A hashable identity for one ``on_drop`` plan."""
    d = on_drop or {}
    return (str(d.get("scene", "")), str(d.get("palette", "")), str(d.get("particle_mode", "")))


def _rotate(values: Sequence[str], index: int) -> str:
    return values[index % len(values)]


class History:
    """The last :data:`HISTORY_LEN` decisions, newest first.

    Entries are ``{"scene", "palette", "t", "source", "on_drop"}``. ``duration``
    is deliberately *not* stored: how long a look was on screen is only known
    once the next decision lands (and for the newest entry, only relative to
    "now"), so :meth:`records` computes it on the fly.

    ``since_scene_change`` is a counter rather than something derived from the
    deque, because the deque only holds eight entries and the force-a-change
    rule fires at ten.
    """

    def __init__(self, maxlen: int = HISTORY_LEN) -> None:
        self.entries: Deque[dict] = deque(maxlen=maxlen)
        self.since_scene_change = 0
        self.count = 0

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)

    # -- writing ------------------------------------------------------------

    def append(self, decision: dict, now: float, source: str = "rule") -> dict:
        """Record one published decision. Newest ends up at index 0."""
        scene = decision["scene"]
        previous = self.entries[0]["scene"] if self.entries else None
        entry = {
            "scene": scene,
            "palette": decision["palette"],
            "t": float(now),
            "source": source,
            "on_drop": dict(decision.get("on_drop") or {}),
        }
        self.entries.appendleft(entry)
        self.count += 1
        self.since_scene_change = 1 if scene != previous else self.since_scene_change + 1
        return entry

    def clear(self) -> None:
        self.entries.clear()
        self.since_scene_change = 0
        self.count = 0

    # -- reading ------------------------------------------------------------

    @property
    def current_scene(self) -> str | None:
        return self.entries[0]["scene"] if self.entries else None

    def last_on_drop(self) -> dict | None:
        """The most recent ``on_drop`` plan, or ``None`` on an empty history."""
        return self.entries[0]["on_drop"] if self.entries else None

    def last_use(self, scene: str, palette: str, now: float) -> float | None:
        """Seconds since this exact scene+palette pair was last on screen.

        ``None`` means "never used", which is different from "used a very long
        time ago" only in that callers do not have to invent a sentinel.
        """
        for entry in self.entries:
            if entry["scene"] == scene and entry["palette"] == palette:
                return float(now) - entry["t"]
        return None

    def age_of(self, field: str, value: str, now: float) -> float:
        """Seconds since ``field`` last held ``value``; ``inf`` if never."""
        for entry in self.entries:
            if entry.get(field) == value:
                return float(now) - entry["t"]
        return float("inf")

    def decisions_since_scene_change(self) -> int:
        """Decisions in a row on the current scene, counting the one that
        introduced it — so ``1`` immediately after a change and ``0`` on an
        empty history. At :data:`FORCE_SCENE_AFTER` the next decision is moved
        off that scene, which caps a run at exactly that many."""
        return self.since_scene_change

    def records(self, now: float) -> list[dict]:
        """Prompt-shaped history: newest first, with ``ago_s`` and ``duration_s``.

        ``duration_s`` is how long that look actually held: for the newest
        entry, up to now; for the others, until the entry that replaced it.
        """
        out: list[dict] = []
        newer_t = float(now)
        for entry in self.entries:
            out.append(
                {
                    "scene": entry["scene"],
                    "palette": entry["palette"],
                    "ago_s": round(float(now) - entry["t"], 1),
                    "duration_s": round(newer_t - entry["t"], 1),
                    "source": entry["source"],
                    "on_drop": "/".join(_on_drop_key(entry["on_drop"])),
                }
            )
            newer_t = entry["t"]
        return out


# -- anti-repetition --------------------------------------------------------


def _least_recently_used(
    candidates: Iterable[str],
    field: str,
    history: History,
    now: float,
    order: Sequence[str],
) -> list[str]:
    """``candidates`` sorted oldest-use first, ties broken by schema order."""
    index = {value: i for i, value in enumerate(order)}
    return sorted(
        candidates,
        key=lambda value: (-history.age_of(field, value, now), index.get(value, len(order))),
    )


def enforce_variety(decision: dict, history: History, now: float) -> dict:
    """Make one decision obey the anti-repetition rules, then validate it.

    Three repairs, in the order the rules were written:

    1. scene+palette used within :data:`REPEAT_WINDOW_S` → rotate the palette
       to the least recently used one that clears the window; if every palette
       is stale for this scene, rotate the scene instead.
    2. :data:`FORCE_SCENE_AFTER` decisions on the same scene → force a
       different one, whatever the director asked for.
    3. ``on_drop`` identical to the previous one → change its particle mode
       (and its palette if that was not enough) so the drop is not a re-run.

    Always returns a :func:`~amv.schema.validate_and_clamp`-ed decision, with
    any ``_``-prefixed bookkeeping keys carried across untouched.
    """
    private = _private(decision)
    out = dict(strip_private(decision))

    # 1. no repeat inside the window.
    used = history.last_use(out["scene"], out["palette"], now)
    if used is not None and used < REPEAT_WINDOW_S:
        scene = out["scene"]
        for palette in _least_recently_used(PALETTES, "palette", history, now, PALETTES):
            age = history.last_use(scene, palette, now)
            if age is None or age >= REPEAT_WINDOW_S:
                out["palette"] = palette
                break
        else:
            palette = out["palette"]
            for candidate in _least_recently_used(SCENES, "scene", history, now, SCENES):
                age = history.last_use(candidate, palette, now)
                if age is None or age >= REPEAT_WINDOW_S:
                    out["scene"] = candidate
                    break

    # 2. never sit on one scene forever.
    current = history.current_scene
    if (
        current is not None
        and history.decisions_since_scene_change() >= FORCE_SCENE_AFTER
        and out["scene"] == current
    ):
        others = [s for s in SCENES if s != current]
        if others:
            out["scene"] = _least_recently_used(others, "scene", history, now, SCENES)[0]

    # 3. a drop you have already seen is not a drop.
    previous_drop = history.last_on_drop()
    if previous_drop and _on_drop_key(out.get("on_drop")) == _on_drop_key(previous_drop):
        plan = dict(out.get("on_drop") or {})
        # Preference order, not schema order: a drop that loses its particles
        # is a worse fix than a drop that changes them.
        plan["particle_mode"] = next(
            m for m in DROP_PARTICLE_ORDER if m != plan.get("particle_mode")
        )
        if _on_drop_key(plan) == _on_drop_key(previous_drop):  # pragma: no cover - unreachable
            palettes = [p for p in PALETTES if p != plan.get("palette")]
            plan["palette"] = _least_recently_used(palettes, "palette", history, now, PALETTES)[0]
        out["on_drop"] = plan

    out = validate_and_clamp(out)
    out.update(private)
    return out


# -- the rule director ------------------------------------------------------


class RuleDirector:
    """Deterministic director: section in, schema-valid decision out.

    This is the fallback that has to survive an hour without Codex, so it is a
    state machine rather than a random walk: each section has a target shape
    (SPEC §3.2 and the Phase 4 notes) and the seeded RNG only jitters within
    that shape. Same seed, same section sequence, same decisions — which is
    what makes it testable and what makes a rehearsal reproducible.
    """

    #: Per-section plan. Ranges are (low, high) and inclusive for integers.
    PLANS: dict[str, dict[str, Any]] = {
        "build": {
            "scenes": ("tunnel", "fractal_temple", "kaleido_mesh"),
            "feedback": (0.70, 0.90),
            "symmetry": (8, 12),
            "camera_speed": (0.20, 0.40),
            "particles": ("spiral", "orbit"),
            "projectm_mix": (0.0, 0.25),
            "transition": ("glide", 16),
            "intent": "build：堆高 feedback 與對稱、壓低鏡頭速度，峰值留給 drop",
        },
        "drop": {
            "scenes": ("kaleido_mesh", "fractal_temple", "particle_field"),
            "feedback": (0.50, 0.78),
            "symmetry": (6, 12),
            "camera_speed": (0.60, 0.95),
            "particles": ("burst",),
            "projectm_mix": (0.0, 0.35),
            "transition": ("cut", 1),
            "intent": "drop：直接切換、粒子爆開、鏡頭全速，把積蓄的張力一次放掉",
        },
        "breakdown": {
            "scenes": ("tunnel", "projectm_blend", "particle_field"),
            "feedback": (0.20, 0.45),
            "symmetry": (1, 4),
            "camera_speed": (0.05, 0.25),
            "particles": ("none", "rain"),
            "projectm_mix": (0.0, 0.20),
            "transition": ("glide", 8),
            "intent": "breakdown：降低複雜度、粒子收乾、projectM 壓到最低，留白等下一次抬升",
        },
        "steady": {
            "scenes": SCENES,
            "feedback": (0.35, 0.60),
            "symmetry": (3, 8),
            "camera_speed": (0.30, 0.60),
            "particles": ("spiral", "orbit", "rain"),
            "projectm_mix": (0.0, 0.30),
            "transition": ("glide", 12),
            "intent": "steady：輪替場景與色盤維持新鮮度，參數留在中段等下一個段落",
        },
    }

    #: Scenes and particle modes a pre-decided drop is allowed to use.
    DROP_SCENES: tuple[str, ...] = ("kaleido_mesh", "fractal_temple", "particle_field", "tunnel")
    DROP_PARTICLES: tuple[str, ...] = ("burst", "spiral", "orbit")

    def __init__(self, seed: int = 20260908) -> None:
        self.seed = int(seed)
        self.rng = random.Random(self.seed)
        self.n = 0

    def reset(self) -> None:
        """Rewind the RNG and the rotation counter."""
        self.rng = random.Random(self.seed)
        self.n = 0

    def decide(
        self,
        summary: dict,
        section: str,
        history: History,
        now: float,
        bpm: float | None = None,
    ) -> dict:
        """One decision for ``section``, already past :func:`enforce_variety`."""
        started = time.monotonic()
        plan = self.PLANS.get(section, self.PLANS["steady"])
        n = self.n
        self.n += 1
        energy = float(summary.get("energy", 0.5) or 0.0)

        # Energy nudges the two parameters the eye reads fastest; the section
        # plan still decides the shape. validate_and_clamp catches the edges.
        lo, hi = plan["feedback"]
        feedback = self.rng.uniform(lo, hi) + (energy - 0.5) * 0.08
        lo, hi = plan["camera_speed"]
        camera_speed = self.rng.uniform(lo, hi) + (energy - 0.5) * 0.05
        mode, beats = plan["transition"]

        decision = {
            "scene": _rotate(plan["scenes"], n),
            "palette": _rotate(PALETTES, n),
            "feedback": round(feedback, 3),
            "symmetry": self.rng.randint(*plan["symmetry"]),
            "camera_speed": round(camera_speed, 3),
            "particle_mode": _rotate(plan["particles"], n),
            "projectm_mix": round(self.rng.uniform(*plan["projectm_mix"]), 3),
            "transition": {"mode": mode, "beats": beats},
            "on_drop": {
                "scene": _rotate(self.DROP_SCENES, n + 1),
                "palette": _rotate(PALETTES, n + 2),
                "particle_mode": _rotate(self.DROP_PARTICLES, n),
            },
            "intent": plan["intent"][:120],
        }
        decision = enforce_variety(decision, history, now)
        decision["_source"] = "rule"
        decision["_latency_s"] = round(time.monotonic() - started, 4)
        return decision


# -- the prompt -------------------------------------------------------------


def build_prompt(
    summary: dict,
    section: str,
    history: History,
    bpm: float | None = None,
    now: float = 0.0,
) -> str:
    """Assemble the ``codex exec`` prompt: rules, now, and what has been seen.

    Only summaries go in — never the raw 10 Hz series (Phase 4 notes). The
    history carries durations so the model can tell "that scene has been up for
    ninety seconds" from "that scene flashed by".

    The result is capped at :data:`MAX_PROMPT_CHARS` by dropping the oldest
    history entries, so prompt size cannot drift over a long set.
    """
    context = {"section": section, "bpm": round(float(bpm or DEFAULT_BPM))}
    context.update(summary)
    now_line = "\nNow: " + json.dumps(context, ensure_ascii=False, sort_keys=False)
    records = history.records(now)
    while True:
        history_line = "\nVisual history (newest first): " + json.dumps(
            records, ensure_ascii=False
        )
        prompt = SYSTEM_PROMPT + now_line + history_line
        if len(prompt) <= MAX_PROMPT_CHARS or not records:
            return prompt
        records = records[:-1]


# -- the GPT director -------------------------------------------------------


class GPTDirector:
    """:class:`RuleDirector`'s signature, backed by one ``codex exec`` call.

    Every failure mode of the GPT path — timeout, non-zero exit, unparseable
    output, schema violation, quota — surfaces the same way: the rule director
    answers instead and ``_source`` says which exception caused it. Nothing
    raises out of :meth:`decide`; a director that can throw is a director that
    can black out the screen.
    """

    def __init__(
        self,
        client: Any,
        fallback: RuleDirector,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.fallback = fallback
        self.clock = clock

    def decide(
        self,
        summary: dict,
        section: str,
        history: History,
        now: float,
        bpm: float | None = None,
    ) -> dict:
        started = self.clock()
        try:
            prompt = build_prompt(summary, section, history, bpm, now)
            decision = self.client.decide(prompt)
            decision = enforce_variety(decision, history, now)
        except BaseException as exc:  # noqa: BLE001 - see the class docstring
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            decision = self.fallback.decide(summary, section, history, now, bpm)
            decision["_source"] = f"rule ({type(exc).__name__})"
            decision["_error"] = str(exc)[:200]
        else:
            decision["_source"] = "gpt"
        decision["_latency_s"] = round(self.clock() - started, 3)
        return decision


# -- the loop ---------------------------------------------------------------


def _clock_stamp() -> str:
    return time.strftime("%H:%M:%S", time.localtime())


class DirectorLoop:
    """Timing, threading and publishing for the director (SPEC §5).

    Call :meth:`on_tick` from :class:`~amv.sidecar.Sidecar`'s ``on_tick`` hook.
    It decides whether this tick deserves a decision and, if so, starts one —
    on a worker thread, so the 10 Hz loop is never behind a 13 second call.

    Args:
        td: Where decisions go.
        director: The ``gpt`` director. If it carries a ``fallback`` (i.e. it
            is a :class:`GPTDirector`) that fallback is the ``rule`` mode
            director; otherwise this object serves both modes.
        mode: ``gpt`` / ``rule`` / ``manual``.
        period_s: Seconds between decisions, on ``clock``'s scale.
        min_interval_s: Floor between an event-triggered decision and the last
            one, so a build → drop → steady flurry cannot fire three calls.
        clock: Track time. Pass the sidecar's ``ScaledClock`` and ``--speed``
            compresses the decision period with everything else.
        log_path: JSONL, one line per decision.
        worker: ``False`` runs decisions inline (tests only).
    """

    def __init__(
        self,
        td: TDClient | None,
        director: Any,
        *,
        mode: str = "gpt",
        period_s: float = 18.0,
        min_interval_s: float = 6.0,
        clock: Callable[[], float] = time.monotonic,
        log_path: str | Path | None = None,
        worker: bool = True,
        out: TextIO | None = None,
        bpm: float = DEFAULT_BPM,
        history: History | None = None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        self.td = td
        self.director = director
        self.rule = getattr(director, "fallback", director)
        self.mode = mode
        self.period_s = float(period_s)
        self.min_interval_s = float(min_interval_s)
        self.clock = clock
        self.log_path = Path(log_path) if log_path else None
        self.worker = bool(worker)
        self.out = out if out is not None else sys.stdout
        self.bpm = float(bpm)
        self.history = history if history is not None else History()

        self.decisions: list[dict] = []
        self.heartbeat = 0
        self.errors = 0
        self.stop_requested = False
        self.last_section: str | None = None
        self._last_decision: float | None = None
        self._inflight = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._log: TextIO | None = None

    # -- lifecycle ----------------------------------------------------------

    def close(self, timeout: float = 5.0) -> None:
        """Wait for an in-flight decision and close the log. Safe to call twice."""
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        with self._lock:
            if self._log is not None:
                self._log.close()
                self._log = None

    def __enter__(self) -> "DirectorLoop":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def set_mode(self, name: str) -> str:
        """Switch mode live (the hotkeys, SPEC §5). Returns the new mode."""
        if name not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {name!r}")
        if name != self.mode:
            self.mode = name
            print(f"{_clock_stamp()}  mode → {name}", file=self.out, flush=True)
        return self.mode

    def request_stop(self) -> None:
        """Ask the sidecar loop to end at the next tick (the ``q`` hotkey)."""
        self.stop_requested = True

    # -- triggering ---------------------------------------------------------

    def _director_for_mode(self) -> Any:
        return self.rule if self.mode == "rule" else self.director

    def should_fire(self, section: str, now: float) -> bool:
        """Is this tick a decision? Period elapsed, or a section worth reacting to."""
        if self._inflight:
            return False
        if self._last_decision is None:
            return True
        since = now - self._last_decision
        if since >= self.period_s:
            return True
        changed = section != self.last_section and section in EVENT_SECTIONS
        return changed and since >= self.min_interval_s

    def on_tick(self, summary: dict, section: str) -> bool:
        """The :class:`~amv.sidecar.Sidecar` hook. Returns whether it fired.

        Raises ``KeyboardInterrupt`` when :meth:`request_stop` has been called,
        which is how the ``q`` hotkey ends a run: the sidecar's own loop already
        treats that as a clean shutdown.
        """
        if self.stop_requested:
            raise KeyboardInterrupt
        now = self.clock()
        fire = self.should_fire(section, now)
        self.last_section = section
        if not fire:
            return False
        self._last_decision = now
        if self.mode == "manual":
            # No decisions, but TD's watchdog still needs to see us (SPEC §5).
            self._beat()
            return True
        self._inflight = True
        if self.worker:
            self._thread = threading.Thread(
                target=self._run_decision,
                args=(dict(summary), section, now),
                name="amv-director",
                daemon=True,
            )
            self._thread.start()
        else:
            self._run_decision(dict(summary), section, now)
        return True

    # -- deciding -----------------------------------------------------------

    def _bpm(self, summary: dict) -> float:
        """Tempo for the prompt: the kick rate when it looks like one."""
        kicks = float(summary.get("kicks_per_min") or 0.0)
        return kicks if 60.0 <= kicks <= 220.0 else self.bpm

    def _run_decision(self, summary: dict, section: str, now: float) -> None:
        try:
            director = self._director_for_mode()
            decision = director.decide(summary, section, self.history, now, self._bpm(summary))
            self._publish(decision, section, now)
        except BaseException as exc:  # noqa: BLE001 - a worker thread must not die silently
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self.errors += 1
            print(
                f"{_clock_stamp()}  director error: {type(exc).__name__}: {exc}",
                file=self.out,
                flush=True,
            )
        finally:
            self._inflight = False

    # -- publishing ---------------------------------------------------------

    def _beat(self) -> int:
        self.heartbeat += 1
        if self.td is not None:
            self.td.send_heartbeat(self.heartbeat)
        return self.heartbeat

    def _publish(self, decision: dict, section: str, now: float) -> dict:
        """Send, remember, log and print one decision."""
        clean = strip_private(decision)
        source = str(decision.get("_source", self.mode))
        latency = float(decision.get("_latency_s", 0.0) or 0.0)
        with self._lock:
            self.heartbeat += 1
            beat = self.heartbeat
            if self.td is not None:
                # send_director ends with the heartbeat, so passing ours keeps
                # one decision to exactly one beat.
                self.td.send_director(clean, heartbeat=beat)
            self.history.append(clean, now, source)
            record = {
                "t": round(float(now), 3),
                "wall": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                "section": section,
                "source": source,
                "latency_s": round(latency, 3),
                "heartbeat": beat,
                "decision": clean,
            }
            self.decisions.append(record)
            self._write(record)
            print(self.format_line(record), file=self.out, flush=True)
        return record

    def _write(self, record: dict) -> None:
        if self.log_path is None:
            return
        if self._log is None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log = self.log_path.open("a", encoding="utf-8")
        self._log.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._log.flush()

    @staticmethod
    def format_line(record: dict) -> str:
        """``HH:MM:SS  [gpt 12.3s] build  scene/palette  glide 16  — intent``."""
        d = record["decision"]
        transition = d.get("transition") or {}
        return (
            f"{time.strftime('%H:%M:%S', time.localtime())}  "
            f"[{record['source']} {record['latency_s']:.1f}s] "
            f"{record['section']:<9} {d['scene']}/{d['palette']}  "
            f"{transition.get('mode', '?')} {transition.get('beats', '?')}  "
            f"— {d.get('intent', '')}"
        )

    # -- reporting ----------------------------------------------------------

    def stats(self) -> dict:
        """Decision counts by source and the mean latency, for the shutdown line."""
        by_source: dict[str, int] = {}
        total = 0.0
        for record in self.decisions:
            by_source[record["source"]] = by_source.get(record["source"], 0) + 1
        for record in self.decisions:
            total += float(record["latency_s"])
        n = len(self.decisions)
        return {
            "decisions": n,
            "by_source": by_source,
            "mean_latency_s": round(total / n, 2) if n else 0.0,
            "heartbeats": self.heartbeat,
            "errors": self.errors,
            "mode": self.mode,
        }


# -- hotkeys ----------------------------------------------------------------


class Hotkeys:
    """``g`` / ``r`` / ``m`` switch mode, ``q`` stops (SPEC §5).

    A daemon thread in cbreak mode reading one character at a time. When stdin
    is not a TTY — piped, backgrounded, under pytest — :meth:`start` does
    nothing at all and says so via :attr:`active`, because a background process
    fighting over the terminal is worse than having no hotkeys.
    """

    KEYS: dict[str, str] = {"g": "gpt", "r": "rule", "m": "manual"}

    def __init__(self, loop: DirectorLoop, stream: TextIO | None = None) -> None:
        self.loop = loop
        self.stream = stream if stream is not None else sys.stdin
        self.active = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> "Hotkeys":
        try:
            if not self.stream.isatty():
                return self
        except (AttributeError, ValueError):
            return self
        self.active = True
        self._thread = threading.Thread(target=self._run, name="amv-hotkeys", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.active = False

    def handle(self, char: str) -> None:
        """Apply one keystroke. Separated out so tests need no terminal."""
        key = char.lower()
        if key in self.KEYS:
            self.loop.set_mode(self.KEYS[key])
        elif key == "q":
            self.loop.request_stop()

    def _run(self) -> None:  # pragma: no cover - needs a real terminal
        import termios
        import tty

        fd = self.stream.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not self._stop.is_set():
                char = self.stream.read(1)
                if not char:
                    break
                self.handle(char)
        except Exception:
            pass
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            except Exception:
                pass
