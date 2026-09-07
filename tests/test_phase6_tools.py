"""Tests for the Phase 6 show-hardening tools.

Three rules shaped this file:

* **No Codex quota.** The one item that really executes a subprocess is the
  fallback drill, and it drives ``tools/fake_codex.py`` in ``fail`` mode — the
  exact thing it is supposed to prove, for free.
* **No audio hardware.** ``tools/audio_check.py``'s two commands are mocked at
  the seam ``tools/preflight.py`` calls them through, so these tests pass on a
  machine with no interface and no microphone permission.
* **No real ``~/.codex``.** Every ``tools/codex_sessions.py`` test builds its
  own sessions tree in ``tmp_path``. Nothing here can move or delete a real
  session file.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

from amv.codex_client import SANDBOX_CWD, CodexError
from amv.director import SYSTEM_PROMPT, DirectorLoop, GPTDirector, History, RuleDirector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import codex_sessions  # noqa: E402  (needs the path above)
import dry_run  # noqa: E402
import preflight  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_CODEX = REPO_ROOT / "tools" / "fake_codex.py"

DECISION = {
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


def record(t: float, scene: str, palette: str, *, latency: float = 12.0, source: str = "gpt") -> dict:
    decision = dict(DECISION, scene=scene, palette=palette)
    return {
        "t": t,
        "wall": "2026-09-08T22:00:00",
        "section": "steady",
        "source": source,
        "latency_s": latency,
        "heartbeat": int(t),
        "decision": decision,
    }


# ===========================================================================
# tools/preflight.py
# ===========================================================================


class FakeAudioCheck:
    """Enough of ``tools/audio_check.py`` for the two audio rows."""

    class CheckError(RuntimeError):
        pass

    METER_MIN_RMS = 0.01
    ROUTING_HINT = "系統輸出還是喇叭？"

    def __init__(self, available: bool = True) -> None:
        self.available = available

    def load_sounddevice(self):
        if not self.available:
            raise self.CheckError("sounddevice is not available\ninstall the audio extra")
        return object()


def install_audio(monkeypatch, module: FakeAudioCheck, payload: dict, code: int = 0) -> list:
    """Point preflight's two audio seams at ``module`` / ``payload``."""
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        return code, payload

    monkeypatch.setattr(preflight, "_audio_check", lambda: module)
    monkeypatch.setattr(preflight, "_run_audio_command", run)
    return calls


class TestEnvironmentCheck:
    def test_all_present_is_ok(self, monkeypatch):
        monkeypatch.setattr(
            preflight.check_env,
            "CHECKS",
            [("python", lambda: "3.11.9 (ok)"), ("codex binary", lambda: "/bin/codex — 1.0")],
        )
        result = preflight.check_environment()
        assert result.status == preflight.OK
        assert result.mark == "✅"

    def test_missing_required_row_fails(self, monkeypatch):
        monkeypatch.setattr(
            preflight.check_env,
            "CHECKS",
            [("python", lambda: "3.11.9 (ok)"), ("codex binary", lambda: "missing")],
        )
        result = preflight.check_environment()
        assert result.status == preflight.FAIL
        assert "codex binary" in result.reason
        assert any("codex binary" in line for line in result.detail)

    def test_missing_optional_row_only_warns(self, monkeypatch):
        monkeypatch.setattr(
            preflight.check_env,
            "CHECKS",
            [("python", lambda: "3.11.9 (ok)"), ("projectM", lambda: "missing (optional)")],
        )
        result = preflight.check_environment()
        assert result.status == preflight.WARN
        assert "projectM" in result.reason

    def test_old_python_is_a_failure_not_a_warning(self, monkeypatch):
        monkeypatch.setattr(
            preflight.check_env, "CHECKS", [("python", lambda: "3.9.6 (TOO OLD, need >= 3.11)")]
        )
        assert preflight.check_environment().status == preflight.FAIL

    def test_a_row_that_raises_does_not_abort_the_row(self, monkeypatch):
        def boom():
            raise RuntimeError("nope")

        monkeypatch.setattr(preflight.check_env, "CHECKS", [("python", boom)])
        result = preflight.check_environment()
        assert result.status == preflight.FAIL
        assert "nope" in result.data["rows"]["python"]


