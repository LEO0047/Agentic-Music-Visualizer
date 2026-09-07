#!/usr/bin/env python3
"""Phase 5 environment report — is anything the projectM sidechain needs here?

    uv run python tools/projectm_check.py

Read-only, stdlib only, and **always exits 0**: it is a dashboard, not a gate.
SPEC Phase 0 lists projectM as optional, and `td/build_network.py` builds a
black Constant TOP fallback precisely so a machine with none of this still runs
the show — so a screen full of `missing` is a valid state, not a failure.

One row per thing `docs/phase5-projectm.md` asks you to install, plus the
install hint for the ones that are not here. Nothing is downloaded, installed
or launched: the two capture paths (Syphon and OBS+NDI) both need a human at a
GUI anyway.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

MISSING = "missing"

BREW_PREFIXES = ("/opt/homebrew", "/usr/local")

NDI_TOOLS_URL = "https://ndi.video/tools/"
SYPHON_APPS_URL = "https://syphon.github.io/"


def run(argv: list[str], timeout: float = 20.0) -> tuple[int, str]:
    """Run *argv* with no stdin; never raise, never hang the report."""
    try:
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, (proc.stdout or "")


def brew() -> str | None:
    found = shutil.which("brew")
    if found:
        return found
    for prefix in BREW_PREFIXES:
        candidate = Path(prefix) / "bin" / "brew"
        if candidate.is_file():
            return str(candidate)
    return None


def apps_matching(pattern: str) -> list[Path]:
    """/Applications entries whose name matches *pattern*, case-insensitively."""
    root = Path("/Applications")
    if not root.is_dir():
        return []
    needle = pattern.lower()
    try:
        return sorted(p for p in root.iterdir() if needle in p.name.lower())
    except OSError:
        return []


# -- individual checks ------------------------------------------------------


def check_projectm() -> str:
    """The library/app itself: brew formula first, then a bundled .app."""
    binary = brew()
    if binary is not None:
        code, out = run([binary, "list", "--formula", "projectm"])
        if code == 0 and out.strip():
            return "brew formula projectm installed"
    for prefix in BREW_PREFIXES:
        cellar = Path(prefix) / "opt" / "projectm"
        if cellar.exists():
            return f"brew: {cellar}"
    hits = apps_matching("projectm")
    if hits:
        return str(hits[0])
    return MISSING


def check_projectm_sdl() -> str:
    """The standalone player. This is the thing you actually run for Phase 5."""
    for exe in ("projectMSDL", "projectM-SDL", "frontend-sdl2", "projectMSDL-latest"):
        found = shutil.which(exe)
        if found:
            return f"{exe} → {found}"
    for prefix in BREW_PREFIXES:
        for exe in ("projectMSDL", "frontend-sdl2"):
            candidate = Path(prefix) / "bin" / exe
            if candidate.is_file():
                return str(candidate)
    return f"{MISSING} (not on PATH)"


def check_obs() -> str:
    hits = apps_matching("obs")
    return str(hits[0]) if hits else MISSING


def check_syphon() -> str:
    """Syphoner specifically, or any other Syphon server app."""
    exact = apps_matching("syphoner")
    if exact:
        return str(exact[0])
    others = apps_matching("syphon")
    if others:
        return "no Syphoner, but: " + ", ".join(p.name for p in others)
    return MISSING


def check_ndi() -> str:
    sdk = Path("/Library/NDI SDK for Apple")
    if sdk.exists():
        return str(sdk)
    for folder in ("/usr/local/lib", "/opt/homebrew/lib", "/Library/NDI/lib/macOS"):
        try:
            hits = sorted(Path(folder).glob("libndi*.dylib"))
        except OSError:
            hits = []
        if hits:
            return str(hits[0])
    hits = apps_matching("ndi")
    if hits:
        return "NDI Tools app present, runtime not found: " + hits[0].name
    return MISSING


def check_blackhole() -> str:
    """Phase 1 already needs this; projectM reads the same virtual device."""
    driver = Path("/Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver")
    if driver.exists():
        return f"{driver} present"
    code, out = run(["/usr/sbin/system_profiler", "SPAudioDataType"], timeout=60)
    if code == 0 and "blackhole" in out.lower():
        return "a BlackHole device is present (not the 2ch driver bundle)"
    return MISSING


#: ``(label, check, hint shown when the value starts with "missing")``
CHECKS: list[tuple[str, object, str]] = [
    (
        "projectM",
        check_projectm,
        "brew install projectm",
    ),
    (
        "projectMSDL on PATH",
        check_projectm_sdl,
        "comes with `brew install projectm`; otherwise build projectM-SDL "
        "(frontend-sdl2) from https://github.com/projectM-visualizer/frontend-sdl2",
    ),
    (
        "OBS.app",
        check_obs,
        "brew install --cask obs   (then add the NDI output plugin)",
    ),
    (
        "Syphoner.app / any Syphon app",
        check_syphon,
        f"Syphoner or another Syphon server app — see {SYPHON_APPS_URL}",
    ),
    (
        "NDI runtime",
        check_ndi,
        f"install NDI Tools (includes the runtime): {NDI_TOOLS_URL}",
    ),
    (
        "BlackHole 2ch",
        check_blackhole,
        "brew install blackhole-2ch",
    ),
]


def main() -> int:
    rows: list[tuple[str, str, str]] = []
    for label, fn, hint in CHECKS:
        try:
            value = fn()  # type: ignore[operator]
        except Exception as exc:  # a broken check must not break the report
            value = f"check errored: {exc}"
        rows.append((label, value, hint))

    width = max(len(label) for label, _, _ in rows)
    print("AMV Phase 5 · projectM sidechain")
    print("-" * (width + 3 + 46))
    for label, value, _ in rows:
        print(f"{label.ljust(width)} | {value}")
    print("-" * (width + 3 + 46))

    absent = [(label, hint) for label, value, hint in rows if value.startswith(MISSING)]
    if absent:
        print("\nHow to get what is missing:")
        for label, hint in absent:
            print(f"  {label}: {hint}")
        print(
            "\nNone of this is required to build the network. With nothing installed,"
            "\nleave the director's Projectmsource par on `none`: projectm_in falls back"
            "\nto a black Constant TOP and the composite stays well defined."
        )
    else:
        print("\nEverything the projectM sidechain needs is present.")
    print("\nSetup and acceptance: docs/phase5-projectm.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
