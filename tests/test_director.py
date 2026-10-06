"""Tests for amv.director — Phase 4.

No real ``codex exec`` runs here: the end-to-end tests drive
``tools/fake_codex.py`` through the real :class:`~amv.codex_client.CodexClient`
(same argv, same subprocess, same timeout path) so failover is exercised
without spending Codex quota. Everything else is fake clocks and stubs.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from amv.codex_client import CodexClient, CodexError
from amv.director import (
    DEFAULT_BPM,
    FORCE_SCENE_AFTER,
    MAX_PROMPT_CHARS,
    REPEAT_WINDOW_S,
    SYSTEM_PROMPT,
    DirectorLoop,
    GPTDirector,
    History,
    Hotkeys,
    RuleDirector,
    build_prompt,
    enforce_variety,
    strip_private,
)
from amv.schema import PALETTES, SCENES, DecisionError, validate_and_clamp
from amv.sections import SECTIONS

FAKE_CODEX = Path(__file__).resolve().parent.parent / "tools" / "fake_codex.py"

SUMMARY = {
    "bass": 0.62,
    "mid": 0.44,
    "high": 0.31,
    "energy": 0.71,
    "energy_30s": 0.68,
    "energy_120s": 0.55,
    "energy_trend_30s": "rising",
    "kicks_per_min": 145.0,
    "centroid": 2400.0,
}

VALID = {
    "scene": "tunnel",
    "palette": "violet_cyan",
    "feedback": 0.4,
    "symmetry": 4,
    "camera_speed": 0.5,
    "particle_mode": "spiral",
    "projectm_mix": 0.1,
    "transition": {"mode": "glide", "beats": 8},
    "on_drop": {"scene": "kaleido_mesh", "palette": "infrared", "particle_mode": "burst"},
    "intent": "test",
}


class FakeClock:
    """A clock that only moves when a test tells it to."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> "FakeClock":
        self.t += seconds
        return self


class RecordingTD:
    """The two :class:`~amv.osc_io.TDClient` methods the loop actually uses."""

    def __init__(self) -> None:
        self.decisions: list[dict] = []
        self.heartbeats: list[int] = []

    def send_director(self, decision: dict, heartbeat: int | None = None) -> int:
        self.decisions.append(decision)
        self.heartbeats.append(int(heartbeat or 0))
        return int(heartbeat or 0)

    def send_heartbeat(self, n: int) -> None:
        self.heartbeats.append(int(n))


class StubClient:
    """Stands in for CodexClient: returns a payload or raises."""

    def __init__(self, payload: dict | None = None, error: BaseException | None = None) -> None:
        self.payload = payload
        self.error = error
        self.prompts: list[str] = []

    def decide(self, prompt: str) -> dict:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return validate_and_clamp(dict(self.payload or VALID))


def rule_loop(td=None, **kwargs) -> tuple[DirectorLoop, FakeClock]:
    clock = FakeClock()
    loop = DirectorLoop(
        td,
        RuleDirector(),
        mode=kwargs.pop("mode", "rule"),
        clock=clock,
        worker=kwargs.pop("worker", False),
        out=kwargs.pop("out", io.StringIO()),
        **kwargs,
    )
    return loop, clock


# -- History ----------------------------------------------------------------


def test_history_keeps_eight_newest_first():
    history = History()
    for i in range(12):
        history.append({**VALID, "scene": SCENES[i % len(SCENES)]}, float(i))
    assert len(history) == 8
    assert history.entries[0]["t"] == 11.0
    assert history.count == 12


def test_history_last_use_and_ages():
    history = History()
    history.append({**VALID, "scene": "tunnel", "palette": "infrared"}, 10.0)
    history.append({**VALID, "scene": "kaleido_mesh", "palette": "acid_lime"}, 40.0)
    assert history.last_use("tunnel", "infrared", 70.0) == 60.0
    assert history.last_use("tunnel", "acid_lime", 70.0) is None
    assert history.age_of("scene", "kaleido_mesh", 70.0) == 30.0
    assert history.age_of("scene", "projectm_blend", 70.0) == float("inf")


def test_history_counts_decisions_since_scene_change():
    history = History()
    assert history.decisions_since_scene_change() == 0
    history.append({**VALID, "scene": "tunnel"}, 0.0)
    assert history.decisions_since_scene_change() == 1
    for i in range(1, 4):
        history.append({**VALID, "scene": "tunnel"}, float(i))
    assert history.decisions_since_scene_change() == 4
    history.append({**VALID, "scene": "particle_field"}, 9.0)
    assert history.decisions_since_scene_change() == 1


def test_history_records_carry_durations():
    history = History()
    history.append({**VALID, "scene": "tunnel"}, 0.0)
    history.append({**VALID, "scene": "kaleido_mesh"}, 20.0)
    records = history.records(now=50.0)
    assert [r["scene"] for r in records] == ["kaleido_mesh", "tunnel"]
    assert records[0]["duration_s"] == 30.0  # newest: still on screen
    assert records[1]["duration_s"] == 20.0  # replaced after 20 s
    assert records[0]["ago_s"] == 30.0


# -- enforce_variety --------------------------------------------------------


def test_enforce_variety_rotates_palette_on_a_60s_repeat():
    history = History()
    history.append({**VALID, "scene": "tunnel", "palette": "violet_cyan"}, 0.0)
    out = enforce_variety(dict(VALID), history, now=REPEAT_WINDOW_S - 1)
    assert out["scene"] == "tunnel"
    assert out["palette"] != "violet_cyan"


