#!/usr/bin/env python3
"""SPEC §2「正式演出前 dry run 60 分鐘看額度」as one command (Phase 6).

    uv run python tools/dry_run.py --minutes 60 --director gpt --report dry_run.md
    uv run python tools/dry_run.py --minutes 2 --speed 20 --director gpt   # compressed
    uv run python tools/dry_run.py --minutes 60 --live --listen 127.0.0.1:9000

It starts ``python -m amv.sidecar`` and (unless ``--live``) ``tools/fake_td.py``
as child processes, lets them run for ``--minutes`` of *show* time, then reads
the decisions JSONL back and answers the two questions the SPEC asks before a
gig:

**Will the quota survive an hour?** SPEC §2 measured one decision at 25,192
tokens. The report multiplies the observed decisions-per-hour by
``--tokens-per-decision`` and says how that lands against the Codex 5-hour and
weekly windows. This is the number that decides whether the period stays at
18 s or goes to 30 s.

**Does it look repetitive?** Gaps between decisions, the longest run without a
scene change, and any scene+palette pair that came back inside
:data:`~amv.director.REPEAT_WINDOW_S` — that last one must be 0, because
``enforce_variety`` is supposed to make it impossible, and a non-zero count
means the anti-repetition rules have a hole rather than that the show was dull.

``--speed`` compresses the run: it is handed to *both* children, so the
sidecar's decision period and the section windows stay in show time while the
wall clock runs N× faster. ``--minutes 60 --speed 20`` is three real minutes and
still produces the ~200 decisions a real hour would. Latencies are always real
seconds (``codex exec`` does not get faster because a clock was scaled), so
latency stats from a compressed run are honest and the gap stats are show time.

``tools/fake_td.py`` plays a ~116 s script and exits; this relaunches it until
the run is over, so an hour of dry run is the same loop of track over and over.
That is fine for quota and repetition — it is not a musical rehearsal.

Ctrl-C at any point terminates both children and still writes the report for
however long it ran.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from amv.director import REPEAT_WINDOW_S  # noqa: E402  (needs the sys.path shim)

__all__ = [
    "TOKENS_PER_DECISION",
    "CODEX_5H_WINDOW_H",
    "load_records",
    "percentile",
    "summarise",
    "render_report",
    "main",
]

#: SPEC §2, 2026-09-08 實測: one decision cost 25,192 tokens end to end, almost
#: all of it the Codex system prompt. Rounded down to a round number because
#: the projection is an order-of-magnitude answer, not an invoice.
TOKENS_PER_DECISION = 25000

#: The two windows a ChatGPT subscription's Codex quota is measured over.
CODEX_5H_WINDOW_H = 5.0
CODEX_WEEK_WINDOW_H = 24.0 * 7.0

FAKE_TD = _REPO_ROOT / "tools" / "fake_td.py"

#: Seconds given to the sidecar to bind its socket before the feed starts. UDP
#: to a closed port is dropped in silence, so this is the difference between a
#: dry run and a dry run that quietly lost its first seconds of track.
STARTUP_GRACE_S = 1.0


# -- reading the log --------------------------------------------------------


def load_records(path: str | Path) -> list[dict[str, Any]]:
    """Every decision line from a sidecar ``--decisions-log``, oldest first.

    Unparseable lines are skipped rather than fatal: the log is appended to
    live, and a run that ends mid-write should still be summarisable.
    """
    records: list[dict[str, Any]] = []
    file = Path(path)
    if not file.is_file():
        return records
    with file.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and "decision" in record:
                records.append(record)
    records.sort(key=lambda r: float(r.get("t", 0.0)))
    return records


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile, ``q`` in 0–1. Empty → 0.0.

    numpy is a dependency, but the summariser is also the thing you paste into
    a terminal on the night, so it stays stdlib.
    """
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _stats(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"n": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "min": 0.0, "max": 0.0}
    return {
        "n": len(values),
        "mean": round(sum(values) / len(values), 3),
        "p50": round(percentile(values, 0.50), 3),
        "p95": round(percentile(values, 0.95), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def summarise(
    records: Sequence[dict[str, Any]],
    *,
    show_seconds: float | None = None,
    tokens_per_decision: int = TOKENS_PER_DECISION,
    repeat_window_s: float = REPEAT_WINDOW_S,
) -> dict[str, Any]:
    """Turn decision records into the numbers the report is made of.

    ``show_seconds`` is the length of the run on the *show* clock; when it is
    omitted it is taken from the log's own timestamps, which under-counts the
    tail (the run kept going after the last decision) and therefore slightly
    over-states decisions per hour. Pass it when you know it.
    """
    times = [float(r.get("t", 0.0)) for r in records]
    latencies = [float(r.get("latency_s", 0.0) or 0.0) for r in records]
    span = float(show_seconds) if show_seconds else ((times[-1] - times[0]) if len(times) > 1 else 0.0)

    by_source: dict[str, int] = {}
    for record in records:
        source = str(record.get("source", "?"))
        by_source[source] = by_source.get(source, 0) + 1

    gaps = [round(b - a, 3) for a, b in zip(times, times[1:])]

    # Longest stretch on one scene, in decisions and in show seconds.
    longest_run = 0
    longest_run_s = 0.0
    run = 0
    run_start = times[0] if times else 0.0
    current: str | None = None
    for record, t in zip(records, times):
        scene = record["decision"].get("scene")
        if scene == current:
            run += 1
        else:
            current, run, run_start = scene, 1, t
        longest_run = max(longest_run, run)
        longest_run_s = max(longest_run_s, t - run_start)

    # scene+palette repeats inside the window. enforce_variety should make this
    # impossible, so every hit is a bug, and the pairs are listed to find it.
    repeats: list[dict[str, Any]] = []
    for i, (record, t) in enumerate(zip(records, times)):
        key = (record["decision"].get("scene"), record["decision"].get("palette"))
        for earlier, earlier_t in zip(records[:i], times[:i]):
            if t - earlier_t >= repeat_window_s:
                continue
            if (earlier["decision"].get("scene"), earlier["decision"].get("palette")) == key:
                repeats.append(
                    {
                        "scene": key[0],
                        "palette": key[1],
                        "t": round(t, 2),
                        "previous_t": round(earlier_t, 2),
                        "apart_s": round(t - earlier_t, 2),
                    }
                )
                break

    hours = span / 3600.0 if span > 0 else 0.0
    per_hour = (len(records) / hours) if hours > 0 else 0.0
    tokens_per_hour = per_hour * tokens_per_decision

    return {
        "decisions": len(records),
        "show_seconds": round(span, 1),
        "by_source": by_source,
        "gpt_share": round(by_source.get("gpt", 0) / len(records), 3) if records else 0.0,
        "latency_s": _stats(latencies),
        "gap_s": _stats(gaps),
        "longest_scene_run": longest_run,
        "longest_scene_run_s": round(longest_run_s, 1),
        "repeat_window_s": repeat_window_s,
        "repeats_within_window": len(repeats),
        "repeats": repeats[:20],
        "tokens_per_decision": tokens_per_decision,
        "decisions_per_hour": round(per_hour, 1),
        "tokens_per_hour": int(round(tokens_per_hour)),
        "tokens_per_5h_window": int(round(tokens_per_hour * CODEX_5H_WINDOW_H)),
        "sections": _section_counts(records),
    }


def _section_counts(records: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        name = str(record.get("section", "?"))
        counts[name] = counts.get(name, 0) + 1
    return counts


# -- the report -------------------------------------------------------------


def _row(label: str, value: Any) -> str:
    return f"| {label} | {value} |"


def render_report(summary: dict[str, Any], meta: dict[str, Any]) -> str:
    """The Markdown ``--report`` file: what was run, what happened, what it costs."""
    latency = summary["latency_s"]
    gap = summary["gap_s"]
    repeats = summary["repeats_within_window"]
    lines: list[str] = [
        "# AMV dry run — SPEC §2 額度與反重複驗收",
        "",
        f"- 產生時間：{meta.get('finished_at')}",
        f"- 指令：`{meta.get('command', '')}`",
        f"- 模式：`--director {meta.get('director')}`，period {meta.get('period')} s，"
        f"speed ×{meta.get('speed')}",
        f"- 節目長度：{summary['show_seconds'] / 60.0:.1f} 分鐘（實際牆鐘 "
        f"{meta.get('wall_seconds', 0.0) / 60.0:.1f} 分鐘）",
        f"- 特徵來源：{'真實 TD feed' if meta.get('live') else 'tools/fake_td.py'}"
        + (f"（重播 {meta.get('fake_td_runs')} 次）" if not meta.get("live") else ""),
        f"- codex 二進位：`{meta.get('codex_bin') or '(預設 find_codex)'}`",
        f"- 決策 log：`{meta.get('decisions_log')}`",
        "",
        "## 決策",
        "",
        "| 項目 | 值 |",
        "|---|---|",
        _row("decisions", summary["decisions"]),
        _row("by_source", json.dumps(summary["by_source"], ensure_ascii=False)),
        _row("gpt 佔比", f"{summary['gpt_share'] * 100:.0f}%"),
        _row("section 分布", json.dumps(summary["sections"], ensure_ascii=False)),
        "",
        "## 延遲（實秒，不受 --speed 影響）",
        "",
        "| 項目 | 秒 |",
        "|---|---|",
        _row("mean", f"{latency['mean']:.2f}"),
        _row("p50", f"{latency['p50']:.2f}"),
        _row("p95", f"{latency['p95']:.2f}"),
        _row("max", f"{latency['max']:.2f}"),
        "",
        "## 節奏（節目時間）",
        "",
        "| 項目 | 值 |",
        "|---|---|",
        _row("決策間隔 min / mean / max", f"{gap['min']:.1f} / {gap['mean']:.1f} / {gap['max']:.1f} s"),
        _row(
            "最長沒換 scene",
            f"{summary['longest_scene_run']} 次決策 / {summary['longest_scene_run_s']:.0f} s",
        ),
        _row(
            f"{summary['repeat_window_s']:.0f} s 內 scene+palette 重複",
            f"{repeats}{'  ✅' if repeats == 0 else '  ❌ 應為 0'}",
        ),
        "",
    ]

    if repeats:
        lines += [
            "### 重複明細（enforce_variety 應該讓這張表是空的）",
            "",
            "| scene | palette | t | 上次 t | 相隔 s |",
            "|---|---|---|---|---|",
        ]
        for hit in summary["repeats"]:
            lines.append(
                f"| {hit['scene']} | {hit['palette']} | {hit['t']} | "
                f"{hit['previous_t']} | {hit['apart_s']} |"
            )
        lines.append("")

    per_hour = summary["decisions_per_hour"]
    tokens_h = summary["tokens_per_hour"]
    period = float(meta.get("period") or 18.0)
    relaxed = int(round(tokens_h * period / 30.0)) if period else tokens_h
    fell_back = sum(n for source, n in summary["by_source"].items() if source != "gpt")
    fallback_note = (
        f"（本次 {fell_back}/{summary['decisions']} 次決策確實由 fallback 接手）"
        if fell_back
        else "（本次全部走 gpt，fallback 沒被觸發——`tools/preflight.py` 的 fallback drill 才是它的驗收）"
    )
    lines += [
        "## Codex 額度推估（SPEC §2）",
        "",
        f"每次決策以 **{summary['tokens_per_decision']:,} tokens** 估"
        "（2026-09-08 實測 25,192，大多是 Codex 系統提示）。",
        "",
        "| 項目 | 值 |",
        "|---|---|",
        _row("decisions / hour", f"{per_hour:.0f}"),
        _row("tokens / hour", f"{tokens_h:,}"),
        _row(f"tokens / {CODEX_5H_WINDOW_H:.0f} 小時窗", f"{summary['tokens_per_5h_window']:,}"),
        "",
        "> **SPEC §2 警告**：用量計入 ChatGPT 訂閱的 Codex 額度，而額度是以"
        f" **{CODEX_5H_WINDOW_H:.0f} 小時滾動窗**與**每週窗**結算的。"
        f"以這個節奏連跑 {CODEX_5H_WINDOW_H:.0f} 小時就是"
        f" {summary['tokens_per_5h_window'] / 1_000_000:.1f}M tokens，"
        "很可能在演出中途撞窗。",
        ">",
        f"> 對策照 SPEC §7：規則式 fallback 常駐{fallback_note}，"
        f"並在 set 中隨時把 `--period` 從 {period:g} s 拉到 30 s——"
        f"那會把每小時的量降到約 {relaxed:,} tokens。",
        "",
        "## 判讀",
        "",
    ]

    verdicts: list[str] = []
    if summary["decisions"] == 0:
        verdicts.append("- ❌ 一次決策都沒有：確認 sidecar 有收到 `/feat/*`，且 `--period` 短於整段長度。")
    if repeats:
        verdicts.append(f"- ❌ {repeats} 次 60 s 內重複：`enforce_variety` 有漏洞，回去看 amv/director.py。")
    else:
        verdicts.append("- ✅ 60 s 內沒有重複的 scene+palette。")
    if summary["by_source"].get("gpt", 0) and summary["gpt_share"] < 0.9:
        verdicts.append(
            f"- ⚠️ 只有 {summary['gpt_share'] * 100:.0f}% 的決策來自 gpt，"
            "其餘是 fallback；看 by_source 的錯誤類型。"
        )
    if latency["p95"] > 20.0:
        verdicts.append(f"- ⚠️ p95 延遲 {latency['p95']:.1f}s，逼近 30 s timeout，考慮 `--period 30`。")
    if summary["longest_scene_run"] >= 10:
        verdicts.append(
            f"- ⚠️ 最長 {summary['longest_scene_run']} 次決策沒換 scene（強制換場門檻是 10）。"
        )
    lines += verdicts
    lines.append("")
    return "\n".join(lines)


# -- running the children ---------------------------------------------------


def _terminate(proc: subprocess.Popen | None, timeout: float = 5.0) -> None:
    """SIGTERM, then SIGKILL. A dry run must not leave a sidecar on the port."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:  # pragma: no cover - the OS gave up too
            pass
    except OSError:  # pragma: no cover - already gone
        pass


def _sidecar_argv(args: argparse.Namespace, decisions_log: Path, wall_seconds: float) -> list[str]:
    return [
        sys.executable,
        "-m",
        "amv.sidecar",
        "--listen",
        args.listen,
        "--td",
        args.td,
        "--director",
        args.director,
        "--period",
        f"{args.period:g}",
        "--speed",
        f"{args.speed:g}",
        "--duration",
        f"{wall_seconds:g}",
        "--decisions-log",
        str(decisions_log),
        "--rate",
        f"{args.status_rate:g}",
    ]


def _fake_td_argv(args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        str(FAKE_TD),
        "--to",
        args.listen,
        "--speed",
        f"{args.speed:g}",
        "--script",
        args.script,
    ]


# -- CLI --------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tools/dry_run.py",
        description="SPEC §2 的 60 分鐘 dry run：跑 sidecar + 合成 TD，統計節奏與 Codex 額度。",
    )
    parser.add_argument("--minutes", type=float, default=60.0, help="show minutes (default 60)")
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="compress the wall clock; 20 runs 60 show-minutes in 3 real minutes",
    )
    parser.add_argument(
        "--director", choices=("rule", "gpt", "manual"), default="gpt", help="sidecar director mode"
    )
    parser.add_argument("--period", type=float, default=18.0, help="seconds between decisions")
    parser.add_argument("--listen", default="127.0.0.1:9000", help="sidecar OSC listen endpoint")
    parser.add_argument("--td", default="127.0.0.1:9001", help="where the sidecar sends decisions")
    parser.add_argument("--script", default="default", help="tools/fake_td.py script")
    parser.add_argument(
        "--live",
        action="store_true",
        help="do not spawn tools/fake_td.py; expect a real TD feed on --listen",
    )
    parser.add_argument("--report", default=None, help="write the Markdown report here")
    parser.add_argument(
        "--decisions-log", default=None, help="decisions JSONL (default: a temp file)"
    )
    parser.add_argument(
        "--tokens-per-decision",
        type=int,
        default=TOKENS_PER_DECISION,
        help=f"for the quota projection (default {TOKENS_PER_DECISION}, SPEC §2 measurement)",
    )
    parser.add_argument(
        "--status-rate",
        type=float,
        default=1.0,
        help=(
            "sidecar status lines per show second (default 1.0). The director only "
            "gets to fire on a status tick, so lowering this quantises the decision "
            "gaps and makes them read longer than --period"
        ),
    )
    parser.add_argument("--quiet", action="store_true", help="hide the children's output")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON too")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.speed <= 0:
        print("--speed must be positive", file=sys.stderr)
        return 2
    if args.minutes <= 0:
        print("--minutes must be positive", file=sys.stderr)
        return 2

    show_seconds = args.minutes * 60.0
    wall_seconds = show_seconds / args.speed
    tmpdir: tempfile.TemporaryDirectory | None = None
    if args.decisions_log:
        decisions_log = Path(args.decisions_log).expanduser()
        decisions_log.parent.mkdir(parents=True, exist_ok=True)
    else:
        tmpdir = tempfile.TemporaryDirectory(prefix="amv-dry-run-")
        decisions_log = Path(tmpdir.name) / "decisions.jsonl"

    sink = subprocess.DEVNULL if args.quiet else None
    started_wall = time.monotonic()  # replaced once the feed actually starts
    sidecar: subprocess.Popen | None = None
    feeder: subprocess.Popen | None = None
    fake_td_runs = 0
    interrupted = False

    print(
        f"dry run — {args.minutes:g} show-minutes at ×{args.speed:g} "
        f"({wall_seconds / 60.0:.1f} real minutes), director {args.director}, "
        f"period {args.period:g}s",
        flush=True,
    )
    print(f"decisions → {decisions_log}", flush=True)

    try:
        sidecar = subprocess.Popen(
            _sidecar_argv(args, decisions_log, wall_seconds + STARTUP_GRACE_S),
            cwd=str(_REPO_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
        )
        time.sleep(STARTUP_GRACE_S)
        started_wall = time.monotonic()

        while True:
            elapsed = time.monotonic() - started_wall
            if elapsed >= wall_seconds or sidecar.poll() is not None:
                break
            if not args.live and (feeder is None or feeder.poll() is not None):
                feeder = subprocess.Popen(
                    _fake_td_argv(args),
                    cwd=str(_REPO_ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=sink,
                    stderr=sink,
                )
                fake_td_runs += 1
            time.sleep(0.25)
    except KeyboardInterrupt:
        interrupted = True
        print("\ninterrupted — 收拾子行程並照跑到目前為止的統計", flush=True)
    finally:
        _terminate(feeder)
        _terminate(sidecar)

    wall_elapsed = time.monotonic() - started_wall
    records = load_records(decisions_log)
    observed_show_seconds = min(show_seconds, wall_elapsed * args.speed)
    summary = summarise(
        records,
        show_seconds=observed_show_seconds,
        tokens_per_decision=args.tokens_per_decision,
    )
    meta = {
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "command": " ".join([Path(sys.argv[0]).name] + list(sys.argv[1:])),
        "director": args.director,
        "period": args.period,
        "speed": args.speed,
        "live": bool(args.live),
        "fake_td_runs": fake_td_runs,
        "wall_seconds": round(wall_elapsed, 1),
        "decisions_log": str(decisions_log),
        "codex_bin": os.environ.get("AMV_CODEX_BIN"),
        "interrupted": interrupted,
    }
    report = render_report(summary, meta)

    if args.report:
        path = Path(args.report).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")

    latency, gap = summary["latency_s"], summary["gap_s"]
    print("-" * 72, flush=True)
    print(
        f"decisions {summary['decisions']}  "
        f"({json.dumps(summary['by_source'], ensure_ascii=False)})",
        flush=True,
    )
    print(
        f"latency  mean {latency['mean']:.2f}s  p50 {latency['p50']:.2f}s  "
        f"p95 {latency['p95']:.2f}s  max {latency['max']:.2f}s",
        flush=True,
    )
    print(
        f"gaps     min {gap['min']:.1f}s  mean {gap['mean']:.1f}s  max {gap['max']:.1f}s  "
        f"(show time)",
        flush=True,
    )
    print(
        f"variety  longest scene run {summary['longest_scene_run']} decisions / "
        f"{summary['longest_scene_run_s']:.0f}s   "
        f"repeats within {summary['repeat_window_s']:.0f}s: "
        f"{summary['repeats_within_window']}",
        flush=True,
    )
    print(
        f"quota    {summary['decisions_per_hour']:.0f} decisions/h × "
        f"{summary['tokens_per_decision']:,} tokens = {summary['tokens_per_hour']:,} tokens/h "
        f"→ {summary['tokens_per_5h_window'] / 1_000_000:.1f}M per 5h window (SPEC §2 警告："
        "額度以 5 小時／每週窗結算，撞窗就靠 rule fallback 或把 period 拉到 30 s)",
        flush=True,
    )
    if args.report:
        print(f"report   {args.report}", flush=True)
    if args.json:
        print(json.dumps({"summary": summary, "meta": meta}, ensure_ascii=False, indent=2))

    if tmpdir is not None and not args.report:
        # The temp log dies with the process; say so rather than printing a
        # path that will not exist by the time anyone looks.
        print("（沒有 --report 也沒有 --decisions-log：本次 log 已隨暫存目錄消失）", flush=True)
    if tmpdir is not None:
        tmpdir.cleanup()

    if interrupted:
        return 130
    return 0 if summary["decisions"] and not summary["repeats_within_window"] else 1


if __name__ == "__main__":
    # Ctrl-C must reach the finally block above, not the default SIGTERM path.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    raise SystemExit(main())
