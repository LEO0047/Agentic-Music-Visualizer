#!/usr/bin/env python3
"""SPEC §8「上場前檢查」as an executable (Phase 6).

    uv run python tools/preflight.py
    uv run python tools/preflight.py --json
    uv run python tools/preflight.py --real-codex        # spends Codex quota
    uv run python tools/preflight.py --record-dir /Volumes/SSD/amv

Every item prints one line — ``✅`` / ``⚠️`` / ``❌`` plus a reason short enough
to read from the FOH position — and the process exits 0 when nothing is ``❌``.
That split is the whole design: a warning is something you *decided* to run
without (no projectM, no music playing yet), a failure is something that will
end the set, and a checklist that cannot tell the two apart gets ignored by the
third gig.

Nothing here is a re-implementation. The environment rows come from
``tools/check_env.py``, the two audio rows drive ``tools/audio_check.py``'s own
``loopback`` / ``meter`` commands through their argparse layer, and the
fallback drill runs the real :class:`~amv.director.GPTDirector` against
``tools/fake_codex.py``. If one of those tools changes, this checklist changes
with it.

**Quota.** The only item that can spend ChatGPT Codex quota is the smoke test,
and it stays skipped unless ``--real-codex`` is passed or ``$AMV_CODEX_BIN``
points somewhere (which is how the fake gets exercised). ``codex --version``
and the fallback drill are free.

**Secrets.** ``~/.codex/auth.json`` is only ever checked for existence and its
``auth_mode`` key, by ``tools/check_env.py``. No token is read or printed.

What this file deliberately does *not* check: the TouchDesigner half of
SPEC §8 (waveform moving, TD playing a song on its own, hotkeys reaching the
network, ``on_drop`` firing on a real drop, Movie File Out actually recording).
Those need TD open and a human watching the screen — see
``docs/phase6-show-hardening.md``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS = _REPO_ROOT / "tools"
for _p in (str(_REPO_ROOT), str(_TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import check_env  # noqa: E402  (needs the sys.path shim above)

__all__ = [
    "OK",
    "WARN",
    "FAIL",
    "Check",
    "CHECK_ORDER",
    "ENV_REQUIRED",
    "FAKE_CODEX",
    "FALLBACK_DEADLINE_S",
    "METER_SECONDS",
    "MIN_RECORD_GB",
    "SESSION_WARN_COUNT",
    "check_environment",
    "check_loopback",
    "check_routing",
    "check_codex_binary",
    "check_codex_smoke",
    "check_fallback_drill",
    "check_record_space",
    "check_sessions",
    "run_checks",
    "format_report",
    "main",
]

OK = "ok"
WARN = "warn"
FAIL = "fail"

MARK: dict[str, str] = {OK: "✅", WARN: "⚠️", FAIL: "❌"}

#: ``tools/check_env.py`` rows that must not say "missing". The rest (uv, the
#: default model, TouchDesigner, projectM, OBS) are reported as warnings: this
#: checklist covers the sidecar half of the show, and TD's own readiness is a
#: thing a human confirms on the TD machine.
ENV_REQUIRED: frozenset[str] = frozenset(
    {"python", "codex binary", "~/.codex/auth.json"}
)

#: How long the routing meter listens for signal from the Multi-Output device.
METER_SECONDS = 3.0

#: SPEC §7: GPT failure must reach the rule director without the screen
#: noticing. Two seconds is generous — the drill normally lands in ~0.5 s.
FALLBACK_DEADLINE_S = 2.0

#: SPEC §8「Movie File Out 錄影」. 5 GB is roughly ten minutes of 1280×1280
#: HAP at the Non-Commercial output cap, i.e. enough to notice you are short.
MIN_RECORD_GB = 5.0

#: SPEC §7「``~/.codex/sessions`` 堆滿」. Past this, run tools/codex_sessions.py.
SESSION_WARN_COUNT = 500

FAKE_CODEX = _TOOLS / "fake_codex.py"

SESSIONS_DIR = Path.home() / ".codex" / "sessions"

#: Feature summary handed to the fallback drill and the smoke test. Shaped like
#: :meth:`amv.features.FeatureBuffer.summary`, values from the middle of a set.
DRILL_SUMMARY: dict[str, Any] = {
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


@dataclass
class Check:
    """One checklist row: a mark, a one-line reason, and the numbers behind it."""

    name: str
    status: str
    reason: str
    detail: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def mark(self) -> str:
        return MARK.get(self.status, "?")

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "status": self.status, "reason": self.reason}
        if self.detail:
            out["detail"] = list(self.detail)
        if self.data:
            out["data"] = self.data
        return out


# -- shared helpers ---------------------------------------------------------


@contextlib.contextmanager
def _env(**overrides: str | None) -> Iterator[None]:
    """Temporarily set (or, with ``None``, unset) environment variables."""
    saved = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _audio_check() -> Any:
    """Import ``tools/audio_check.py`` lazily (it pulls in numpy)."""
    import audio_check

    return audio_check


def _run_audio_command(argv: Sequence[str]) -> tuple[int, dict[str, Any]]:
    """Run one ``tools/audio_check.py`` sub-command and return its JSON payload.

    The command is driven through its own parser rather than by shelling out,
    so a flag that stops existing there stops working here too, loudly. Its
    live meter lines go to stderr and are swallowed; only the summary is kept.
    """
    audio_check = _audio_check()
    args = audio_check.build_parser().parse_args(list(argv))
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = int(args.func(args))
    raw = stdout.getvalue().strip()
    payload = json.loads(raw) if raw else {}
    return code, payload


def _gb_free(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024.0**3)


# -- 1. environment ---------------------------------------------------------


def check_environment() -> Check:
    """SPEC Phase 0 — reuse ``tools/check_env.py``'s rows, then judge them."""
    rows: dict[str, str] = {}
    for label, fn in check_env.CHECKS:
        try:
            rows[label] = str(fn())
        except Exception as exc:  # a dashboard row must never abort the checklist
            rows[label] = f"check errored: {exc}"

    def bad(value: str) -> bool:
        return value.startswith(check_env.MISSING) or "TOO OLD" in value or "errored" in value

    # ENV_REQUIRED filters what check_env actually reported rather than
    # asserting it: a row that disappears from check_env.py is a change to that
    # file, not a broken machine, and inventing a failure for it would make
    # this checklist lie the first time the dashboard is edited.
    failed = [label for label, value in rows.items() if label in ENV_REQUIRED and bad(value)]
    warned = [label for label, value in rows.items() if label not in ENV_REQUIRED and bad(value)]

    detail = [f"{label}: {value}" for label, value in rows.items() if bad(value)]
    if failed:
        status, reason = FAIL, "缺少必要環境：" + "、".join(sorted(failed))
    elif warned:
        status, reason = WARN, "選配缺席：" + "、".join(warned)
    else:
        status, reason = OK, "check_env 全部就位"
    return Check("environment", status, reason, detail, {"rows": rows})


