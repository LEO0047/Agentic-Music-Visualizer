"""No macOS, Swift, TD, Codex or dependency installation is used by these tests."""
from __future__ import annotations

import ast
import io
import json
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from amv import launcher
from amv.launcher import Options


class Runner:
    def __init__(self, *, active=False, fail=None, after_enable=None, sidecar_code=0):
        self.calls = []
        self.active = active
        self.fail = fail or {}
        self.after_enable = after_enable
        self.sidecar_code = sidecar_code

    def __call__(self, command, **kwargs):
        if command[0] == 'swift':
            name = command[-1]
        elif command[0] == 'uv':
            name = 'install'
        elif command[0] == '/usr/bin/open':
            name = 'open'
        elif '-c' in command:
            name = 'check'
        else:
            name = 'sidecar'
        self.calls.append((name, command, kwargs))
        if name in self.fail:
            error = self.fail.pop(name)
            if error:
                raise error
        if name == 'enable':
            self.active = True
            if self.after_enable:
                raise self.after_enable
        if name == 'restore':
            self.active = False
        uid = launcher.ROUTE_UID if self.active else 'speakers'
        return subprocess.CompletedProcess(
            command, self.sidecar_code if name == 'sidecar' else 0,
            stdout=f'Default output: Device\nUID: {uid}\n', stderr='',
        )

    @property
    def names(self):
        return [name for name, _, _ in self.calls]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / 'repo with spaces'
    root.mkdir()
    (root / 'td').mkdir()
    (root / 'Agentic-Music-Visualizer.toe').touch()
    (root / '.venv/bin').mkdir(parents=True)
    (root / '.venv/bin/python').touch()
    (root / 'tools').mkdir()
    (root / 'tools/audio_route.swift').touch()
    td_app = tmp_path / 'TouchDesigner.app'
    td_app.mkdir()
    monkeypatch.setattr(launcher, 'TD_APP', td_app)
    return root


def execute(repo, options=Options(), runner=None, **kwargs):
    runner = runner or Runner()
    output = io.StringIO()
    kwargs.setdefault('platform', 'darwin')
    kwargs.setdefault('which', lambda name: f'/test/bin/{name}')
    code = launcher.execute(options, root=repo, runner=runner, out=output, **kwargs)
    return code, runner, output.getvalue()


def saved_route(repo, **values):
    path = repo / 'artifacts/audio-route.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({
        'originalOutputUID': 'speakers', 'originalOutputName': 'Studio speakers', **values,
    }))
    return path


def test_defaults_do_not_install_route_or_use_an_account(repo):
    code, runner, text = execute(repo)
    assert code == 0
    assert runner.names == ['check', 'open', 'sidecar']
    sidecar = runner.calls[-1][1]
    assert sidecar[sidecar.index('--director') + 1] == 'rule'
    assert 'codex' not in ' '.join(sidecar).lower()
    assert 'No Codex/account usage' in text
    assert not (repo / 'artifacts').exists()
    assert all('shell' not in kwargs for _, _, kwargs in runner.calls)
    assert all(kwargs['cwd'] == repo for _, _, kwargs in runner.calls)
    assert 'stdin' not in runner.calls[-1][2]  # hotkeys still work
    assert runner.calls[1][1][-1] == str(repo / 'Agentic-Music-Visualizer.toe')
    assert not any('fullscreen' in arg for _, cmd, _ in runner.calls for arg in cmd)


def test_defaults_do_not_require_uv_or_swift(repo):
    assert execute(repo, which=lambda _: None)[0] == 0


def test_only_explicit_ai_opt_in_selects_gpt(repo):
    code, runner, text = execute(repo, Options(enable_ai=True))
    assert code == 0
    assert runner.names == ['check', 'open', 'sidecar']
    assert runner.calls[-1][1][-3] == 'gpt'
    assert 'subscription quota' in text