def test_enforce_variety_leaves_a_stale_pair_alone():
    history = History()
    history.append({**VALID, "scene": "tunnel", "palette": "violet_cyan"}, 0.0)
    out = enforce_variety(dict(VALID), history, now=REPEAT_WINDOW_S + 1)
    assert (out["scene"], out["palette"]) == ("tunnel", "violet_cyan")


def test_enforce_variety_rotates_the_scene_when_every_palette_is_fresh():
    """All five palettes used on one scene inside the window → change scene."""
    history = History()
    for i, palette in enumerate(PALETTES):
        history.append({**VALID, "scene": "tunnel", "palette": palette}, float(i))
    out = enforce_variety(dict(VALID), history, now=10.0)
    assert out["scene"] != "tunnel"


def test_enforce_variety_forces_a_scene_change_after_ten_decisions():
    history = History()
    for i in range(FORCE_SCENE_AFTER):
        history.append({**VALID, "scene": "tunnel"}, float(i) * 120.0)
    assert history.decisions_since_scene_change() == FORCE_SCENE_AFTER
    now = FORCE_SCENE_AFTER * 120.0 + 120.0
    out = enforce_variety({**VALID, "scene": "tunnel"}, history, now)
    assert out["scene"] != "tunnel"


def test_enforce_variety_does_not_force_a_change_before_ten():
    history = History()
    for i in range(FORCE_SCENE_AFTER - 1):
        history.append({**VALID, "scene": "tunnel"}, float(i) * 120.0)
    now = FORCE_SCENE_AFTER * 120.0
    out = enforce_variety({**VALID, "scene": "tunnel"}, history, now)
    assert out["scene"] == "tunnel"


def test_enforce_variety_changes_a_repeated_on_drop():
    history = History()
    history.append({**VALID, "scene": "particle_field", "palette": "amber_dusk"}, 0.0)
    out = enforce_variety(
        {**VALID, "scene": "particle_field", "palette": "amber_dusk"}, history, now=1000.0
    )
    assert out["on_drop"] != VALID["on_drop"]
    assert out["on_drop"]["particle_mode"] != VALID["on_drop"]["particle_mode"]


def test_enforce_variety_always_returns_a_valid_decision():
    history = History()
    out = enforce_variety({**VALID, "feedback": 4.2, "symmetry": 99, "intent": "x" * 400},
                          history, now=0.0)
    assert validate_and_clamp(out) == out
    assert out["feedback"] == 0.98
    assert out["symmetry"] == 16
    assert len(out["intent"]) == 120


def test_enforce_variety_keeps_private_keys():
    history = History()
    out = enforce_variety({**VALID, "_source": "gpt", "_latency_s": 1.5}, history, now=0.0)
    assert out["_source"] == "gpt"
    assert "_source" not in strip_private(out)


def test_enforce_variety_rejects_junk():
    with pytest.raises(DecisionError):
        enforce_variety({**VALID, "scene": "no_such_scene"}, History(), now=0.0)


# -- RuleDirector -----------------------------------------------------------


@pytest.mark.parametrize("section", SECTIONS)
def test_rule_director_output_validates_for_every_section(section):
    decision = RuleDirector().decide(SUMMARY, section, History(), now=0.0)
    clean = strip_private(decision)
    assert validate_and_clamp(clean) == clean
    assert decision["_source"] == "rule"
    assert isinstance(decision["_latency_s"], float)


def test_rule_director_shapes_each_section():
    history = History()
    now = 0.0
    shapes = {}
    for section in ("build", "drop", "breakdown", "steady"):
        now += 120.0
        shapes[section] = RuleDirector().decide(SUMMARY, section, history, now)

    build = shapes["build"]
    assert 0.66 <= build["feedback"] <= 0.94
    assert 8 <= build["symmetry"] <= 12
    assert build["camera_speed"] <= 0.43
    assert build["transition"] == {"mode": "glide", "beats": 16}

    drop = shapes["drop"]
    assert drop["particle_mode"] == "burst"
    assert drop["transition"]["mode"] == "cut"

    down = shapes["breakdown"]
    assert down["particle_mode"] in ("none", "rain")
    assert down["feedback"] <= 0.5
    assert down["projectm_mix"] <= 0.2
    assert down["transition"] == {"mode": "glide", "beats": 8}

    assert shapes["steady"]["transition"] == {"mode": "glide", "beats": 12}


def test_rule_director_fifty_seeded_runs_stay_valid_and_varied():
    """50 seeds × 24 decisions: every one schema-valid, no repeat, no rut."""
    import random

    for seed in range(50):
        rng = random.Random(seed)
        director = RuleDirector(seed=seed)
        history = History()
        now = 0.0
        for _ in range(24):
            now += rng.uniform(6.0, 20.0)
            section = rng.choice(SECTIONS)
            summary = {**SUMMARY, "energy": rng.random()}
            decision = director.decide(summary, section, history, now)
            clean = strip_private(decision)
            assert validate_and_clamp(clean) == clean
            # The rule the prompt only asks for: never the same look twice
            # inside a minute.
            used = history.last_use(clean["scene"], clean["palette"], now)
            assert used is None or used >= REPEAT_WINDOW_S, (seed, clean, used)
            history.append(clean, now, "rule")
            assert history.decisions_since_scene_change() <= FORCE_SCENE_AFTER


