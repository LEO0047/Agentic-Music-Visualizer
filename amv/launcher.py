"""Safe, stdlib-only Mac launcher; orchestration is testable without macOS.

The bootstrap also runs under macOS's Python 3.9. The actual sidecar always
uses the project's Python >= 3.11 virtual environment. No dependency manager,
audio routing helper or account-backed director runs without an explicit flag.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
from typing import Sequence, TextIO


ROOT = Path(__file__).resolve().parents[1]
TD_APP = Path("/Applications/TouchDesigner.app")
ROUTE_UID = "com.agentic-music-visualizer.monitor"
DEPENDENCY_CHECK = (
    "import sys; "
    "assert sys.version_info >= (3, 11), 'Python >= 3.11 is required'; "
    "import numpy, pythonosc, jsonschema"
)


@dataclass(frozen=True)
class Options:
    install_deps: bool = False
    route_audio: bool = False
    enable_ai: bool = False
    dry_run: bool = False
    restore_audio: bool = False
    yes: bool = False


class LaunchError(Exception):
    """An actionable setup or cleanup failure."""


class Interrupted(Exception):
    def __init__(self, signum: int):
        self.signum = signum


@contextmanager
def _signal_handlers():
    """Let the orchestration's finally block run on terminal close or TERM."""
    def stop(signum, _frame):
        raise Interrupted(signum)

    previous = {}
    try:
        for name in ("SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is not None:
                previous[signum] = signal.signal(signum, stop)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _run_sidecar(command, *, cwd, check=False, popen=None, signal_group=None, grace_s=7.0):
    """Supervise a private process group, including account-backed descendants.

    Keep inherited terminal input for hotkeys, but forward interrupts ourselves.
    A normal child exit also reaps leftover group members. This cannot recover
    from SIGKILL of the launcher or a child deliberately creating its own session.
    """
    popen = subprocess.Popen if popen is None else popen
    signal_group = os.killpg if signal_group is None else signal_group
    terminal = None
    try:
        import termios
        if sys.stdin.isatty():
            terminal = (sys.stdin.fileno(), termios.tcgetattr(sys.stdin.fileno()))
    except (ImportError, OSError, ValueError):
        pass
    process = popen(command, cwd=cwd, start_new_session=True)

    def send(signum):
        try:
            signal_group(process.pid, signum)
        except ProcessLookupError:
            pass

    try:
        try:
            returncode = process.wait()
        except BaseException:
            send(signal.SIGINT)
            try:
                process.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                pass
            raise
    finally:
        try:
            # The sidecar may have exited while its Codex worker was still busy.
            send(signal.SIGKILL)
            process.wait()
        finally:
            if terminal is not None:
                try:
                    termios.tcsetattr(terminal[0], termios.TCSADRAIN, terminal[1])
                except (OSError, ValueError):
                    pass
    result = subprocess.CompletedProcess(command, returncode)
    if check:
        result.check_returncode()
    return result


def _commands(root: Path, options: Options) -> dict[str, list[str]]:
    return {
        "install": ["uv", "sync", "--locked", "--extra", "audio"],
        "check": [str(root / ".venv/bin/python"), "-c", DEPENDENCY_CHECK],
        "status": ["swift", "tools/audio_route.swift", "status"],
        "enable": ["swift", "tools/audio_route.swift", "enable"],
        "restore": ["swift", "tools/audio_route.swift", "restore"],
        "open": ["/usr/bin/open", "-a", str(TD_APP), str(root / "Agentic-Music-Visualizer.toe")],
        "sidecar": [
            str(root / ".venv/bin/python"), "-m", "amv.sidecar", "--director",
            "gpt" if options.enable_ai else "rule",
            "--decisions-log", "artifacts/director-decisions.jsonl",
        ],
    }


def execute(
    options: Options, *, root: Path = ROOT, runner=None, platform=None,
    which=None, out: TextIO | None = None, input_fn=None, interactive=None,
) -> int:
    """Run the launch plan. Inject subprocesses/platform/paths for cloud tests."""
    custom_runner = runner
    runner = subprocess.run if runner is None else runner
    platform = sys.platform if platform is None else platform
    which = shutil.which if which is None else which
    out = sys.stdout if out is None else out
    input_fn = input if input_fn is None else input_fn
    interactive = sys.stdin.isatty() if interactive is None else interactive
    commands = _commands(root, options)

    def say(message):
        print(message, file=out, flush=True)

    def run(name, *, capture=False, check=True, timeout=60):
        kwargs = {"cwd": root, "check": check}
        if name != "sidecar":
            kwargs.update(stdin=subprocess.DEVNULL, timeout=timeout)
        if capture:
            kwargs.update(capture_output=True, text=True)
        if name == "sidecar" and custom_runner is None:
            return _run_sidecar(commands[name], **kwargs)
        return runner(commands[name], **kwargs)

    def route_active():
        result = run("status", capture=True)
        uid = next((line[5:].strip() for line in result.stdout.splitlines()
                    if line.startswith("UID: ")), None)
        if uid is None:
            raise LaunchError("Could not verify the current audio output; audio was not changed.")
        return uid == ROUTE_UID

    if options.dry_run:
        say("Preview only: no commands will run, files be created, or audio/accounts be used.")
        names = ["restore"] if options.restore_audio else (
            (["install"] if options.install_deps else []) + ["check"]
            + (["status", "enable"] if options.route_audio else []) + ["open", "sidecar"]
        )
        for name in names:
            say(shlex.join(commands[name]))
        if options.route_audio:
            say("On exit: restore only a route enabled here that is still the active output.")
        return 0

    route_attempted = False
    code = 0
    try:
        if platform != "darwin":
            raise LaunchError("This launcher needs macOS. Use --dry-run to preview safely elsewhere.")
        if options.route_audio or options.restore_audio:
            if not which("swift"):
                raise LaunchError("Swift is required for the optional audio routing helper.")
            if not (root / "tools/audio_route.swift").is_file():
                raise LaunchError("Missing tools/audio_route.swift; keep the repository files together.")

        if options.restore_audio:
            state_path = root / "artifacts/audio-route.json"
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise LaunchError("No readable saved audio route. Choose your output in macOS Sound settings.") from exc
            if (not isinstance(state, dict) or not state.get("originalOutputUID")
                    or not all(isinstance(value, str) for value in state.values())):
                raise LaunchError("Invalid saved audio route. Choose your output in macOS Sound settings.")
            target = state.get("originalOutputName", "the saved listening device")
            if not options.yes:
                if not interactive:
                    raise LaunchError("Audio restore needs confirmation; run interactively or pass --yes.")
                try:
                    answer = input_fn(f"Restore macOS audio output to {target}? [y/N] ")
                except EOFError:
                    answer = ""
                if answer.strip().lower() not in ("y", "yes"):
                    say("Audio output unchanged.")
                    return 0
            run("restore")
            return 0

        if not (root / "Agentic-Music-Visualizer.toe").is_file() or not (root / "td").is_dir():
            raise LaunchError("Keep Agentic-Music-Visualizer.toe and td/ together in the repository.")
        if not TD_APP.is_dir():
            raise LaunchError("Install TouchDesigner and activate its license before launching.")
        if options.install_deps:
            if not which("uv"):
                raise LaunchError("Install uv first: https://docs.astral.sh/uv/")
            say("Installing locked dependencies (--install-deps was explicitly selected).")
            run("install", timeout=None)
        if not (root / ".venv/bin/python").is_file():
            raise LaunchError("Missing .venv. Run Start Visualizer.command --install-deps to install dependencies.")
        try:
            run("check")
        except (subprocess.CalledProcessError, OSError) as exc:
            raise LaunchError("The .venv needs Python >= 3.11 and project dependencies. Use --install-deps to repair it.") from exc
        if options.enable_ai:
            say("AI enabled: Codex uses your existing login and subscription quota; rule fallback stays available.")
        else:
            say("Director: rule mode. No Codex/account usage; enable it explicitly with --enable-ai.")
        if options.route_audio:
            if route_active():
                say("An AMV audio route is already active; this launch will leave it unchanged on exit.")
            else:
                # Set BEFORE enable: even a partial helper failure may need cleanup.
                route_attempted = True
                run("enable")
                if not route_active():
                    raise LaunchError("The audio helper did not activate AMV output.")
        else:
            say("System audio unchanged. Configure BlackHole yourself or opt in with --route-audio.")
        run("open")
        say("TouchDesigner opens its saved, bordered window. Close it with its × button; Ctrl-C stops the director.")
        say("Apple Music: choose this Mac as output. If routing is enabled, use the player's own volume slider.")
        result = run("sidecar", check=False)
        code = result.returncode if result.returncode >= 0 else 128 - result.returncode
    except KeyboardInterrupt:
        code = 130
        say("Stopping the director.")
    except Interrupted as exc:
        code = 128 + exc.signum
        say("Stopping the director.")
    except (LaunchError, OSError, subprocess.SubprocessError) as exc:
        say(f"Launch failed: {exc}")
        detail = getattr(exc, "stderr", None)
        if detail:
            say(str(detail).strip())
        code = 1
    finally:
        if route_attempted:
            try:
                if route_active():
                    run("restore")
                else:
                    say("Audio output is no longer the AMV route; leaving your current selection unchanged.")
            except (LaunchError, OSError, subprocess.SubprocessError) as exc:
                say(f"Audio restore could not be verified: {exc}")
                say("Run Restore AirPods.command, or select your listening output in macOS Sound settings.")
                if code == 0:
                    code = 1
    return code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-deps", action="store_true", help="allow uv sync (downloads and changes .venv)")
    parser.add_argument("--route-audio", action="store_true", help="allow temporary system output routing to BlackHole + current device")
    parser.add_argument("--enable-ai", action="store_true", help="allow GPT/Codex calls using the existing login and subscription quota")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only; safe on non-Mac systems")
    parser.add_argument("--restore-audio", action="store_true", help="restore the previously saved macOS output instead of launching")
    parser.add_argument("--yes", action="store_true", help="confirm the requested audio restore without a prompt")
    args = parser.parse_args(argv)
    if args.yes and not args.restore_audio:
        parser.error("--yes applies only to --restore-audio")
    if args.restore_audio and (args.install_deps or args.route_audio or args.enable_ai):
        parser.error("--restore-audio cannot be combined with launch options")
    with _signal_handlers():
        return execute(Options(**vars(args)))


if __name__ == "__main__":
    raise SystemExit(main())