# -- 2. BlackHole loopback --------------------------------------------------


def check_loopback(seconds: float = 1.5) -> Check:
    """SPEC Phase 1 — is the driver itself alive, independent of any routing?"""
    audio_check = _audio_check()
    try:
        audio_check.load_sounddevice()
    except audio_check.CheckError as exc:
        return Check(
            "blackhole loopback",
            WARN,
            "skipped — sounddevice 不可用（uv sync --extra audio）",
            [str(exc).splitlines()[0]],
        )
    try:
        code, payload = _run_audio_command(["loopback", "--seconds", f"{seconds:g}", "--json"])
    except audio_check.CheckError as exc:
        return Check("blackhole loopback", FAIL, str(exc).splitlines()[0])
    except Exception as exc:  # noqa: BLE001 - PortAudio raises its own zoo
        return Check("blackhole loopback", FAIL, f"{type(exc).__name__}: {exc}")

    ok = code == 0 and bool(payload.get("ok"))
    reason = (
        f"BlackHole 回讀 {payload.get('measured_hz', 0):.0f} Hz / RMS "
        f"{payload.get('recorded_rms', 0):.3f}"
        if ok
        else f"loopback 沒有回讀到測試音（RMS {payload.get('recorded_rms', 0):.4f}）"
    )
    return Check("blackhole loopback", OK if ok else FAIL, reason, [], payload)


# -- 3. Multi-Output routing ------------------------------------------------