def test_rule_director_is_reproducible():
    a = RuleDirector(seed=7).decide(SUMMARY, "build", History(), 0.0)
    b = RuleDirector(seed=7).decide(SUMMARY, "build", History(), 0.0)
    assert strip_private(a) == strip_private(b)


# -- build_prompt -----------------------------------------------------------


def full_history(now: float = 400.0) -> History:
    history = History()
    for i in range(12):
        history.append(
            {
                **VALID,
                "scene": SCENES[i % len(SCENES)],
                "palette": PALETTES[(i * 3) % len(PALETTES)],
                "intent": "x" * 120,
            },
            now - (12 - i) * 20.0,
            "gpt",
        )
    return history


def test_build_prompt_fits_and_carries_the_context():
    prompt = build_prompt(SUMMARY, "build", full_history(), bpm=145.0, now=400.0)
    assert len(prompt) < 2000
    assert len(prompt) <= MAX_PROMPT_CHARS
    assert prompt.startswith(SYSTEM_PROMPT)
    assert '"section": "build"' in prompt
    assert '"bpm": 145' in prompt
    assert "Visual history (newest first):" in prompt
    assert "duration_s" in prompt
    assert "energy_trend_30s" in prompt


def test_build_prompt_survives_an_absurd_history():
    """A prompt cannot grow past the cap even if every entry is maximal."""
    history = History(maxlen=64)
    for i in range(64):
        history.append({**VALID, "scene": SCENES[i % 5], "palette": PALETTES[i % 5]}, float(i))
    prompt = build_prompt(SUMMARY, "steady", history, bpm=145.0, now=999.0)
    assert len(prompt) <= MAX_PROMPT_CHARS


def test_build_prompt_defaults_the_bpm():
    prompt = build_prompt(SUMMARY, "steady", History(), bpm=None, now=0.0)
    assert f'"bpm": {round(DEFAULT_BPM)}' in prompt
    assert "Visual history (newest first): []" in prompt


# -- GPTDirector ------------------------------------------------------------


def test_gpt_director_tags_a_successful_call():
    director = GPTDirector(StubClient(VALID), RuleDirector())
    decision = director.decide(SUMMARY, "build", History(), now=0.0)
    assert decision["_source"] == "gpt"
    assert decision["_latency_s"] >= 0.0
    clean = strip_private(decision)
    assert validate_and_clamp(clean) == clean


def test_gpt_director_falls_back_on_codex_error():
    director = GPTDirector(StubClient(error=CodexError("boom")), RuleDirector())
    decision = director.decide(SUMMARY, "drop", History(), now=0.0)
    assert decision["_source"] == "rule (CodexError)"
    assert "boom" in decision["_error"]
    assert decision["particle_mode"] == "burst"  # the rule director really ran


def test_gpt_director_falls_back_on_any_exception():
    director = GPTDirector(StubClient(error=RuntimeError("nope")), RuleDirector())
    decision = director.decide(SUMMARY, "steady", History(), now=0.0)
    assert decision["_source"] == "rule (RuntimeError)"
    clean = strip_private(decision)
    assert validate_and_clamp(clean) == clean


def test_gpt_director_falls_back_when_the_reply_is_off_schema():
    director = GPTDirector(StubClient({**VALID, "scene": "nope"}), RuleDirector())
    decision = director.decide(SUMMARY, "steady", History(), now=0.0)
    assert decision["_source"] == "rule (DecisionError)"


def test_gpt_director_applies_variety_to_the_model_reply():
    history = History()
    history.append(dict(VALID), 0.0)
    director = GPTDirector(StubClient(VALID), RuleDirector())
    decision = director.decide(SUMMARY, "steady", history, now=10.0)
    assert decision["_source"] == "gpt"
    assert (decision["scene"], decision["palette"]) != (VALID["scene"], VALID["palette"])


def test_gpt_director_sends_the_built_prompt():
    client = StubClient(VALID)
    GPTDirector(client, RuleDirector()).decide(SUMMARY, "breakdown", History(), 0.0, bpm=150.0)
    assert client.prompts[0].startswith(SYSTEM_PROMPT)
    assert '"bpm": 150' in client.prompts[0]


# -- DirectorLoop -----------------------------------------------------------

@pytest.mark.parametrize("action", ["manual", "rule_then_gpt", "stop", "close"])
def test_inflight_reply_cannot_override_takeover_or_shutdown(action):
    td = RecordingTD()
    clock = FakeClock()
    class Client:
        def decide(self, prompt):
            if action == "manual":
                loop.set_mode("manual")
            elif action == "rule_then_gpt":
                loop.set_mode("rule")
                loop.set_mode("gpt")
            elif action == "stop":
                loop.request_stop()
            else:
                loop.close()
            return dict(VALID)
    loop = DirectorLoop(td, GPTDirector(Client(), RuleDirector()),
                        clock=clock, worker=False, out=io.StringIO())
    loop.on_tick(SUMMARY, "build")
    assert td.decisions == []


@pytest.mark.parametrize("changed_section", [True, False])
def test_stale_reply_uses_current_music_and_publication_time(changed_section):
    td = RecordingTD()
    clock = FakeClock()
    class Client:
        def decide(self, prompt):
            clock.advance(13 if changed_section else 20)
            loop.on_tick(dict(SUMMARY, energy=0.1),
                         "breakdown" if changed_section else "build")
            return dict(VALID)
    loop = DirectorLoop(td, GPTDirector(Client(), RuleDirector()),
                        clock=clock, worker=False, out=io.StringIO())
    loop.on_tick(SUMMARY, "build")
    assert len(td.decisions) == 1
    assert loop.decisions[0]["source"] == "rule (stale decision)"
    assert loop.decisions[0]["t"] == clock()
    assert loop.decisions[0]["section"] == ("breakdown" if changed_section else "build")