class TestAudioChecks:
    def test_loopback_ok(self, monkeypatch):
        payload = {"ok": True, "measured_hz": 1000.2, "recorded_rms": 0.178}
        calls = install_audio(monkeypatch, FakeAudioCheck(), payload)
        result = preflight.check_loopback(1.5)
        assert result.status == preflight.OK
        assert calls[0][0] == "loopback" and "--json" in calls[0]

    def test_loopback_failure_is_a_failure(self, monkeypatch):
        install_audio(monkeypatch, FakeAudioCheck(), {"ok": False, "recorded_rms": 0.0}, code=1)
        assert preflight.check_loopback().status == preflight.FAIL

    def test_loopback_skipped_without_sounddevice(self, monkeypatch):
        install_audio(monkeypatch, FakeAudioCheck(available=False), {})
        result = preflight.check_loopback()
        assert result.status == preflight.WARN
        assert "skipped" in result.reason

    def test_routing_with_signal_is_ok(self, monkeypatch):
        payload = {"mean_rms": 0.21, "min_rms_threshold": 0.01, "kicks_per_min": 145.0}
        calls = install_audio(monkeypatch, FakeAudioCheck(), payload)
        result = preflight.check_routing(3.0)
        assert result.status == preflight.OK
        assert calls[0][:3] == ["meter", "--seconds", "3"]

    def test_silence_warns_rather_than_fails(self, monkeypatch):
        """Nothing playing yet is the normal state at load-in, not a fault."""
        install_audio(monkeypatch, FakeAudioCheck(), {"mean_rms": 0.0004, "min_rms_threshold": 0.01})
        result = preflight.check_routing()
        assert result.status == preflight.WARN
        assert "no signal" in result.reason

    def test_routing_skipped_without_sounddevice(self, monkeypatch):
        install_audio(monkeypatch, FakeAudioCheck(available=False), {})
        assert preflight.check_routing().status == preflight.WARN


class TestCodexChecks:
    def test_binary_version(self, monkeypatch):
        monkeypatch.setenv("AMV_CODEX_BIN", str(FAKE_CODEX))
        result = preflight.check_codex_binary()
        assert result.status == preflight.OK
        assert "amv fake" in result.reason

    def test_binary_missing(self, monkeypatch):
        monkeypatch.setenv("AMV_CODEX_BIN", "/nonexistent/codex")
        assert preflight.check_codex_binary().status == preflight.FAIL

    def test_smoke_is_skipped_without_permission(self, monkeypatch):
        monkeypatch.delenv("AMV_CODEX_BIN", raising=False)
        result = preflight.check_codex_smoke(real=False)
        assert result.status == preflight.WARN
        assert "--real-codex" in result.reason

    def test_smoke_runs_against_the_fake_when_env_points_at_it(self, monkeypatch):
        monkeypatch.setenv("AMV_CODEX_BIN", str(FAKE_CODEX))
        monkeypatch.setenv("AMV_FAKE_CODEX_MODE", "ok")
        monkeypatch.setenv("AMV_FAKE_CODEX_DELAY", "0")
        result = preflight.check_codex_smoke(real=False)
        assert result.status == preflight.OK
        assert result.data["real"] is False
        assert result.data["decision"]["scene"]

    def test_smoke_failure_is_reported(self, monkeypatch):
        monkeypatch.setenv("AMV_CODEX_BIN", str(FAKE_CODEX))
        monkeypatch.setenv("AMV_FAKE_CODEX_MODE", "fail")
        monkeypatch.setenv("AMV_FAKE_CODEX_DELAY", "0")
        assert preflight.check_codex_smoke(real=False).status == preflight.FAIL