def test_only_explicit_install_runs_locked_uv_sync(repo):
    code, runner, _ = execute(repo, Options(install_deps=True))
    assert code == 0
    assert runner.names == ['install', 'check', 'open', 'sidecar']
    assert runner.calls[0][1] == ['uv', 'sync', '--locked', '--extra', 'audio']


@pytest.mark.parametrize('options', [Options(), Options(install_deps=True, route_audio=True, enable_ai=True), Options(restore_audio=True, yes=True)])
def test_non_mac_rejected_before_any_command_or_mutation(repo, options):
    code, runner, text = execute(repo, options, platform='linux')
    assert code == 1
    assert runner.calls == []
    assert 'needs macOS' in text
    assert not (repo / 'artifacts').exists()


@pytest.mark.parametrize('options', [Options(dry_run=True), Options(dry_run=True, install_deps=True, route_audio=True, enable_ai=True), Options(dry_run=True, restore_audio=True)])
def test_dry_run_needs_no_platform_tools_files_or_input(tmp_path, options):
    missing = tmp_path / 'does not exist'
    code, runner, text = execute(missing, options, platform='linux', which=lambda _: None,
                                 input_fn=lambda _: pytest.fail('must not prompt'))
    assert code == 0
    assert runner.calls == []
    assert not missing.exists()
    assert 'Preview only' in text


@pytest.mark.parametrize('missing', ['.venv/bin/python', 'Agentic-Music-Visualizer.toe', 'td'])
def test_missing_prerequisites_do_not_open_or_modify_system(repo, missing):
    path = repo / missing
    path.rmdir() if path.is_dir() else path.unlink()
    code, runner, _ = execute(repo)
    assert code == 1
    assert runner.calls == []


def test_missing_td_blocks_even_opted_in_install(repo):
    launcher.TD_APP.rmdir()
    code, runner, _ = execute(repo, Options(install_deps=True))
    assert code == 1
    assert runner.calls == []


def test_missing_uv_blocks_requested_install(repo):
    code, runner, text = execute(repo, Options(install_deps=True), which=lambda _: None)
    assert code == 1
    assert runner.calls == []
    assert 'Install uv first' in text


def test_invalid_dependencies_fail_before_audio_and_td(repo):
    runner = Runner(fail={'check': subprocess.CalledProcessError(1, ['python'])})
    code, runner, text = execute(repo, Options(route_audio=True), runner)
    assert code == 1
    assert runner.names == ['check']
    assert '--install-deps' in text


def test_failed_install_does_not_open_td(repo):
    runner = Runner(fail={'install': subprocess.CalledProcessError(1, ['uv'])})
    code, runner, _ = execute(repo, Options(install_deps=True), runner)
    assert code == 1
    assert runner.names == ['install']


def test_opted_in_route_is_restored_on_normal_exit(repo):
    code, runner, _ = execute(repo, Options(route_audio=True))
    assert code == 0
    assert runner.names == ['check', 'status', 'enable', 'status', 'open', 'sidecar', 'status', 'restore']
    assert not runner.active


def test_preexisting_route_is_not_owned_or_restored(repo):
    code, runner, text = execute(repo, Options(route_audio=True), Runner(active=True))
    assert code == 0
    assert runner.names == ['check', 'status', 'open', 'sidecar']
    assert runner.active
    assert 'leave it unchanged' in text


@pytest.mark.parametrize('name', ['open', 'sidecar'])
@pytest.mark.parametrize('error,expected_code', [
    (subprocess.CalledProcessError(2, ['command']), 1),
    (OSError('failed to launch'), 1),
    (KeyboardInterrupt(), 130),
    (launcher.Interrupted(signal.SIGTERM), 143),
])
def test_launch_failure_or_interruption_restores_owned_route(repo, name, error, expected_code):
    runner = Runner(fail={name: error})
    code, runner, _ = execute(repo, Options(route_audio=True), runner)
    assert code == expected_code
    assert runner.names[-2:] == ['status', 'restore']
    assert not runner.active