def check_routing(seconds: float = METER_SECONDS) -> Check:
    """SPEC §8 第一項 — 多重輸出裝置是系統輸出，而且真的有音樂在播。

    A silent meter is a warning, not a failure: it is also what you get when
    the checklist runs before anyone hit play. The reason line says which.
    """
    audio_check = _audio_check()
    try:
        audio_check.load_sounddevice()
    except audio_check.CheckError as exc:
        return Check(
            "multi-output routing",
            WARN,
            "skipped — sounddevice 不可用",
            [str(exc).splitlines()[0]],
        )
    try:
        _, payload = _run_audio_command(["meter", "--seconds", f"{seconds:g}", "--json"])
    except audio_check.CheckError as exc:
        return Check("multi-output routing", FAIL, str(exc).splitlines()[0])
    except Exception as exc:  # noqa: BLE001
        return Check("multi-output routing", FAIL, f"{type(exc).__name__}: {exc}")

    mean_rms = float(payload.get("mean_rms", 0.0) or 0.0)
    threshold = float(payload.get("min_rms_threshold", audio_check.METER_MIN_RMS))
    if mean_rms > threshold:
        return Check(
            "multi-output routing",
            OK,
            f"有訊號 — mean RMS {mean_rms:.3f}，{payload.get('kicks_per_min', 0):.0f} kicks/min",
            [],
            payload,
        )
    return Check(
        "multi-output routing",
        WARN,
        f"no signal — Multi-Output not set or nothing playing (mean RMS {mean_rms:.4f})",
        [audio_check.ROUTING_HINT],
        payload,
    )


# -- 4. codex binary --------------------------------------------------------


def check_codex_binary(timeout: float = 20.0) -> Check:
    """``codex --version`` — free, and the first thing a CLI upgrade breaks."""
    from amv.codex_client import CodexClient, CodexError, find_codex

    try:
        binary = find_codex()
    except CodexError as exc:
        return Check("codex binary", FAIL, str(exc).splitlines()[0])
    try:
        version = CodexClient(binary=binary, timeout=timeout).version()
    except CodexError as exc:
        return Check("codex binary", FAIL, f"{binary}: {exc}", [], {"binary": str(binary)})
    return Check(
        "codex binary",
        OK,
        f"{version or 'version unknown'} — {binary}",
        [],
        {"binary": str(binary), "version": version},
    )


# -- 5. codex smoke test ----------------------------------------------------


def check_codex_smoke(real: bool = False, timeout: float = 40.0) -> Check:
    """SPEC §8「``codex exec`` smoke test 通過」— one real decision, end to end.

    Skipped unless asked for: this is the one item on the checklist that costs
    ~25k tokens of the show's quota (SPEC §2). ``$AMV_CODEX_BIN`` counts as
    asking, because that is how ``tools/fake_codex.py`` gets driven for free.
    """
    from amv.codex_client import CodexClient, CodexError
    from amv.director import History, build_prompt

    env_bin = os.environ.get("AMV_CODEX_BIN")
    if not real and not env_bin:
        return Check(
            "codex smoke test",
            WARN,
            "skipped — 加 --real-codex 才會花額度，或設 $AMV_CODEX_BIN 用假的",
        )
    try:
        client = CodexClient(timeout=timeout)
    except CodexError as exc:
        return Check("codex smoke test", FAIL, str(exc).splitlines()[0])

    # find_codex() honours $AMV_CODEX_BIN first, so --real-codex with that set
    # is still the fake. Say so rather than letting the flag imply otherwise.
    note = (
        [f"$AMV_CODEX_BIN 指向 {client.binary}，--real-codex 用的是它，不是 PATH 上的 codex"]
        if real and env_bin
        else []
    )
    prompt = build_prompt(DRILL_SUMMARY, "steady", History(), 145.0, 0.0)
    started = time.monotonic()
    try:
        decision = client.decide(prompt)
    except CodexError as exc:
        return Check(
            "codex smoke test",
            FAIL,
            f"decide 失敗：{exc}"[:200],
            note,
            {"binary": str(client.binary), "real": bool(real)},
        )
    elapsed = time.monotonic() - started
    return Check(
        "codex smoke test",
        OK,
        f"{elapsed:.1f}s → {decision['scene']}/{decision['palette']}"
        + ("" if real else f"（{client.binary.name}，未花額度）"),
        note,
        {
            "binary": str(client.binary),
            "real": bool(real),
            "latency_s": round(elapsed, 3),
            "decision": decision,
        },
    )


# -- 6. fallback drill ------------------------------------------------------