def test_loop_fires_immediately_then_once_a_period():
    td = RecordingTD()
    loop, clock = rule_loop(td, period_s=18.0)
    assert loop.on_tick(SUMMARY, "steady") is True
    assert loop.on_tick(SUMMARY, "steady") is False
    clock.advance(17.9)
    assert loop.on_tick(SUMMARY, "steady") is False
    clock.advance(0.2)
    assert loop.on_tick(SUMMARY, "steady") is True
    assert len(td.decisions) == 2


def test_loop_event_trigger_respects_min_interval():
    td = RecordingTD()
    loop, clock = rule_loop(td, period_s=18.0, min_interval_s=6.0)
    loop.on_tick(SUMMARY, "steady")  # first decision at t=0
    clock.advance(3.0)
    assert loop.on_tick(SUMMARY, "build") is False  # section changed, too soon
    clock.advance(4.0)  # t=7 > min_interval
    assert loop.on_tick(SUMMARY, "steady") is False  # steady is not an event
    assert loop.on_tick(SUMMARY, "drop") is True  # ... but a drop is
    assert td.decisions[-1]["particle_mode"] == "burst"


def test_loop_event_trigger_needs_an_actual_change():
    td = RecordingTD()
    loop, clock = rule_loop(td, period_s=18.0, min_interval_s=6.0)
    loop.on_tick(SUMMARY, "build")
    clock.advance(10.0)
    assert loop.on_tick(SUMMARY, "build") is False  # same section, no event
    clock.advance(10.0)
    assert loop.on_tick(SUMMARY, "build") is True  # period elapsed


def test_loop_manual_mode_sends_heartbeats_only():
    td = RecordingTD()
    loop, clock = rule_loop(td, mode="manual", period_s=18.0)
    for _ in range(3):
        assert loop.on_tick(SUMMARY, "steady") is True
        clock.advance(18.0)
    assert td.decisions == []
    assert td.heartbeats == [1, 2, 3]
    assert loop.stats()["decisions"] == 0


def test_loop_set_mode_switches_director():
    td = RecordingTD()
    clock = FakeClock()
    gpt = GPTDirector(StubClient(VALID), RuleDirector())
    loop = DirectorLoop(td, gpt, mode="gpt", clock=clock, worker=False,
                        out=io.StringIO())
    loop.on_tick(SUMMARY, "steady")
    assert loop.decisions[-1]["source"] == "gpt"
    loop.set_mode("rule")
    clock.advance(20.0)
    loop.on_tick(SUMMARY, "drop")
    assert loop.decisions[-1]["source"] == "rule"
    with pytest.raises(ValueError):
        loop.set_mode("nonsense")


def test_loop_publishes_history_heartbeats_and_a_log(tmp_path):
    td = RecordingTD()
    log = tmp_path / "decisions.jsonl"
    loop, clock = rule_loop(td, period_s=10.0, log_path=log)
    for _ in range(3):
        loop.on_tick(SUMMARY, "steady")
        clock.advance(10.0)
    loop.close()

    assert td.heartbeats == [1, 2, 3]
    assert len(loop.history) == 3
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 3
    assert set(lines[0]) >= {"t", "section", "source", "latency_s", "decision"}
    assert lines[0]["section"] == "steady"
    assert lines[0]["source"] == "rule"
    assert lines[1]["t"] == 10.0
    decision = lines[0]["decision"]
    assert validate_and_clamp(decision) == decision
    assert not [k for k in decision if k.startswith("_")]


def test_loop_never_sends_private_keys_to_td():
    td = RecordingTD()
    clock = FakeClock()
    loop = DirectorLoop(td, GPTDirector(StubClient(VALID), RuleDirector()), mode="gpt",
                        clock=clock, worker=False, out=io.StringIO())
    loop.on_tick(SUMMARY, "build")
    assert not [k for k in td.decisions[0] if k.startswith("_")]


def test_loop_prints_one_line_per_decision(capsys):
    td = RecordingTD()
    clock = FakeClock()
    loop = DirectorLoop(td, RuleDirector(), mode="rule", clock=clock, worker=False,
                        out=sys.stdout)
    loop.on_tick(SUMMARY, "build")
    line = capsys.readouterr().out.strip()
    assert "[rule " in line and "build" in line and "glide 16" in line and "—" in line
    assert "/" in line.split("build")[1]


def test_loop_keeps_only_one_decision_in_flight():
    """A 13 second call must not queue up behind the 10 Hz tick."""
    gate = threading.Event()

    class SlowDirector:
        def __init__(self) -> None:
            self.calls = 0

        def decide(self, summary, section, history, now, bpm=None):
            self.calls += 1
            gate.wait(5.0)
            return RuleDirector().decide(summary, section, history, now)

    td = RecordingTD()
    clock = FakeClock()
    slow = SlowDirector()
    loop = DirectorLoop(td, slow, mode="rule", period_s=1.0, clock=clock, worker=True,
                        out=io.StringIO())
    assert loop.on_tick(SUMMARY, "steady") is True
    deadline = time.monotonic() + 2.0
    while slow.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    for _ in range(20):  # two seconds of ticks, all past the period
        clock.advance(1.0)
        assert loop.on_tick(SUMMARY, "steady") is False
    assert slow.calls == 1
    gate.set()
    # Let this reply publish before shutting down. close() intentionally
    # rejects replies still in flight, as the takeover tests above verify.
    loop._thread.join(2.0)
    assert slow.calls == 1
    assert len(td.decisions) == 1

    clock.advance(10.0)
    assert loop.on_tick(SUMMARY, "steady") is True
    loop._thread.join(2.0)
    loop.close()
    assert slow.calls == 2


