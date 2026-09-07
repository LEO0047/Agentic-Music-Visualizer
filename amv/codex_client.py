"""Thin wrapper around one-shot ``codex exec`` calls (SPEC §2, route A).

Every director decision is its own short-lived process: no daemon, no shared
state, and a hung call can only ever cost one cycle. Two details are load
bearing and easy to get wrong:

* ``stdin=subprocess.DEVNULL`` — ``codex exec`` waits for stdin EOF when it is
  not on a TTY, so without this the call hangs until the timeout.
* ``-C <empty dir>`` — codex is pointed at a dedicated empty directory so it
  never scans a project tree looking for context.

Auth comes from the existing ChatGPT login in ``~/.codex/auth.json``; this
module never reads, prints or copies that file.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .schema import SCHEMA_PATH, DecisionError, validate_and_clamp

__all__ = ["CodexError", "find_codex", "CodexClient", "DEFAULT_MODEL", "DEFAULT_EFFORT"]

DEFAULT_MODEL = "gpt-6-astra"
DEFAULT_EFFORT = "low"

#: Where codex-cli lives when it is installed as a Codex plugin rather than on PATH.
PLUGIN_CODEX = Path.home() / ".codex" / "plugins" / ".plugin-appserver" / "codex"

#: Dedicated empty directory handed to ``codex exec -C`` so it has nothing to scan.
SANDBOX_CWD = Path(tempfile.gettempdir()) / "amv-codex-cwd"

_STDERR_TAIL_CHARS = 1200


class CodexError(RuntimeError):
    """A ``codex exec`` call failed, timed out, or produced unusable output."""


def _tail(text: str | bytes | None, limit: int = _STDERR_TAIL_CHARS) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def find_codex() -> Path:
    """Locate the codex binary.

    Order: ``$AMV_CODEX_BIN``, then ``codex`` on ``PATH``, then the Codex
    plugin install at ``~/.codex/plugins/.plugin-appserver/codex``.
    """
    env = os.environ.get("AMV_CODEX_BIN")
    if env:
        candidate = Path(env).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        raise CodexError(f"AMV_CODEX_BIN points at {candidate}, which is not an executable file")
    on_path = shutil.which("codex")
    if on_path:
        return Path(on_path)
    if PLUGIN_CODEX.is_file() and os.access(PLUGIN_CODEX, os.X_OK):
        return PLUGIN_CODEX
    raise CodexError(
        "codex binary not found: set $AMV_CODEX_BIN, put `codex` on PATH, "
        f"or install it at {PLUGIN_CODEX}"
    )


class CodexClient:
    """Runs one ``codex exec`` per decision and returns a clamped decision dict."""

    def __init__(
        self,
        binary: str | os.PathLike[str] | None = None,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        cwd: str | os.PathLike[str] | None = None,
        timeout: float = 30.0,
        schema_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.binary = Path(binary) if binary is not None else find_codex()
        self.model = model
        self.effort = effort
        self.timeout = float(timeout)
        self.schema_path = Path(schema_path) if schema_path is not None else Path(SCHEMA_PATH)
        self.cwd = Path(cwd) if cwd is not None else SANDBOX_CWD
        self.cwd.mkdir(parents=True, exist_ok=True)

    # -- argv ---------------------------------------------------------------

    def build_argv(self, prompt: str, out_path: str | os.PathLike[str]) -> list[str]:
        """The exact command line used for a decision (also handy for logging)."""
        return [
            str(self.binary),
            "exec",
            "-m",
            self.model,
            "-c",
            f'model_reasoning_effort="{self.effort}"',
            "-s",
            "read-only",
            "--skip-git-repo-check",
            "-C",
            str(self.cwd),
            "--output-schema",
            str(self.schema_path),
            "-o",
            str(out_path),
            prompt,
        ]

    # -- calls --------------------------------------------------------------

    def decide(self, prompt: str) -> dict[str, Any]:
        """Ask the director for one decision. Raises :class:`CodexError` on any failure."""
        if not self.schema_path.is_file():
            raise CodexError(f"output schema not found at {self.schema_path}")
        with tempfile.TemporaryDirectory(prefix="amv-codex-out-") as tmp:
            out_path = Path(tmp) / "director_out.json"
            argv = self.build_argv(prompt, out_path)
            try:
                proc = subprocess.run(
                    argv,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise CodexError(
                    f"codex exec timed out after {self.timeout:g}s. stderr tail: {_tail(exc.stderr)}"
                ) from exc
            except OSError as exc:
                raise CodexError(f"could not launch {self.binary}: {exc}") from exc

            if proc.returncode != 0:
                raise CodexError(
                    f"codex exec exited {proc.returncode}. stderr tail: {_tail(proc.stderr)}"
                )
            if not out_path.is_file():
                raise CodexError(
                    f"codex exec wrote no output file at {out_path}. "
                    f"stderr tail: {_tail(proc.stderr)}"
                )
            raw = out_path.read_text(encoding="utf-8")

        if not raw.strip():
            raise CodexError("codex exec wrote an empty output file")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CodexError(f"codex output was not valid JSON: {exc}") from exc
        try:
            return validate_and_clamp(data)
        except DecisionError as exc:
            raise CodexError(f"codex output did not match the director schema: {exc}") from exc

    def version(self) -> str:
        """Return the output of ``codex --version``."""
        try:
            proc = subprocess.run(
                [str(self.binary), "--version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CodexError(f"codex --version timed out after {self.timeout:g}s") from exc
        except OSError as exc:
            raise CodexError(f"could not launch {self.binary}: {exc}") from exc
        if proc.returncode != 0:
            raise CodexError(
                f"codex --version exited {proc.returncode}. stderr tail: {_tail(proc.stderr)}"
            )
        return (proc.stdout or "").strip()