class TestFallbackDrill:
    """Really runs: fake codex fails, the rule director has to answer in time."""

    def test_drill_passes_and_the_decision_is_the_rule_director(self, monkeypatch):
        monkeypatch.setenv("AMV_FAKE_CODEX_DELAY", "0")
        result = preflight.check_fallback_drill()
        assert result.status == preflight.OK, result.reason
        assert result.data["source"].startswith("rule")
        assert result.data["elapsed_s"] <= preflight.FALLBACK_DEADLINE_S
        assert result.data["scene"]

    def test_drill_restores_the_environment_it_borrowed(self, monkeypatch):
        monkeypatch.setenv("AMV_FAKE_CODEX_MODE", "ok")
        monkeypatch.setenv("AMV_FAKE_CODEX_DELAY", "0")
        preflight.check_fallback_drill()
        assert os.environ["AMV_FAKE_CODEX_MODE"] == "ok"

    def test_an_impossible_deadline_fails(self, monkeypatch):
        monkeypatch.setenv("AMV_FAKE_CODEX_DELAY", "0.3")
        result = preflight.check_fallback_drill(deadline_s=0.0001)
        assert result.status == preflight.FAIL


class TestSpaceAndSessions:
    def test_enough_space(self, tmp_path):
        result = preflight.check_record_space(tmp_path, min_gb=0.0)
        assert result.status == preflight.OK

    def test_not_enough_space(self, tmp_path):
        result = preflight.check_record_space(tmp_path, min_gb=10**9)
        assert result.status == preflight.FAIL
        assert result.data["free_gb"] >= 0

    def test_missing_record_dir_warns(self, tmp_path):
        result = preflight.check_record_space(tmp_path / "not-there", min_gb=0.0)
        assert result.status == preflight.WARN

    def test_session_count_under_threshold(self, tmp_path):
        (tmp_path / "a.jsonl").write_text("x", encoding="utf-8")
        result = preflight.check_sessions(tmp_path, warn_at=500)
        assert result.status == preflight.OK
        assert result.data["count"] == 1

    def test_session_pile_up_warns(self, tmp_path):
        nested = tmp_path / "2026" / "09"
        nested.mkdir(parents=True)
        for i in range(5):
            (nested / f"{i}.jsonl").write_text("x", encoding="utf-8")
        result = preflight.check_sessions(tmp_path, warn_at=3)
        assert result.status == preflight.WARN
        assert result.data["count"] == 5

    def test_absent_sessions_dir_is_fine(self, tmp_path):
        assert preflight.check_sessions(tmp_path / "gone").status == preflight.OK