def check_fallback_drill(deadline_s: float = FALLBACK_DEADLINE_S) -> Check:
    """SPEC §8「拔網路：30 s 內 fallback 接管」, rehearsed without a quota bill.

    ``tools/fake_codex.py`` in ``fail`` mode is a Codex outage that costs
    nothing, and :class:`~amv.director.GPTDirector` is the real one, so what is
    being measured here is the actual failover path: a schema-valid decision
    out of the rule director, inside ``deadline_s``.
    """
    from amv.director import GPTDirector, History, RuleDirector

    if not FAKE_CODEX.is_file():
        return Check("fallback drill", FAIL, f"{FAKE_CODEX} 不存在")
    if not os.access(FAKE_CODEX, os.X_OK):
        return Check("fallback drill", FAIL, f"{FAKE_CODEX} 沒有執行權限（chmod +x）")

    from amv.codex_client import CodexClient

    with _env(AMV_CODEX_BIN=str(FAKE_CODEX), AMV_FAKE_CODEX_MODE="fail"):
        client = CodexClient(binary=FAKE_CODEX, timeout=deadline_s)
        director = GPTDirector(client, RuleDirector())
        started = time.monotonic()
        decision = director.decide(DRILL_SUMMARY, "drop", History(), 0.0, 145.0)
        elapsed = time.monotonic() - started

    source = str(decision.get("_source", ""))
    took_over = source.startswith("rule")
    in_time = elapsed <= deadline_s
    data = {
        "elapsed_s": round(elapsed, 3),
        "deadline_s": deadline_s,
        "source": source,
        "scene": decision.get("scene"),
        "palette": decision.get("palette"),
    }
    if took_over and in_time:
        return Check(
            "fallback drill",
            OK,
            f"codex fail → rule 接手 {elapsed:.2f}s（{decision['scene']}/{decision['palette']}）",
            [],
            data,
        )
    if not took_over:
        return Check("fallback drill", FAIL, f"沒有落到 rule director（_source={source!r}）", [], data)
    return Check(
        "fallback drill",
        FAIL,
        f"rule 接手但花了 {elapsed:.2f}s > {deadline_s:g}s",
        [],
        data,
    )


# -- 7. recording disk space ------------------------------------------------


def check_record_space(record_dir: str | Path | None = None, min_gb: float = MIN_RECORD_GB) -> Check:
    """SPEC §8「Movie File Out 錄影」— somewhere to put the recording."""
    path = Path(record_dir).expanduser() if record_dir else Path.home() / "Movies"
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free_gb = _gb_free(probe)
    except OSError as exc:
        return Check("record disk space", FAIL, f"{path}: {exc}")

    data = {"path": str(path), "measured_on": str(probe), "free_gb": round(free_gb, 1), "min_gb": min_gb}
    if not path.exists():
        return Check(
            "record disk space",
            WARN,
            f"{path} 不存在（{probe} 還有 {free_gb:.0f} GB）",
            [],
            data,
        )
    if free_gb < min_gb:
        return Check(
            "record disk space",
            FAIL,
            f"{path} 只剩 {free_gb:.1f} GB，需要 > {min_gb:g} GB",
            [],
            data,
        )
    return Check("record disk space", OK, f"{path} 還有 {free_gb:.0f} GB", [], data)


# -- 8. codex session litter ------------------------------------------------


def check_sessions(
    sessions_dir: str | Path | None = None, warn_at: int = SESSION_WARN_COUNT
) -> Check:
    """SPEC §7「``~/.codex/sessions`` 堆滿」— count them, do not read them."""
    path = Path(sessions_dir).expanduser() if sessions_dir else SESSIONS_DIR
    if not path.exists():
        return Check("~/.codex/sessions", OK, f"{path} 不存在（沒有堆積）", [], {"count": 0})
    count = 0
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            count += 1
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    mb = total / (1024.0**2)
    data = {"path": str(path), "count": count, "total_mb": round(mb, 1), "warn_at": warn_at}
    if count > warn_at:
        return Check(
            "~/.codex/sessions",
            WARN,
            f"{count} 個檔案 / {mb:.0f} MB > {warn_at}，跑 tools/codex_sessions.py archive",
            [],
            data,
        )
    return Check("~/.codex/sessions", OK, f"{count} 個檔案 / {mb:.0f} MB", [], data)


# -- the checklist ----------------------------------------------------------