def test_loop_survives_a_director_that_raises():
    class Broken:
        def decide(self, *a, **k):
            raise ZeroDivisionError("oops")

    td = RecordingTD()
    loop, clock = rule_loop(td)
    loop.director = loop.rule = Broken()
    loop.on_tick(SUMMARY, "steady")
    assert loop.errors == 1
    assert td.decisions == []
    clock.advance(20.0)
    loop.director = loop.rule = RuleDirector()
    assert loop.on_tick(SUMMARY, "steady") is True


def test_loop_stop_request_raises_keyboard_interrupt():
    loop, _ = rule_loop(RecordingTD())
    loop.request_stop()
    with pytest.raises(KeyboardInterrupt):
        loop.on_tick(SUMMARY, "steady")


def test_loop_stats_counts_sources_and_latency():
    td = RecordingTD()
    clock = FakeClock()
    gpt = GPTDirector(StubClient(VALID), RuleDirector())
    loop = DirectorLoop(td, gpt, mode="gpt", period_s=10.0, clock=clock, worker=False,
                        out=io.StringIO())
    loop.on_tick(SUMMARY, "steady")
    clock.advance(10.0)
    gpt.client.error = CodexError("quota")
    loop.on_tick(SUMMARY, "steady")
    stats = loop.stats()
    assert stats["decisions"] == 2
    assert stats["by_source"] == {"gpt": 1, "rule (CodexError)": 1}
    assert stats["mean_latency_s"] >= 0.0
    assert stats["heartbeats"] == 2


def test_loop_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        DirectorLoop(None, RuleDirector(), mode="off")


# -- Hotkeys ----------------------------------------------------------------


def test_hotkeys_switch_modes_and_stop():
    loop, _ = rule_loop(RecordingTD(), mode="gpt")
    keys = Hotkeys(loop)
    keys.handle("r")
    assert loop.mode == "rule"
    keys.handle("M")
    assert loop.mode == "manual"
    keys.handle("g")
    assert loop.mode == "gpt"
    keys.handle("z")  # unknown keys are ignored
    assert loop.mode == "gpt"
    keys.handle("q")
    assert loop.stop_requested is True


def test_hotkeys_skip_a_non_tty():
    class NotATty:
        def isatty(self) -> bool:
            return False

    loop, _ = rule_loop(RecordingTD())
    keys = Hotkeys(loop, stream=NotATty()).start()
    assert keys.active is False


# -- end to end through tools/fake_codex.py ---------------------------------


def test_fake_codex_is_executable():
    assert FAKE_CODEX.is_file()
    assert os.access(FAKE_CODEX, os.X_OK), "tools/fake_codex.py must be chmod +x"
    assert FAKE_CODEX.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3")


def fake_client(timeout: float = 2.0, tmp_path: Path | None = None) -> CodexClient:
    return CodexClient(binary=FAKE_CODEX, timeout=timeout, cwd=tmp_path)


@pytest.mark.parametrize(
    "mode,timeout,expected",
    [
        ("ok", 5.0, "gpt"),
        ("fail", 5.0, "rule (CodexError)"),
        ("garbage", 5.0, "rule (CodexError)"),
        ("hang", 1.0, "rule (CodexError)"),
    ],
)
def test_end_to_end_through_fake_codex(monkeypatch, tmp_path, mode, timeout, expected):
    monkeypatch.setenv("AMV_FAKE_CODEX_MODE", mode)
    monkeypatch.setenv("AMV_FAKE_CODEX_DELAY", "0")
    director = GPTDirector(fake_client(timeout, tmp_path), RuleDirector())
    decision = director.decide(SUMMARY, "build", History(), now=0.0)
    assert decision["_source"] == expected
    clean = strip_private(decision)
    assert validate_and_clamp(clean) == clean


def test_fake_codex_varies_with_the_prompt(monkeypatch, tmp_path):
    monkeypatch.setenv("AMV_FAKE_CODEX_MODE", "ok")
    monkeypatch.setenv("AMV_FAKE_CODEX_DELAY", "0")
    client = fake_client(5.0, tmp_path)
    one = client.decide("prompt one")
    two = client.decide("prompt two")
    again = client.decide("prompt one")
    assert one == again
    assert (one["scene"], one["palette"], one["symmetry"]) != (
        two["scene"],
        two["palette"],
        two["symmetry"],
    )


def test_fake_codex_reports_a_version(tmp_path):
    assert "codex-cli" in fake_client(5.0, tmp_path).version()


def test_end_to_end_loop_falls_back_without_stalling(monkeypatch, tmp_path):
    """The Phase 4 acceptance test in miniature: codex down, screen still moves."""
    monkeypatch.setenv("AMV_FAKE_CODEX_MODE", "fail")
    monkeypatch.setenv("AMV_FAKE_CODEX_DELAY", "0")
    td = RecordingTD()
    clock = FakeClock()
    loop = DirectorLoop(
        td,
        GPTDirector(fake_client(5.0, tmp_path), RuleDirector()),
        mode="gpt",
        period_s=18.0,
        clock=clock,
        worker=False,
        out=io.StringIO(),
    )
    for _ in range(6):
        loop.on_tick(SUMMARY, "steady")
        clock.advance(18.0)
    assert len(td.decisions) == 6
    assert loop.stats()["by_source"] == {"rule (CodexError)": 6}
    assert loop.errors == 0
    for decision in td.decisions:
        assert validate_and_clamp(decision) == decision