def test_partial_enable_failure_is_restored(repo):
    error = subprocess.CalledProcessError(1, ['swift'])
    code, runner, _ = execute(repo, Options(route_audio=True), Runner(after_enable=error))
    assert code == 1
    assert runner.names == ['check', 'status', 'enable', 'status', 'restore']
    assert not runner.active


def test_enable_failure_before_mutation_does_not_apply_stale_restore(repo):
    error = subprocess.CalledProcessError(1, ['swift'])
    saved_route(repo)
    code, runner, _ = execute(repo, Options(route_audio=True), Runner(fail={'enable': error}))
    assert code == 1
    assert runner.names == ['check', 'status', 'enable', 'status']
    assert not runner.active


def test_user_changed_output_is_left_alone(repo):
    runner = Runner()
    def switch_on_sidecar(command, **kwargs):
        result = runner(command, **kwargs)
        if 'amv.sidecar' in command:
            runner.active = False
        return result
    code, _, text = execute(repo, Options(route_audio=True), switch_on_sidecar)
    assert code == 0
    assert runner.names[-1] == 'status'
    assert 'restore' not in runner.names
    assert 'current selection unchanged' in text


def test_failed_restore_is_not_silenced(repo):
    error = subprocess.CalledProcessError(1, ['swift'])
    code, runner, text = execute(repo, Options(route_audio=True), Runner(fail={'restore': error}))
    assert code == 1
    assert runner.active
    assert 'Restore AirPods.command' in text
    assert 'could not be verified' in text


def test_unreadable_status_blocks_audio_changes(repo):
    runner = Runner()
    def invalid_status(command, **kwargs):
        result = runner(command, **kwargs)
        result.stdout = 'unexpected output'
        return result
    code, _, _ = execute(repo, Options(route_audio=True), invalid_status)
    assert code == 1
    assert runner.names == ['check', 'status']


@pytest.mark.parametrize('sidecar_code,expected', [(7, 7), (-15, 143)])
def test_sidecar_exit_code_is_propagated(repo, sidecar_code, expected):
    code, _, _ = execute(repo, runner=Runner(sidecar_code=sidecar_code))
    assert code == expected


@pytest.mark.parametrize('answer', ['', 'n', 'no', 'anything else'])
def test_restore_confirmation_defaults_to_no(repo, answer):
    saved_route(repo)
    prompts = []
    def confirm(prompt):
        prompts.append(prompt)
        return answer
    code, runner, text = execute(repo, Options(restore_audio=True), input_fn=confirm, interactive=True)
    assert code == 0
    assert runner.calls == []
    assert 'Studio speakers' in prompts[0]
    assert 'unchanged' in text


@pytest.mark.parametrize('yes', [False, True])
def test_restore_runs_only_after_confirmation(repo, yes):
    saved_route(repo)
    code, runner, _ = execute(repo, Options(restore_audio=True, yes=yes),
                              input_fn=lambda _: 'yes', interactive=not yes)
    assert code == 0
    assert runner.names == ['restore']


def test_restore_requires_explicit_noninteractive_confirmation(repo):
    saved_route(repo)
    code, runner, text = execute(repo, Options(restore_audio=True), interactive=False)
    assert code == 1
    assert runner.calls == []
    assert '--yes' in text


def test_restore_with_missing_state_has_no_effect(repo):
    code, runner, text = execute(repo, Options(restore_audio=True, yes=True))
    assert code == 1
    assert runner.calls == []
    assert 'Sound settings' in text


@pytest.mark.parametrize('raw', ['not json', '[]', '{"originalOutputUID":123}'])
def test_restore_rejects_invalid_state(repo, raw):
    saved_route(repo).write_text(raw)
    code, runner, _ = execute(repo, Options(restore_audio=True, yes=True))
    assert code == 1
    assert runner.calls == []


