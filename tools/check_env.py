#!/usr/bin/env python3
"""Phase 0 environment report — one row per thing the show depends on.

    uv run python tools/check_env.py

Read-only and always exits 0: it is a dashboard, not a gate. Stdlib only, so
it still runs before (or instead of) `uv sync`.

Note on secrets: this script only checks that ``~/.codex/auth.json`` exists and
reads the single ``auth_mode`` key. It never reads, prints or copies tokens.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

HOME = Path.home()
CODEX_DIR = HOME / ".codex"
PLUGIN_CODEX = CODEX_DIR / "plugins" / ".plugin-appserver" / "codex"
MISSING = "missing"


def run(argv: list[str], timeout: float = 25.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, (proc.stdout or "")


# -- individual checks ------------------------------------------------------


def check_python() -> str:
    v = sys.version_info
    ok = "ok" if (v.major, v.minor) >= (3, 11) else "TOO OLD, need >= 3.11"
    return f"{v.major}.{v.minor}.{v.micro} ({ok}) — {sys.executable}"


def check_uv() -> str:
    path = shutil.which("uv") or ("/opt/homebrew/bin/uv" if Path("/opt/homebrew/bin/uv").exists() else None)
    if not path:
        return MISSING
    code, out = run([path, "--version"], timeout=10)
    version = out.strip() if code == 0 else "version unknown"
    return f"{path} — {version}"


def find_codex() -> Path | None:
    env = os.environ.get("AMV_CODEX_BIN")
    if env and Path(env).expanduser().is_file():
        return Path(env).expanduser()
    on_path = shutil.which("codex")
    if on_path:
        return Path(on_path)
    if PLUGIN_CODEX.is_file():
        return PLUGIN_CODEX
    return None


def check_codex() -> str:
    binary = find_codex()
    if binary is None:
        return MISSING
    code, out = run([str(binary), "--version"], timeout=20)
    version = out.strip().splitlines()[0] if (code == 0 and out.strip()) else "version unknown"
    return f"{binary} — {version}"


def check_auth() -> str:
    path = CODEX_DIR / "auth.json"
    if not path.is_file():
        return MISSING
    try:
        with open(path, encoding="utf-8") as fh:
            mode = json.load(fh).get("auth_mode")
    except (OSError, json.JSONDecodeError):
        return "present, auth_mode unreadable"
    return f"present, auth_mode={mode!r}" if mode else "present, auth_mode not set"


def check_codex_model() -> str:
    path = CODEX_DIR / "config.toml"
    if not path.is_file():
        return MISSING
    if tomllib is not None:
        try:
            with open(path, "rb") as fh:
                model = tomllib.load(fh).get("model")
            if model:
                return str(model)
        except Exception:  # OSError or tomllib.TOMLDecodeError
            pass
    try:  # crude fallback
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("model") and "=" in stripped:
                return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return "present, model not set"


def check_blackhole() -> str:
    code, out = run(["/usr/sbin/system_profiler", "SPAudioDataType"], timeout=60)
    if code != 0 or not out:
        return "unknown (system_profiler failed)"
    lowered = out.lower()
    if "blackhole 2ch" in lowered:
        return "BlackHole 2ch present"
    if "blackhole" in lowered:
        return "a BlackHole device is present, but not named 'BlackHole 2ch'"
    return MISSING


def check_touchdesigner() -> str:
    hits = sorted(Path("/Applications").glob("TouchDesigner*.app"))
    return str(hits[0]) if hits else MISSING


def check_projectm() -> str:
    for prefix in ("/opt/homebrew/opt/projectm", "/usr/local/opt/projectm"):
        if Path(prefix).exists():
            return f"brew: {prefix}"
    for exe in ("projectMSDL", "projectM-SDL", "frontend-sdl2"):
        found = shutil.which(exe)
        if found:
            return f"cli: {found}"
    hits = sorted(Path("/Applications").glob("projectM*.app"))
    if hits:
        return str(hits[0])
    return f"{MISSING} (optional, Phase 5)"


def check_obs() -> str:
    hits = sorted(Path("/Applications").glob("OBS*.app")) + sorted(
        Path("/Applications").glob("obs*.app")
    )
    return str(hits[0]) if hits else f"{MISSING} (optional, Phase 5)"


CHECKS: list[tuple[str, object]] = [
    ("python", check_python),
    ("uv", check_uv),
    ("codex binary", check_codex),
    ("~/.codex/auth.json", check_auth),
    ("~/.codex config model", check_codex_model),
    ("BlackHole 2ch", check_blackhole),
    ("TouchDesigner.app", check_touchdesigner),
    ("projectM", check_projectm),
    ("OBS", check_obs),
]


def main() -> int:
    rows: list[tuple[str, str]] = []
    for label, fn in CHECKS:
        try:
            value = fn()  # type: ignore[operator]
        except Exception as exc:  # never fail the report
            value = f"check errored: {exc}"
        rows.append((label, value))

    width = max(len(label) for label, _ in rows)
    print("AMV Phase 0 environment")
    print("-" * (width + 3 + 40))
    for label, value in rows:
        print(f"{label.ljust(width)} | {value}")
    print("-" * (width + 3 + 40))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