@pytest.mark.parametrize("action", ["manual", "stop", "close"])
def test_takeover_during_stale_recomputation_cannot_publish(action):
    clock = FakeClock()
    td = RecordingTD()

    class SlowReply:
        def decide(self, prompt):
            clock.advance(20)
            return dict(VALID)

    class TakeoverRule(RuleDirector):
        def decide(self, *args, **kwargs):
            if action == "manual":
                loop.set_mode("manual")
            elif action == "stop":
                loop.request_stop()
            else:
                loop.close()
            return super().decide(*args, **kwargs)

    loop = DirectorLoop(td, GPTDirector(SlowReply(), TakeoverRule()),
                        clock=clock, worker=False, out=io.StringIO())
    loop.on_tick(SUMMARY, "build")
    assert td.decisions == []
    assert loop.history.count == 0


def test_watchdog_bounds_outage_then_rejects_late_reply_and_recovers():
    clock, wall, td = FakeClock(), FakeClock(), RecordingTD()
    entered, release = threading.Event(), threading.Event()

    class Client:
        calls = 0

        def decide(self, prompt):
            self.calls += 1
            if self.calls == 2:
                entered.set()
                assert release.wait(5)
            return dict(VALID)

    client = Client()
    loop = DirectorLoop(td, GPTDirector(client, RuleDirector()), clock=clock,
                        wall_clock=wall, out=io.StringIO())
    try:
        loop.on_tick(SUMMARY, "steady")
        loop._thread.join(2)
        assert len(td.decisions) == 1
        clock.advance(18); wall.advance(18)
        loop.on_tick(SUMMARY, "steady")
        assert entered.wait(2)
        clock.advance(6); wall.advance(6)
        assert not loop.on_tick(SUMMARY, "steady")
        clock.advance(1); wall.advance(1)
        assert loop.on_tick(dict(SUMMARY, energy=0.1), "breakdown")
        assert len(td.decisions) == 2
        record = loop.decisions[-1]
        assert record["t"] == 25
        assert record["source"] == "rule (AI freshness watchdog)"
        assert record["section"] == "breakdown"
        assert record["decision"]["feedback"] <= 0.5
        assert client.calls == 2  # the hung worker is never duplicated
        release.set()
        loop._thread.join(2)
        assert len(td.decisions) == 2  # late GPT is discarded
        assert not loop.on_tick(SUMMARY, "breakdown")  # no immediate burst
        clock.advance(18); wall.advance(18)
        assert loop.on_tick(SUMMARY, "breakdown")
        loop._thread.join(2)
        assert loop.decisions[-1]["source"] == "gpt"
        assert len(td.decisions) == loop.history.count == 3
    finally:
        release.set()
        loop.close()


def test_watchdog_uses_wall_clock_and_initial_request_budget():
    clock, wall = FakeClock(), FakeClock()
    entered, release = threading.Event(), threading.Event()

    class Client:
        def decide(self, prompt):
            entered.set()
            assert release.wait(5)
            return dict(VALID)

    td = RecordingTD()
    loop = DirectorLoop(td, GPTDirector(Client(), RuleDirector()), clock=clock,
                        wall_clock=wall, out=io.StringIO())
    try:
        loop.on_tick(SUMMARY, "steady")
        assert entered.wait(2)
        clock.advance(500); wall.advance(1)
        assert not loop.on_tick(SUMMARY, "steady")
        assert not td.decisions
        wall.advance(24)
        assert loop.on_tick(SUMMARY, "steady")
        assert len(td.decisions) == 1
        # A permanently stuck call still permits subsequent rule publications.
        wall.advance(25); clock.advance(500)
        loop.on_tick(SUMMARY, "steady")
        assert len(td.decisions) == 2
        loop.set_mode("manual")
        wall.advance(25); clock.advance(500)
        loop.on_tick(SUMMARY, "steady")
        assert len(td.decisions) == 2
    finally:
        release.set()
        loop.close()


def test_stale_fallback_keeps_failure_reason_and_original_latency():
    clock = FakeClock()

    class Timeout:
        def decide(self, prompt):
            clock.advance(20)
            raise CodexError("quota exhausted")

    loop = DirectorLoop(RecordingTD(), GPTDirector(Timeout(), RuleDirector(), clock=clock),
                        clock=clock, worker=False, out=io.StringIO())
    loop.on_tick(SUMMARY, "build")
    record = loop.decisions[-1]
    assert record["source"] == "rule (stale decision)"
    assert record["fallback_source"] == "rule (CodexError)"
    assert record["latency_s"] == 20
    assert record["error"] == "quota exhausted"
    assert all(not k.startswith("_") for k in record["decision"])


def test_failed_transport_does_not_count_as_published_history():
    class FailedTD(RecordingTD):
        def send_director(self, *args, **kwargs):
            return None

        def send_heartbeat(self, n):
            return False

    loop, clock = rule_loop(FailedTD())
    loop.on_tick(SUMMARY, "steady")
    assert loop.errors == 1
    assert loop.decisions == []
    assert loop.history.count == loop.heartbeat == 0
    loop.set_mode("manual")
    clock.advance(18)
    loop.on_tick(SUMMARY, "steady")
    assert loop.errors == 2
    assert loop.heartbeat == 0