class TestChecklist:
    def test_run_checks_covers_the_documented_order(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AMV_CODEX_BIN", raising=False)
        monkeypatch.setenv("AMV_FAKE_CODEX_DELAY", "0")
        seen: list[str] = []
        results = preflight.run_checks(
            skip_audio=True,
            record_dir=tmp_path,
            sessions_dir=tmp_path,
            on_result=lambda result: seen.append(result.name),
        )
        assert [r.name for r in results] == list(preflight.CHECK_ORDER)
        assert seen == list(preflight.CHECK_ORDER)

    def test_a_check_that_explodes_becomes_a_failed_row(self, monkeypatch, tmp_path):
        def boom():
            raise RuntimeError("checker is broken")

        monkeypatch.setattr(preflight, "check_environment", boom)
        results = preflight.run_checks(skip_audio=True, record_dir=tmp_path, sessions_dir=tmp_path)
        first = results[0]
        assert first.status == preflight.FAIL and "checker is broken" in first.reason

    def test_format_report_names_the_failures(self):
        results = [
            preflight.Check("a", preflight.OK, "fine"),
            preflight.Check("b", preflight.FAIL, "broken"),
        ]
        text = "\n".join(preflight.format_report(results))
        assert "上場前必須修掉：b" in text

    def test_main_json_exits_zero_without_failures(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("AMV_CODEX_BIN", str(FAKE_CODEX))
        monkeypatch.setenv("AMV_FAKE_CODEX_DELAY", "0")
        monkeypatch.setattr(
            preflight.check_env, "CHECKS", [("python", lambda: "3.11.9 (ok)")]
        )
        code = preflight.main(
            [
                "--json",
                "--skip-audio",
                "--record-dir",
                str(tmp_path),
                "--sessions-dir",
                str(tmp_path),
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["ok"] is True
        assert [c["name"] for c in payload["checks"]] == list(preflight.CHECK_ORDER)


# ===========================================================================
# tools/dry_run.py
# ===========================================================================


class TestPercentile:
    def test_p95_of_one_to_one_hundred(self):
        assert dry_run.percentile(list(range(1, 101)), 0.95) == pytest.approx(95.05)

    def test_p50_interpolates(self):
        assert dry_run.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)

    def test_edges(self):
        assert dry_run.percentile([], 0.95) == 0.0
        assert dry_run.percentile([7.0], 0.95) == 7.0
        assert dry_run.percentile([1.0, 9.0], 1.0) == 9.0


class TestLoadRecords:
    def test_skips_blank_and_broken_lines_and_sorts(self, tmp_path):
        path = tmp_path / "decisions.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(record(30.0, "tunnel", "infrared")),
                    "",
                    "{not json",
                    json.dumps({"t": 5.0}),  # no "decision" key: not a decision line
                    json.dumps(record(10.0, "tunnel", "violet_cyan")),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        records = dry_run.load_records(path)
        assert [r["t"] for r in records] == [10.0, 30.0]

    def test_missing_file_is_empty(self, tmp_path):
        assert dry_run.load_records(tmp_path / "nope.jsonl") == []


class TestSummarise:
    def test_counts_sources_gaps_and_latency(self):
        records = [
            record(0.0, "tunnel", "violet_cyan", latency=10.0),
            record(18.0, "kaleido_mesh", "infrared", latency=14.0, source="rule (CodexError)"),
            record(36.0, "particle_field", "acid_lime", latency=12.0),
        ]
        summary = dry_run.summarise(records, show_seconds=36.0)
        assert summary["decisions"] == 3
        assert summary["by_source"]["gpt"] == 2
        assert summary["gap_s"]["min"] == 18.0 and summary["gap_s"]["max"] == 18.0
        assert summary["latency_s"]["mean"] == pytest.approx(12.0)
        assert summary["latency_s"]["max"] == 14.0
        assert summary["gpt_share"] == pytest.approx(2 / 3, abs=1e-3)

    def test_p95_latency_matches_the_percentile_helper(self):
        records = [record(float(i) * 18, "tunnel", "infrared", latency=float(i)) for i in range(1, 21)]
        summary = dry_run.summarise(records, show_seconds=360.0)
        assert summary["latency_s"]["p95"] == pytest.approx(dry_run.percentile(list(range(1, 21)), 0.95))
        assert summary["latency_s"]["p95"] == pytest.approx(19.05)

    def test_repeat_inside_the_window_is_counted(self):
        records = [
            record(0.0, "tunnel", "violet_cyan"),
            record(20.0, "kaleido_mesh", "infrared"),
            record(40.0, "tunnel", "violet_cyan"),  # 40 s after the first: a repeat
        ]
        summary = dry_run.summarise(records, show_seconds=40.0)
        assert summary["repeats_within_window"] == 1
        hit = summary["repeats"][0]
        assert (hit["scene"], hit["palette"], hit["apart_s"]) == ("tunnel", "violet_cyan", 40.0)

    def test_repeat_outside_the_window_is_not_counted(self):
        records = [
            record(0.0, "tunnel", "violet_cyan"),
            record(70.0, "tunnel", "violet_cyan"),
        ]
        assert dry_run.summarise(records, show_seconds=70.0)["repeats_within_window"] == 0

    def test_longest_run_without_a_scene_change(self):
        records = [
            record(0.0, "tunnel", "violet_cyan"),
            record(18.0, "tunnel", "infrared"),
            record(36.0, "tunnel", "acid_lime"),
            record(54.0, "kaleido_mesh", "mono_white"),
        ]
        summary = dry_run.summarise(records, show_seconds=54.0)
        assert summary["longest_scene_run"] == 3
        assert summary["longest_scene_run_s"] == 36.0

    def test_projected_tokens(self):
        records = [record(float(i) * 360.0, "tunnel", f"p{i}") for i in range(10)]
        summary = dry_run.summarise(records, show_seconds=3600.0, tokens_per_decision=25000)
        assert summary["decisions_per_hour"] == pytest.approx(10.0)
        assert summary["tokens_per_hour"] == 250_000
        assert summary["tokens_per_5h_window"] == 1_250_000

    def test_spec_baseline_reproduces_the_spec_number(self):
        """SPEC §2: 每小時 180 次 ≈ 4.5M tokens."""
        records = [record(float(i) * 20.0, "tunnel", f"p{i}") for i in range(180)]
        summary = dry_run.summarise(records, show_seconds=3600.0)
        assert summary["decisions_per_hour"] == pytest.approx(180.0)
        assert summary["tokens_per_hour"] == 4_500_000

    def test_empty_log_does_not_divide_by_zero(self):
        summary = dry_run.summarise([], show_seconds=3600.0)
        assert summary["decisions"] == 0
        assert summary["tokens_per_hour"] == 0
        assert summary["gap_s"]["n"] == 0

    def test_show_seconds_defaults_to_the_log_span(self):
        records = [record(0.0, "tunnel", "a"), record(1800.0, "tunnel", "b")]
        assert dry_run.summarise(records)["show_seconds"] == 1800.0


class TestRenderReport:
    META = {
        "finished_at": "2026-09-08T22:00:00",
        "command": "dry_run.py --minutes 60",
        "director": "gpt",
        "period": 18.0,
        "speed": 1.0,
        "live": False,
        "fake_td_runs": 31,
        "wall_seconds": 3600.0,
        "decisions_log": "/tmp/d.jsonl",
        "codex_bin": None,
    }

    def test_clean_run_reports_zero_repeats_and_the_quota_warning(self):
        records = [record(float(i) * 20.0, "tunnel", f"p{i}") for i in range(180)]
        text = dry_run.render_report(dry_run.summarise(records, show_seconds=3600.0), self.META)
        assert "4,500,000" in text
        assert "5 小時" in text and "每週窗" in text
        assert "✅ 60 s 內沒有重複" in text
        assert "period` 從 18 s 拉到 30 s" in text

    def test_repeats_get_their_own_table(self):
        records = [record(0.0, "tunnel", "violet_cyan"), record(30.0, "tunnel", "violet_cyan")]
        text = dry_run.render_report(dry_run.summarise(records, show_seconds=30.0), self.META)
        assert "❌ 應為 0" in text
        assert "enforce_variety" in text

    def test_a_fallback_heavy_run_says_so(self):
        records = [
            record(0.0, "tunnel", "a"),
            record(20.0, "kaleido_mesh", "b", source="rule (CodexError)"),
        ]
        text = dry_run.render_report(dry_run.summarise(records, show_seconds=20.0), self.META)
        assert "1/2 次決策確實由 fallback 接手" in text


# ===========================================================================
# tools/codex_sessions.py
# ===========================================================================


OURS_BY_CWD = json.dumps(
    {"type": "session_meta", "payload": {"cwd": str(SANDBOX_CWD), "originator": "codex_exec"}},
    ensure_ascii=False,
)
OURS_BY_PROMPT = json.dumps(
    {"type": "message", "payload": {"text": SYSTEM_PROMPT.splitlines()[0]}}, ensure_ascii=False
)
SOMEONE_ELSES = json.dumps(
    {"type": "session_meta", "payload": {"cwd": "/Users/leo/Repos/other", "originator": "vscode"}},
    ensure_ascii=False,
)


def build_sessions(root: Path) -> dict[str, Path]:
    """A miniature ``~/.codex/sessions`` with two of ours and two that are not."""
    day = root / "2026" / "09" / "08"
    day.mkdir(parents=True)
    files = {
        "cwd": day / "rollout-cwd.jsonl",
        "prompt": day / "rollout-prompt.jsonl",
        "other": day / "rollout-other.jsonl",
        "binary": day / "rollout-binary.jsonl",
    }
    files["cwd"].write_text(OURS_BY_CWD + "\n", encoding="utf-8")
    files["prompt"].write_text(OURS_BY_PROMPT + "\n", encoding="utf-8")
    files["other"].write_text(SOMEONE_ELSES + "\n", encoding="utf-8")
    files["binary"].write_bytes(b"\x00\x01\x02\xff not text at all")
    return files


def age(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


class TestMatching:
    def test_both_fingerprints_match_and_nothing_else_does(self, tmp_path):
        files = build_sessions(tmp_path)
        assert codex_sessions.matches(files["cwd"])
        assert codex_sessions.matches(files["prompt"])
        assert not codex_sessions.matches(files["other"])
        assert not codex_sessions.matches(files["binary"])

    def test_unreadable_file_is_not_ours(self, tmp_path):
        assert not codex_sessions.matches(tmp_path / "missing.jsonl")

    def test_fingerprint_beyond_the_sniff_window_is_missed_on_purpose(self, tmp_path):
        """A bounded read is the trade: cheap scans, and only the head counts."""
        path = tmp_path / "late.jsonl"
        path.write_text("x" * (codex_sessions.SNIFF_BYTES + 10) + str(SANDBOX_CWD), encoding="utf-8")
        assert not codex_sessions.matches(path)

    def test_discover_returns_only_ours_oldest_first(self, tmp_path):
        files = build_sessions(tmp_path)
        age(files["cwd"], 7200)
        found = codex_sessions.discover(tmp_path)
        assert [f.path for f in found] == [files["cwd"], files["prompt"]]

    def test_older_than_filters(self, tmp_path):
        files = build_sessions(tmp_path)
        age(files["cwd"], 86400 * 3)
        found = codex_sessions.discover(tmp_path, older_than_s=codex_sessions.parse_age("1d"))
        assert [f.path for f in found] == [files["cwd"]]

    def test_absent_root(self, tmp_path):
        assert codex_sessions.discover(tmp_path / "nope") == []


class TestParseAge:
    @pytest.mark.parametrize(
        "text,seconds",
        [("30s", 30.0), ("30m", 1800.0), ("12h", 43200.0), ("1d", 86400.0), ("2w", 1209600.0)],
    )
    def test_units(self, text, seconds):
        assert codex_sessions.parse_age(text) == seconds

    def test_bare_number_is_days(self):
        assert codex_sessions.parse_age("2") == 172800.0

    def test_zero_means_any_age(self):
        assert codex_sessions.parse_age("0") == 0.0

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            codex_sessions.parse_age("last tuesday")


class TestArchive:
    def test_moves_only_matches_and_keeps_the_layout(self, tmp_path):
        root, archive_dir = tmp_path / "sessions", tmp_path / "archive"
        files = build_sessions(root)
        moves = codex_sessions.archive(codex_sessions.discover(root), root, archive_dir)
        assert len(moves) == 2
        assert not files["cwd"].exists() and not files["prompt"].exists()
        assert files["other"].exists() and files["binary"].exists()
        assert (archive_dir / "2026" / "09" / "08" / "rollout-cwd.jsonl").is_file()

    def test_dry_run_moves_nothing(self, tmp_path):
        root, archive_dir = tmp_path / "sessions", tmp_path / "archive"
        files = build_sessions(root)
        moves = codex_sessions.archive(
            codex_sessions.discover(root), root, archive_dir, dry_run=True
        )
        assert len(moves) == 2
        assert files["cwd"].exists()
        assert not archive_dir.exists()

    def test_a_name_collision_does_not_overwrite(self, tmp_path):
        root, archive_dir = tmp_path / "sessions", tmp_path / "archive"
        build_sessions(root)
        codex_sessions.archive(codex_sessions.discover(root), root, archive_dir)
        build_sessions(root / "again")  # a second run producing the same names
        second = codex_sessions.discover(root)
        moves = codex_sessions.archive(second, root, archive_dir)
        assert all(dst.is_file() for _, dst in moves)
        assert len(list((archive_dir).rglob("rollout-cwd*.jsonl"))) == 2

    def test_cli_archive_dry_run(self, tmp_path, capsys):
        root, archive_dir = tmp_path / "sessions", tmp_path / "archive"
        files = build_sessions(root)
        age(files["cwd"], 86400 * 2)
        age(files["prompt"], 86400 * 2)
        code = codex_sessions.main(
            [
                "--sessions-dir",
                str(root),
                "--archive-dir",
                str(archive_dir),
                "archive",
                "--older-than",
                "1d",
                "--dry-run",
            ]
        )
        assert code == 0
        assert "would move 2" in capsys.readouterr().out
        assert files["cwd"].exists()

    def test_dry_run_after_the_subcommand_still_works(self, tmp_path, capsys):
        """``archive --dry-run`` must mean the same thing on either side."""
        root, archive_dir = tmp_path / "sessions", tmp_path / "archive"
        files = build_sessions(root)
        codex_sessions.main(
            [
                "archive",
                "--older-than",
                "0",
                "--dry-run",
                "--sessions-dir",
                str(root),
                "--archive-dir",
                str(archive_dir),
            ]
        )
        assert "would move 2" in capsys.readouterr().out
        assert files["cwd"].exists()


class TestClean:
    def test_refuses_to_delete_outside_the_archive(self, tmp_path):
        root = tmp_path / "sessions"
        build_sessions(root)
        with pytest.raises(ValueError, match="outside"):
            codex_sessions.clean(codex_sessions.discover(root), tmp_path / "archive")

    def test_deletes_only_matches_inside_the_archive(self, tmp_path):
        archive_dir = tmp_path / "archive"
        files = build_sessions(archive_dir)
        removed = codex_sessions.clean(codex_sessions.discover(archive_dir), archive_dir)
        assert len(removed) == 2
        assert not files["cwd"].exists()
        assert files["other"].exists()  # never matched, never touched

    def test_dry_run_deletes_nothing(self, tmp_path):
        archive_dir = tmp_path / "archive"
        files = build_sessions(archive_dir)
        removed = codex_sessions.clean(
            codex_sessions.discover(archive_dir), archive_dir, dry_run=True
        )
        assert len(removed) == 2 and files["cwd"].exists()

    def test_cli_refuses_without_yes(self, tmp_path, capsys):
        archive_dir = tmp_path / "archive"
        files = build_sessions(archive_dir)
        code = codex_sessions.main(
            ["--archive-dir", str(archive_dir), "clean", "--older-than", "0"]
        )
        assert code == 2
        assert "--yes" in capsys.readouterr().err
        assert files["cwd"].exists()

    def test_cli_deletes_with_yes(self, tmp_path):
        archive_dir = tmp_path / "archive"
        files = build_sessions(archive_dir)
        code = codex_sessions.main(
            ["--archive-dir", str(archive_dir), "clean", "--older-than", "0", "--yes"]
        )
        assert code == 0
        assert not files["cwd"].exists() and files["other"].exists()


class TestListCommand:
    def test_json_summary(self, tmp_path, capsys):
        root = tmp_path / "sessions"
        build_sessions(root)
        code = codex_sessions.main(["--sessions-dir", str(root), "--json", "list"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["count"] == 2
        assert payload["oldest"] and payload["newest"]

    def test_human_output_on_an_empty_tree(self, tmp_path, capsys):
        code = codex_sessions.main(["--sessions-dir", str(tmp_path), "list"])
        assert code == 0
        assert "沒有本專案產生的 session" in capsys.readouterr().out


# ===========================================================================
# amv.director.DirectorLoop.startup_check
# ===========================================================================


class StubClient:
    """A codex client that fails exactly where a test wants it to."""

    def __init__(self, version_exc: Exception | None = None, decide_exc: Exception | None = None):
        self.version_exc = version_exc
        self.decide_exc = decide_exc
        self.timeout = 30.0
        self.timeouts_seen: list[float] = []
        self.versions = 0
        self.decides = 0

    def version(self) -> str:
        self.versions += 1
        self.timeouts_seen.append(self.timeout)
        if self.version_exc:
            raise self.version_exc
        return "codex-cli 0.153.4"

    def decide(self, prompt: str) -> dict:
        self.decides += 1
        if self.decide_exc:
            raise self.decide_exc
        return dict(DECISION)


def loop_for(client: StubClient, mode: str = "gpt") -> tuple[DirectorLoop, io.StringIO]:
    out = io.StringIO()
    director = GPTDirector(client, RuleDirector())
    return DirectorLoop(None, director, mode=mode, worker=False, out=out), out


class TestStartupCheck:
    def test_version_failure_demotes_to_rule_and_warns(self):
        client = StubClient(version_exc=CodexError("unknown flag --output-schema"))
        loop, out = loop_for(client)
        assert loop.startup_check(timeout_s=5.0) == "rule"
        assert loop.mode == "rule"
        text = out.getvalue()
        assert "startup check failed" in text and "--output-schema" in text
        assert client.decides == 0

    def test_version_ok_stays_gpt(self):
        client = StubClient()
        loop, out = loop_for(client)
        assert loop.startup_check(timeout_s=5.0) == "gpt"
        assert loop.mode == "gpt"
        assert client.versions == 1 and client.decides == 0
        assert "startup check ok" in out.getvalue()

    def test_smoke_false_never_spends_quota(self):
        client = StubClient(decide_exc=CodexError("would have cost 25k tokens"))
        loop, _ = loop_for(client)
        assert loop.startup_check() == "gpt"
        assert client.decides == 0

    def test_smoke_true_makes_one_decision(self):
        client = StubClient()
        loop, out = loop_for(client)
        assert loop.startup_check(smoke=True) == "gpt"
        assert client.decides == 1
        assert "smoke → tunnel/violet_cyan" in out.getvalue()

    def test_smoke_failure_demotes(self):
        client = StubClient(decide_exc=CodexError("codex exec exited 1"))
        loop, out = loop_for(client)
        assert loop.startup_check(smoke=True) == "rule"
        assert loop.mode == "rule"
        assert "CodexError" in out.getvalue()

    def test_the_smoke_decision_is_thrown_away(self):
        client = StubClient()
        loop, _ = loop_for(client)
        loop.startup_check(smoke=True)
        assert len(loop.history) == 0
        assert loop.decisions == [] and loop.heartbeat == 0

    def test_timeout_is_applied_then_restored(self):
        client = StubClient()
        loop, _ = loop_for(client)
        loop.startup_check(timeout_s=3.0)
        assert client.timeouts_seen == [3.0]
        assert client.timeout == 30.0

    def test_a_longer_timeout_does_not_raise_the_clients_own(self):
        client = StubClient()
        loop, _ = loop_for(client)
        loop.startup_check(timeout_s=999.0)
        assert client.timeouts_seen == [30.0]

    def test_rule_mode_is_left_alone(self):
        client = StubClient(version_exc=CodexError("boom"))
        loop, out = loop_for(client, mode="rule")
        assert loop.startup_check() == "rule"
        assert client.versions == 0 and out.getvalue() == ""

    def test_manual_mode_is_left_alone(self):
        client = StubClient()
        loop, _ = loop_for(client, mode="manual")
        assert loop.startup_check() == "manual"
        assert client.versions == 0

    def test_gpt_mode_without_a_client_is_a_lie_and_gets_corrected(self):
        """``--director gpt`` after the sidecar already fell back at build time."""
        out = io.StringIO()
        loop = DirectorLoop(None, RuleDirector(), mode="gpt", worker=False, out=out)
        assert loop.startup_check() == "rule"
        assert "沒有 codex client" in out.getvalue()

    def test_keyboard_interrupt_is_not_swallowed(self):
        client = StubClient(version_exc=KeyboardInterrupt())
        loop, _ = loop_for(client)
        with pytest.raises(KeyboardInterrupt):
            loop.startup_check()

    def test_against_the_real_fake_codex_binary(self, monkeypatch):
        """End to end through CodexClient: fail mode must demote to rule."""
        from amv.codex_client import CodexClient

        monkeypatch.setenv("AMV_FAKE_CODEX_MODE", "fail")
        monkeypatch.setenv("AMV_FAKE_CODEX_DELAY", "0")
        client = CodexClient(binary=FAKE_CODEX, timeout=10.0)
        out = io.StringIO()
        loop = DirectorLoop(
            None, GPTDirector(client, RuleDirector()), mode="gpt", worker=False, out=out
        )
        assert loop.startup_check(timeout_s=10.0) == "gpt"  # --version still works
        assert loop.startup_check(timeout_s=10.0, smoke=True) == "rule"  # exec does not
        assert loop.mode == "rule"


def test_history_is_untouched_by_a_smoke_decision_even_after_real_ones():
    """The smoke test must not eat into the 60 s no-repeat window."""
    client = StubClient()
    loop, _ = loop_for(client)
    loop.history.append(dict(DECISION), 0.0, "gpt")
    loop.startup_check(smoke=True)
    assert len(loop.history) == 1
    assert loop.history.current_scene == "tunnel"