@pytest.mark.parametrize('flags', [['--yes'], ['--restore-audio', '--enable-ai'], ['--restore-audio', '--install-deps'], ['--restore-audio', '--route-audio']])
def test_cli_rejects_ambiguous_options(flags):
    with pytest.raises(SystemExit) as exc:
        launcher.main(flags)
    assert exc.value.code == 2


def test_cli_maps_opt_ins_without_enabling_others(monkeypatch):
    seen = []
    monkeypatch.setattr(launcher, 'execute', lambda options: seen.append(options) or 0)
    assert launcher.main([]) == 0
    assert seen == [Options()]
    assert launcher.main(['--route-audio', '--dry-run']) == 0
    assert seen[-1] == Options(route_audio=True, dry_run=True)


def test_signal_handlers_restore_previous_handlers():
    before = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGHUP)}
    with launcher._signal_handlers():
        with pytest.raises(launcher.Interrupted) as exc:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert exc.value.signum == signal.SIGTERM
    assert {number: signal.getsignal(number) for number in before} == before


def test_bootstrap_module_uses_only_stdlib_and_parses_on_python39():
    source = Path(launcher.__file__).read_text()
    tree = ast.parse(source, feature_version=(3, 9))
    imports = {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imports |= {name.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for name in node.names}
    assert imports <= sys.stdlib_module_names


def test_shell_entrypoints_are_portable_and_dry_runs_are_safe():
    root = Path(launcher.__file__).resolve().parents[1]
    for name in ('Start Visualizer.command', 'Restore AirPods.command'):
        path = root / name
        subprocess.run(['sh', '-n', str(path)], check=True)
        result = subprocess.run(['sh', str(path), '--dry-run'], cwd='/',
                                capture_output=True, text=True, check=True)
        assert 'Preview only' in result.stdout
        assert 'Traceback' not in result.stderr


def test_restore_rejects_mixed_type_swift_state(repo):
    saved_route(repo, originalSystemOutputUID=123)
    code, runner, _ = execute(repo, Options(restore_audio=True, yes=True))
    assert code == 1
    assert runner.calls == []


def test_restore_eof_leaves_output_unchanged(repo):
    saved_route(repo)
    def eof(_):
        raise EOFError
    code, runner, text = execute(repo, Options(restore_audio=True), input_fn=eof, interactive=True)
    assert code == 0
    assert runner.calls == []
    assert 'unchanged' in text


class FakeProcess:
    pid = 54321
    def __init__(self, waits):
        self.waits = list(waits)
        self.calls = []

    def wait(self, timeout=None):
        self.calls.append(timeout)
        value = self.waits.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.mark.parametrize('interruption', [KeyboardInterrupt(), launcher.Interrupted(signal.SIGTERM), launcher.Interrupted(signal.SIGHUP)])
def test_process_supervisor_interrupts_entire_group_then_reaps(interruption):
    process = FakeProcess([interruption, 0, 0])
    signals = []
    def popen(command, **kwargs):
        assert command == ['sidecar']
        assert kwargs == {'cwd': '/repo', 'start_new_session': True}
        return process
    with pytest.raises(type(interruption)):
        launcher._run_sidecar(['sidecar'], cwd='/repo', popen=popen,
                              signal_group=lambda *args: signals.append(args))
    assert signals == [(process.pid, signal.SIGINT), (process.pid, signal.SIGKILL)]
    assert process.calls == [None, 7.0, None]


def test_stubborn_process_group_is_killed_after_bounded_grace():
    process = FakeProcess([KeyboardInterrupt(), subprocess.TimeoutExpired('sidecar', 7), -9])
    signals = []
    with pytest.raises(KeyboardInterrupt):
        launcher._run_sidecar(['sidecar'], cwd='/repo', popen=lambda *a, **k: process,
                              signal_group=lambda *args: signals.append(args))
    assert signals[-1] == (process.pid, signal.SIGKILL)
    assert process.calls == [None, 7.0, None]


def test_normal_exit_cleans_any_leftover_descendants():
    process = FakeProcess([0, 0])
    signals = []
    result = launcher._run_sidecar(['sidecar'], cwd='/repo', popen=lambda *a, **k: process,
                                  signal_group=lambda *args: signals.append(args))
    assert result.returncode == 0
    assert signals == [(process.pid, signal.SIGKILL)]


def test_already_exited_process_group_is_harmless():
    process = FakeProcess([0, 0])
    def gone(*args):
        raise ProcessLookupError
    assert launcher._run_sidecar(['sidecar'], cwd='/repo', popen=lambda *a, **k: process,
                                 signal_group=gone).returncode == 0


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux /proc verifies no live descendants')
@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGHUP])
def test_real_supervisor_signal_stops_synthetic_child_and_grandchild(tmp_path, signum):
    """Only local Python sleepers: no real visualizer, Swift or account calls."""
    import os
    import time

    pids = tmp_path / 'pids.json'
    child_stopped = tmp_path / 'child-stopped'
    child_code = '''
import json, os, signal, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(60)'])
def stop(*args):
    Path(sys.argv[2]).write_text('graceful')
    raise SystemExit(0)
signal.signal(signal.SIGINT, stop)
Path(sys.argv[1]).write_text(json.dumps([os.getpid(), child.pid]))
while True: time.sleep(.01)
'''
    supervisor_code = '''
import sys
from amv.launcher import _run_sidecar, _signal_handlers, Interrupted
try:
    with _signal_handlers():
        _run_sidecar([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]], cwd='.', grace_s=.5)
except Interrupted as exc:
    raise SystemExit(128 + exc.signum)
'''
    root = Path(launcher.__file__).resolve().parents[1]
    supervisor = subprocess.Popen([sys.executable, '-c', supervisor_code, child_code,
                                   str(pids), str(child_stopped)], cwd=root,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child_pid = None
    try:
        until = time.monotonic() + 5
        while not pids.exists() and time.monotonic() < until:
            time.sleep(.01)
        assert pids.exists(), 'synthetic sidecar did not start'
        child_pid, grandchild_pid = json.loads(pids.read_text())
        supervisor.send_signal(signum)
        assert supervisor.wait(timeout=5) == 128 + signum
        assert child_stopped.read_text() == 'graceful'
        until = time.monotonic() + 2
        while time.monotonic() < until:
            stat = Path(f'/proc/{grandchild_pid}/stat')
            if not stat.exists() or stat.read_text().split()[2] == 'Z':
                break
            time.sleep(.01)
        else:
            pytest.fail('grandchild remained alive after supervisor shutdown')
        assert not Path(f'/proc/{child_pid}').exists()
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait()
        if child_pid is not None:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX terminal behavior')
def test_private_group_keeps_hotkey_input_and_restores_terminal(tmp_path, monkeypatch):
    import os
    import pty
    import termios
    import threading
    import time

    master, slave = pty.openpty()
    stream = os.fdopen(os.dup(slave), 'r')
    before = termios.tcgetattr(slave)
    ready = tmp_path / 'ready'
    received = tmp_path / 'received'
    command = [sys.executable, '-c', '''
import sys, tty
from pathlib import Path
tty.setcbreak(0)
Path(sys.argv[1]).write_text('ready')
Path(sys.argv[2]).write_text(sys.stdin.read(1))
''', str(ready), str(received)]
    processes = []
    results = []
    def popen(args, **kwargs):
        process = subprocess.Popen(args, stdin=slave, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(sys, 'stdin', stream)
    thread = threading.Thread(target=lambda: results.append(
        launcher._run_sidecar(command, cwd=tmp_path, popen=popen)), daemon=True)
    try:
        thread.start()
        until = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < until:
            time.sleep(.01)
        assert ready.exists()
        os.write(master, b'q')
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert results[0].returncode == 0
        assert received.read_text() == 'q'
        assert termios.tcgetattr(slave) == before
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        thread.join(timeout=2)
        stream.close()
        os.close(master)
        os.close(slave)