def test_manual_heartbeats_continue_while_old_gpt_is_stuck():
    clock, wall, td = FakeClock(), FakeClock(), RecordingTD()
    entered, release = threading.Event(), threading.Event()

    class Client:
        def decide(self, prompt):
            entered.set()
            assert release.wait(5)
            return dict(VALID)

    loop = DirectorLoop(td, GPTDirector(Client(), RuleDirector()), clock=clock,
                        wall_clock=wall, out=io.StringIO())
    try:
        loop.on_tick(SUMMARY, "steady")
        assert entered.wait(2)
        loop.set_mode("manual")
        clock.advance(18); wall.advance(18)
        assert loop.on_tick(SUMMARY, "steady")
        assert td.heartbeats == [1]
        assert not td.decisions
    finally:
        release.set()
        loop.close()


def test_publication_and_mode_change_are_serialized():
    """Once set_mode returns, an earlier reply cannot reach the transport."""
    td = RecordingTD()
    publishing, release, changed = threading.Event(), threading.Event(), threading.Event()

    class PausedPublish(DirectorLoop):
        def _publish(self, *args, **kwargs):
            publishing.set()
            assert release.wait(5)
            return super()._publish(*args, **kwargs)

    loop = PausedPublish(td, RuleDirector(), mode="rule", out=io.StringIO())
    changer = threading.Thread(target=lambda: (loop.set_mode("manual"), changed.set()))
    try:
        loop.on_tick(SUMMARY, "steady")
        assert publishing.wait(2)
        changer.start()
        assert not changed.wait(0.05)
        release.set()
        loop._thread.join(2)
        assert changed.wait(2)
        published = len(td.decisions)
        loop.on_tick(SUMMARY, "steady")
        assert len(td.decisions) == published
        assert loop.mode == "manual"
    finally:
        release.set()
        changer.join(2)
        loop.close()


@pytest.mark.parametrize("latency", [11, 13])
@pytest.mark.parametrize("outage_result", ["timeout", "late_success"])
def test_watchdog_recovers_realistic_latency_after_outage(latency, outage_result):
    """A provisional rule look must not permanently demote healthy slow AI."""
    clock, wall, td = FakeClock(), FakeClock(), RecordingTD()
    starts = []

    class Client:
        def decide(self, prompt):
            starts.append(wall())
            request = len(starts)
            delay = 0 if request == 1 else 30 if request == 2 else latency
            for _ in range(delay):
                clock.advance(1)
                wall.advance(1)
                loop.on_tick(SUMMARY, "steady")
            if request == 2 and outage_result == "timeout":
                raise CodexError("simulated outage timeout")
            return dict(VALID)

    loop = DirectorLoop(td, GPTDirector(Client(), RuleDirector(), clock=wall),
                        clock=clock, wall_clock=wall, worker=False, out=io.StringIO())
    while wall() < 140:
        loop.on_tick(SUMMARY, "steady")
        clock.advance(1)
        wall.advance(1)
    gpt = [r for r in loop.decisions if r["source"] == "gpt"]
    assert starts[:3] == [0, 18, 49]
    assert len(gpt) == len(starts) - 1  # only the actually timed-out/aged request is dropped
    assert gpt[1]["t"] == 49 + latency
    assert all(b - a >= loop.period_s for a, b in zip(starts, starts[1:]))
    assert max(b["t"] - a["t"] for a, b in zip(loop.decisions, loop.decisions[1:])) <= 25
    assert loop.history.count == len(loop.decisions)


@pytest.mark.parametrize("period", [25, 30, 60])
@pytest.mark.parametrize("latency", [0, 13])
def test_watchdog_never_starves_configured_ai_period(period, latency):
    clock, wall = FakeClock(), FakeClock()
    starts = []

    class Client:
        def decide(self, prompt):
            starts.append(wall())
            # Begin with a successful cached/fast reply, then realistic latency.
            for _ in range(latency if len(starts) > 1 else 0):
                clock.advance(1)
                wall.advance(1)
                loop.on_tick(SUMMARY, "steady")
            return dict(VALID)

    loop = DirectorLoop(RecordingTD(), GPTDirector(Client(), RuleDirector(), clock=wall),
                        period_s=period, clock=clock, wall_clock=wall,
                        worker=False, out=io.StringIO())
    while wall() <= 180:
        loop.on_tick(SUMMARY, "steady")
        clock.advance(1)
        wall.advance(1)
    assert len(starts) >= 180 // (period + 1)
    assert all(period <= b - a <= period + 1 for a, b in zip(starts, starts[1:]))
    assert sum(r["source"] == "gpt" for r in loop.decisions) == len(starts)
    assert max(b["t"] - a["t"] for a, b in zip(loop.decisions, loop.decisions[1:])) <= 25