#: Item order is the order a human would do them in: environment, then audio in,
#: then the director, then the things that only matter once it is running.
CHECK_ORDER: tuple[str, ...] = (
    "environment",
    "blackhole loopback",
    "multi-output routing",
    "codex binary",
    "codex smoke test",
    "fallback drill",
    "record disk space",
    "~/.codex/sessions",
)


def run_checks(
    *,
    real_codex: bool = False,
    record_dir: str | Path | None = None,
    sessions_dir: str | Path | None = None,
    skip_audio: bool = False,
    meter_seconds: float = METER_SECONDS,
    on_result: Callable[[Check], None] | None = None,
) -> list[Check]:
    """Run every item in :data:`CHECK_ORDER`, in order.

    ``on_result`` is called as each row lands so the human sees ``✅`` appear
    one at a time rather than after the meter has finished listening.
    """
    runners: list[tuple[str, Callable[[], Check]]] = [
        ("environment", check_environment),
        (
            "blackhole loopback",
            (
                (lambda: Check("blackhole loopback", WARN, "skipped — --skip-audio"))
                if skip_audio
                else check_loopback
            ),
        ),
        (
            "multi-output routing",
            (
                (lambda: Check("multi-output routing", WARN, "skipped — --skip-audio"))
                if skip_audio
                else (lambda: check_routing(meter_seconds))
            ),
        ),
        ("codex binary", check_codex_binary),
        ("codex smoke test", lambda: check_codex_smoke(real_codex)),
        ("fallback drill", check_fallback_drill),
        ("record disk space", lambda: check_record_space(record_dir)),
        ("~/.codex/sessions", lambda: check_sessions(sessions_dir)),
    ]

    results: list[Check] = []
    for name, runner in runners:
        try:
            result = runner()
        except BaseException as exc:  # noqa: BLE001 - one broken row is not a broken checklist
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            result = Check(name, FAIL, f"檢查本身炸了：{type(exc).__name__}: {exc}")
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


def format_report(results: Sequence[Check]) -> list[str]:
    """The closing summary: counts, and what to do about a ``❌``."""
    counts = {OK: 0, WARN: 0, FAIL: 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    lines = [
        "-" * 72,
        f"{MARK[OK]} {counts[OK]}   {MARK[WARN]} {counts[WARN]}   {MARK[FAIL]} {counts[FAIL]}",
    ]
    if counts[FAIL]:
        lines.append("上場前必須修掉：" + "、".join(r.name for r in results if r.status == FAIL))
    elif counts[WARN]:
        lines.append("可以上場，但先確認這幾項是你有意跳過的："
                     + "、".join(r.name for r in results if r.status == WARN))
    else:
        lines.append("全綠。TD 那半邊（波形、獨立演完一首、熱鍵、on_drop、錄影）請人工確認。")
    return lines


# -- CLI --------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tools/preflight.py",
        description="SPEC §8 pre-show checklist (the half that does not need TouchDesigner).",
    )
    parser.add_argument(
        "--real-codex",
        action="store_true",
        help="run the smoke test against the real codex binary (spends ~25k tokens)",
    )
    parser.add_argument(
        "--record-dir", default=None, help="where Movie File Out will write (default ~/Movies)"
    )
    parser.add_argument(
        "--sessions-dir", default=None, help="override ~/.codex/sessions (tests)"
    )
    parser.add_argument(
        "--skip-audio", action="store_true", help="skip loopback and meter (no interface attached)"
    )
    parser.add_argument(
        "--meter-seconds",
        type=float,
        default=METER_SECONDS,
        help=f"how long to listen for routed audio (default {METER_SECONDS:g})",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable result")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    width = max(len(name) for name in CHECK_ORDER)

    def emit(result: Check) -> None:
        print(f"{result.mark}  {result.name.ljust(width)}  {result.reason}", flush=True)
        for line in result.detail:
            print(f"      {line}", flush=True)

    if not args.json:
        print("AMV preflight — SPEC §8 上場前檢查")
        print("-" * 72)

    results = run_checks(
        real_codex=args.real_codex,
        record_dir=args.record_dir,
        sessions_dir=args.sessions_dir,
        skip_audio=args.skip_audio,
        meter_seconds=args.meter_seconds,
        on_result=None if args.json else emit,
    )
    failed = [r for r in results if r.status == FAIL]

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not failed,
                    "checks": [r.as_dict() for r in results],
                    "counts": {
                        status: sum(1 for r in results if r.status == status)
                        for status in (OK, WARN, FAIL)
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for line in format_report(results):
            print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
