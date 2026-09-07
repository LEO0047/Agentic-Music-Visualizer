"""Tests for amv.codex_client.

`codex exec` is never actually run here: it costs real Codex quota. Every test
monkeypatches subprocess.run and asserts on the argv we would have used.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from amv import codex_client
from amv.codex_client import CodexClient, CodexError, find_codex

VALID_DECISION = {
    "scene": "kaleido_mesh",
    "palette": "infrared",
    "feedback": 1.4,  # out of range on purpose: decide() must clamp to 0.98
    "symmetry": 7.6,  # must round to 8
    "camera_speed": 0.42,
    "particle_mode": "burst",
    "projectm_mix": 0.0,
    "transition": {"mode": "on_next_kick", "beats": 2},
    "on_drop": {"scene": "tunnel", "palette": "acid_lime", "particle_mode": "spiral"},
    "intent": "y" * 200,  # must truncate to 120
}


@pytest.fixture
def client(tmp_path: Path) -> CodexClient:
    return CodexClient(binary="/fake/bin/codex", cwd=tmp_path / "sandbox", timeout=12.5)


class FakeRun:
    """Stand-in for subprocess.run that records the call and fakes the outcome."""

    def __init__(self, *, payload=None, returncode=0, stderr="", raise_timeout=False, raw=None):
        self.payload = payload
        self.raw = raw
        self.returncode = returncode
        self.stderr = stderr
        self.raise_timeout = raise_timeout
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
        out_path = Path(argv[argv.index("-o") + 1]) if "-o" in argv else None
        if out_path is not None:
            if self.raw is not None:
                out_path.write_text(self.raw, encoding="utf-8")
            elif self.payload is not None:
                out_path.write_text(json.dumps(self.payload), encoding="utf-8")
        return subprocess.CompletedProcess(argv, self.returncode, stdout="", stderr=self.stderr)

    @property
    def argv(self) -> list[str]:
        return self.calls[-1][0]

    @property
    def kwargs(self) -> dict:
        return self.calls[-1][1]


def install(monkeypatch, fake: FakeRun) -> FakeRun:
    monkeypatch.setattr(codex_client.subprocess, "run", fake)
    return fake


# -- find_codex -------------------------------------------------------------


def test_find_codex_prefers_env(monkeypatch, tmp_path):
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("AMV_CODEX_BIN", str(binary))
    assert find_codex() == binary


def test_find_codex_env_pointing_at_nothing_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("AMV_CODEX_BIN", str(tmp_path / "nope"))
    with pytest.raises(CodexError):
        find_codex()


def test_find_codex_falls_back_to_path(monkeypatch):
    monkeypatch.delenv("AMV_CODEX_BIN", raising=False)
    monkeypatch.setattr(codex_client.shutil, "which", lambda name: "/usr/local/bin/codex")
    assert find_codex() == Path("/usr/local/bin/codex")


def test_find_codex_falls_back_to_plugin(monkeypatch, tmp_path):
    plugin = tmp_path / "plugin-codex"
    plugin.write_text("#!/bin/sh\n")
    plugin.chmod(0o755)
    monkeypatch.delenv("AMV_CODEX_BIN", raising=False)
    monkeypatch.setattr(codex_client.shutil, "which", lambda name: None)
    monkeypatch.setattr(codex_client, "PLUGIN_CODEX", plugin)
    assert find_codex() == plugin


def test_find_codex_missing_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("AMV_CODEX_BIN", raising=False)
    monkeypatch.setattr(codex_client.shutil, "which", lambda name: None)
    monkeypatch.setattr(codex_client, "PLUGIN_CODEX", tmp_path / "absent")
    with pytest.raises(CodexError):
        find_codex()


# -- argv and subprocess contract -------------------------------------------


def test_decide_builds_the_expected_argv(monkeypatch, client):
    fake = install(monkeypatch, FakeRun(payload=VALID_DECISION))
    client.decide("hello director")
    argv = fake.argv

    assert argv[0] == "/fake/bin/codex"
    assert argv[1] == "exec"
    assert argv[-1] == "hello director"

    assert "--output-schema" in argv
    assert argv[argv.index("--output-schema") + 1] == str(client.schema_path)

    assert "-o" in argv
    out_path = argv[argv.index("-o") + 1]
    assert out_path.endswith(".json")

    assert "-m" in argv and argv[argv.index("-m") + 1] == "gpt-6-astra"
    assert "-s" in argv and argv[argv.index("-s") + 1] == "read-only"
    assert "--skip-git-repo-check" in argv
    assert "-C" in argv and argv[argv.index("-C") + 1] == str(client.cwd)
    assert "-c" in argv and argv[argv.index("-c") + 1] == 'model_reasoning_effort="low"'


def test_decide_uses_devnull_stdin_and_captures_output(monkeypatch, client):
    """codex exec hangs waiting for stdin EOF without DEVNULL — this is load bearing."""
    fake = install(monkeypatch, FakeRun(payload=VALID_DECISION))
    client.decide("prompt")
    assert fake.kwargs["stdin"] is subprocess.DEVNULL
    assert fake.kwargs["capture_output"] is True


def test_decide_passes_the_timeout(monkeypatch, client):
    fake = install(monkeypatch, FakeRun(payload=VALID_DECISION))
    client.decide("prompt")
    assert fake.kwargs["timeout"] == 12.5


def test_effort_and_model_are_configurable(monkeypatch, tmp_path):
    c = CodexClient(binary="/fake/bin/codex", model="gpt-9", effort="high", cwd=tmp_path)
    fake = install(monkeypatch, FakeRun(payload=VALID_DECISION))
    c.decide("prompt")
    argv = fake.argv
    assert argv[argv.index("-m") + 1] == "gpt-9"
    assert argv[argv.index("-c") + 1] == 'model_reasoning_effort="high"'


def test_default_cwd_is_a_created_empty_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_client, "SANDBOX_CWD", tmp_path / "amv-codex-cwd")
    c = CodexClient(binary="/fake/bin/codex")
    assert c.cwd.is_dir()
    assert list(c.cwd.iterdir()) == []


# -- happy path -------------------------------------------------------------


def test_decide_returns_a_clamped_decision(monkeypatch, client):
    install(monkeypatch, FakeRun(payload=VALID_DECISION))
    decision = client.decide("prompt")
    assert decision["scene"] == "kaleido_mesh"
    assert decision["feedback"] == 0.98
    assert decision["symmetry"] == 8
    assert len(decision["intent"]) == 120
    assert decision["on_drop"] == {
        "scene": "tunnel",
        "palette": "acid_lime",
        "particle_mode": "spiral",
    }


# -- failure paths ----------------------------------------------------------


def test_non_zero_exit_raises_with_stderr_tail(monkeypatch, client):
    install(monkeypatch, FakeRun(returncode=2, stderr="stream disconnected before completion"))
    with pytest.raises(CodexError) as exc:
        client.decide("prompt")
    assert "2" in str(exc.value)
    assert "stream disconnected" in str(exc.value)


def test_timeout_raises_codex_error(monkeypatch, client):
    install(monkeypatch, FakeRun(raise_timeout=True))
    with pytest.raises(CodexError) as exc:
        client.decide("prompt")
    assert "timed out" in str(exc.value)


def test_missing_output_file_raises(monkeypatch, client):
    install(monkeypatch, FakeRun(payload=None))  # exits 0 but writes nothing
    with pytest.raises(CodexError) as exc:
        client.decide("prompt")
    assert "no output" in str(exc.value)


def test_unparseable_output_raises(monkeypatch, client):
    install(monkeypatch, FakeRun(raw="not json at all"))
    with pytest.raises(CodexError) as exc:
        client.decide("prompt")
    assert "valid JSON" in str(exc.value)


def test_schema_violating_output_raises(monkeypatch, client):
    bad = dict(VALID_DECISION)
    bad["scene"] = "wormhole"
    install(monkeypatch, FakeRun(payload=bad))
    with pytest.raises(CodexError):
        client.decide("prompt")


def test_missing_schema_file_raises(monkeypatch, tmp_path):
    c = CodexClient(binary="/fake/bin/codex", cwd=tmp_path, schema_path=tmp_path / "gone.json")
    with pytest.raises(CodexError) as exc:
        c.decide("prompt")
    assert "schema" in str(exc.value)


# -- version ----------------------------------------------------------------


def test_version_runs_codex_version(monkeypatch, client):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="codex-cli 0.153.4\n", stderr="")

    monkeypatch.setattr(codex_client.subprocess, "run", fake_run)
    assert client.version() == "codex-cli 0.153.4"
    assert calls[-1] == ["/fake/bin/codex", "--version"]


def test_version_failure_raises(monkeypatch, client):
    install(monkeypatch, FakeRun(returncode=127, stderr="not found"))
    with pytest.raises(CodexError):
        client.version()