def test_fresh_gpt_after_watchdog_rechecks_current_history_variety():
    clock, wall = FakeClock(), FakeClock()
    seen_watchdog = []

    class Rule(RuleDirector):
        def decide(self, *args, **kwargs):
            result = dict(VALID)
            result["_source"] = "rule"
            return result

    class Client:
        calls = 0

        def decide(self, prompt):
            self.calls += 1
            if self.calls > 1:
                for _ in range(13):
                    clock.advance(1)
                    wall.advance(1)
                    loop.on_tick(SUMMARY, "steady")
                    if loop.decisions[-1]["source"] == "rule (AI freshness watchdog)":
                        seen_watchdog.append(loop.decisions[-1])
            return dict(VALID)

    loop = DirectorLoop(RecordingTD(), GPTDirector(Client(), Rule(), clock=wall),
                        clock=clock, wall_clock=wall, worker=False, out=io.StringIO())
    loop.on_tick(SUMMARY, "steady")
    clock.advance(18); wall.advance(18)
    loop.on_tick(SUMMARY, "steady")
    assert seen_watchdog
    assert loop.decisions[-1]["source"] == "gpt"
    assert loop.decisions[-1]["t"] == 31
    previous = seen_watchdog[-1]["decision"]
    current = loop.decisions[-1]["decision"]
    assert (current["scene"], current["palette"]) != (previous["scene"], previous["palette"])


def test_rule_to_gpt_preserves_last_publication_freshness_budget():
    clock, wall = FakeClock(), FakeClock()

    class Client:
        def decide(self, prompt):
            for _ in range(10):
                clock.advance(1)
                wall.advance(1)
                loop.on_tick(SUMMARY, "steady")
            return dict(VALID)

    loop = DirectorLoop(RecordingTD(), GPTDirector(Client(), RuleDirector(), clock=wall),
                        mode="rule", clock=clock, wall_clock=wall,
                        worker=False, out=io.StringIO())
    loop.on_tick(SUMMARY, "steady")
    clock.advance(18); wall.advance(18)
    loop.set_mode("gpt")
    loop.on_tick(SUMMARY, "steady")
    assert [(r["t"], r["source"]) for r in loop.decisions] == [
        (0, "rule"), (25, "rule (AI freshness watchdog)"), (28, "gpt")]


def test_rapid_automatic_mode_toggles_cannot_postpone_watchdog():
    clock, wall = FakeClock(), FakeClock()
    loop = DirectorLoop(RecordingTD(), GPTDirector(StubClient(VALID), RuleDirector()),
                        mode="rule", period_s=60, clock=clock, wall_clock=wall,
                        worker=False, out=io.StringIO())
    loop.on_tick(SUMMARY, "steady")
    for instant, mode in [(5, "gpt"), (10, "rule"), (15, "gpt"),
                          (20, "rule"), (24, "gpt")]:
        clock.t = wall.t = instant
        loop.set_mode(mode)
    clock.t = wall.t = 25
    assert loop.on_tick(SUMMARY, "steady")
    assert [(r["t"], r["source"]) for r in loop.decisions] == [
        (0, "rule"), (25, "rule (AI freshness watchdog)")]


def test_resuming_manual_control_starts_a_new_freshness_budget():
    clock, wall = FakeClock(), FakeClock()
    loop = DirectorLoop(RecordingTD(), GPTDirector(StubClient(VALID), RuleDirector()),
                        period_s=1000, clock=clock, wall_clock=wall,
                        worker=False, out=io.StringIO())
    loop.on_tick(SUMMARY, "steady")
    clock.t = wall.t = 20
    loop.set_mode("manual")
    clock.t = wall.t = 100
    loop.set_mode("gpt")
    assert not loop.on_tick(SUMMARY, "steady")
    clock.t = wall.t = 124
    assert not loop.on_tick(SUMMARY, "steady")
    assert len(loop.decisions) == 1
    clock.t = wall.t = 125
    assert loop.on_tick(SUMMARY, "steady")
    assert loop.decisions[-1]["source"] == "rule (AI freshness watchdog)"
    assert loop.decisions[-1]["t"] == 125
    assert loop.decisions[-1]["latency_s"] == 25


def test_rule_takeover_is_immediate_and_keeps_cadence_while_old_gpt_hangs():
    clock, wall, td = FakeClock(), FakeClock(), RecordingTD()
    entered, release = threading.Event(), threading.Event()

    class Client:
        calls = 0

        def decide(self, prompt):
            self.calls += 1
            if self.calls == 2:
                entered.set()
                assert release.wait(5)
            return dict(VALID)

    client = Client()
    loop = DirectorLoop(td, GPTDirector(client, RuleDirector()),
                        clock=clock, wall_clock=wall, out=io.StringIO())
    try:
        loop.on_tick(SUMMARY, "steady")
        loop._thread.join(2)
        clock.t = wall.t = 18
        loop.on_tick(SUMMARY, "steady")
        assert entered.wait(2)
        original_worker = loop._thread
        clock.t = wall.t = 20
        loop.set_mode("rule")
        assert loop.on_tick(SUMMARY, "steady")
        assert [r["t"] for r in loop.decisions] == [0, 20]
        assert loop.decisions[-1]["source"] == "rule"
        assert loop._inflight
        assert loop._thread is original_worker
        assert not loop.on_tick(SUMMARY, "steady")  # takeover is not repeated
        clock.t = wall.t = 37
        assert not loop.on_tick(SUMMARY, "steady")
        clock.t = wall.t = 38
        assert loop.on_tick(SUMMARY, "steady")
        assert [r["t"] for r in loop.decisions] == [0, 20, 38]
        assert loop._thread is original_worker
        assert client.calls == 2
        release.set()
        original_worker.join(2)
        assert not loop._inflight
        assert len(loop.decisions) == 3  # obsolete GPT reply remains suppressed
        clock.t = wall.t = 56
        assert loop.on_tick(SUMMARY, "steady")
        loop._thread.join(2)
        assert loop.decisions[-1]["t"] == 56
        assert loop.decisions[-1]["source"] == "rule"
    finally:
        release.set()
        loop.close()
