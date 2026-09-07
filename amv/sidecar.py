"""``python -m amv.sidecar`` — Phase 3: feature ingestion and section detection.

This is the sidecar with its director removed. It listens to TouchDesigner's
``/feat/*`` stream, keeps a two minute rolling window of it
(:class:`~amv.features.FeatureBuffer`), runs the SPEC §4 state machine over
energy and bass ten times a second (:class:`~amv.sections.SectionDetector`),
and does three things with the result:

* echoes every section change back to TD on ``/feat/section``;
* prints ``HH:MM:SS.s  build → drop`` so a human can watch it against their
  ears, and a compact status line once a second;
* appends one JSON line per transition to ``--log``, which is what you diff
  against ``tools/fake_td.py``'s printed ground truth.

Phase 4 bolts the director on here without reshaping anything: pass an
``on_tick(summary, section)`` callback to :class:`Sidecar` and it is called
with the same dict the director prompt wants, once per status period. A GPT
loop lives inside that callback (rate-limiting itself to 15–20 s), so the
10 Hz detection loop never waits on a 13 second ``codex exec``.

``--speed`` scales the sidecar's own clock. It exists so a compressed test run
still measures real windows: ``tools/fake_td.py --speed 20`` plays two minutes
of track in six seconds, and ``--speed 20`` here makes the 4 s breakdown rule
mean four seconds *of track*. ``--duration`` is the one number that stays on
the process (wall) clock: it says how long to keep the process alive, so it
must not shrink by ``--speed`` and stop the run before the music arrives.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable, Sequence, TextIO

from .features import FeatureBuffer
from .osc_io import FeatureReceiver, TDClient, parse_endpoint
from .sections import SectionDetector

__all__ = ["Sidecar", "ScaledClock", "main", "TICK_S"]

#: How often the section detector is fed, in (scaled) seconds. SPEC §3.1 has
#: features arriving at 10 Hz, so anything faster only re-reads the same value.
TICK_S = 0.1


class ScaledClock:
    """Monotonic seconds since construction, multiplied by ``speed``.

    Returns 0.0 at construction, which makes every printed and logged
    timestamp an offset into the set rather than an unreadable absolute.
    """

    def __init__(self, speed: float = 1.0, source: Callable[[], float] = time.monotonic) -> None:
        if speed <= 0:
            raise ValueError("speed must be positive")
        self.speed = float(speed)
        self._source = source
        self._t0 = source()

    def __call__(self) -> float:
        return (self._source() - self._t0) * self.speed

    def sleep(self, seconds: float) -> None:
        """Sleep ``seconds`` of *scaled* time."""
        real = seconds / self.speed
        if real > 0:
            time.sleep(real)


def _stamp(now: float | None = None) -> str:
    """Wall clock ``HH:MM:SS.s`` for a transition line."""
    now = time.time() if now is None else now
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 10) % 10}"


class Sidecar:
    """The Phase 3 loop: buffer in, section out.

    Args:
        buffer: Feature history, already wired to a receiver.
        detector: Section state machine.
        td: Outgoing OSC client, or ``None`` to print only.
        log_path: JSONL file appended to on every transition.
        status_hz: Status lines per second of track time.
        on_tick: ``on_tick(summary, section)``, called once per status period.
            The Phase 4 director hook.
        clock: Callable returning track time in seconds.
        out: Where the human-readable lines go.
    """

    def __init__(
        self,
        buffer: FeatureBuffer,
        detector: SectionDetector,
        td: TDClient | None = None,
        *,
        log_path: str | Path | None = None,
        status_hz: float = 1.0,
        on_tick: Callable[[dict, str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        out: TextIO | None = None,
    ) -> None:
        self.buffer = buffer
        self.detector = detector
        self.td = td
        self.log_path = Path(log_path) if log_path else None
        self.status_period = 1.0 / status_hz if status_hz and status_hz > 0 else 0.0
        self.on_tick = on_tick
        self.clock = clock
        self.out = out if out is not None else sys.stdout
        self.section = detector.state
        self.transitions: list[dict] = []
        # Track time is measured from the first feature sample, not from
        # process start: the sidecar is normally launched before the music,
        # and a log you cannot line up with the track is a log you cannot use.
        self._t0: float | None = None
        self._log: TextIO | None = None
        self._next_status = 0.0

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> "Sidecar":
        if self.log_path is not None and self._log is None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log = self.log_path.open("a", encoding="utf-8")
        return self

    def close(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None

    def __enter__(self) -> "Sidecar":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def elapsed(self, now: float) -> float:
        """Seconds of track time — zero until the first feature arrives."""
        if self._t0 is None:
            return 0.0
        return now - self._t0

    # -- one 10 Hz tick -----------------------------------------------------

    def tick(self, now: float) -> str | None:
        """Feed the detector once. Returns the new section on a transition.

        Silently does nothing until energy has actually arrived: starting the
        sidecar before TouchDesigner must not invent a steady section out of
        zeros.
        """
        energy = self.buffer.latest("energy")
        if energy is None:
            return None
        if self._t0 is None:
            self._t0 = now
        track = now - self._t0
        bass = self.buffer.latest("bass")
        state = self.detector.update(track, energy, 0.0 if bass is None else bass)
        if state == self.section:
            return None
        previous, self.section = self.section, state
        self._on_transition(track, previous, state)
        return state

    def _on_transition(self, now: float, previous: str, state: str) -> None:
        print(f"{_stamp()}  {previous} → {state}", file=self.out, flush=True)
        record = {
            "t": round(now, 3),
            "wall": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "from": previous,
            "to": state,
            "summary": self.buffer.summary(),
        }
        self.transitions.append(record)
        if self._log is not None:
            self._log.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._log.flush()
        if self.td is not None:
            self.td.send_section(state)

    # -- once a second ------------------------------------------------------

    def status(self, now: float) -> str:
        """Emit the compact status line and run the director hook."""
        summary = self.buffer.summary()
        if self._t0 is None:
            # Nothing has arrived yet. Say so, rather than reporting a
            # confident 0.00 energy that looks like silence in the room.
            line = f"t=+{0.0:6.1f}s  waiting for /feat/energy ..."
            print(line, file=self.out, flush=True)
            return line
        line = (
            f"t=+{self.elapsed(now):6.1f}s  energy {summary['energy']:.2f} "
            f"({summary['energy_30s']:.2f}/30s {summary['energy_trend_30s']})  "
            f"section {self.section:<9}  kicks {summary['kicks_per_min']:.0f}/min"
        )
        print(line, file=self.out, flush=True)
        if self.td is not None:
            # Also a keepalive: UDP drops are silent, and TD should never be
            # left holding a section that changed while a packet went missing.
            self.td.send_section(self.section)
        if self.on_tick is not None:
            self.on_tick(summary, self.section)
        return line

    # -- the loop -----------------------------------------------------------

    def run(
        self,
        duration: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.monotonic,
    ) -> int:
        """Run for ``duration`` wall-clock seconds, or until Ctrl-C.

        ``duration`` is *wall-clock* (process) seconds, counted from the start
        of this call by ``wall`` — deliberately not ``self.clock``, because the
        track clock may be scaled. Under ``--speed 20`` a ``--duration 9``
        measured on the track clock expires after 0.45 real seconds, killing
        the process before the sender has put anything on the wire.
        ``self.clock`` still drives ``tick``, the status lines and the log, so
        the timestamps stay in track time.

        ``duration`` of 0 means forever. Returns the number of transitions.
        """
        self.open()
        started = wall()
        self._next_status = self.clock()
        try:
            while True:
                if duration and wall() - started >= duration:
                    break
                now = self.clock()
                self.tick(now)
                if self.status_period and now >= self._next_status:
                    self.status(now)
                    self._next_status = now + self.status_period
                sleep(TICK_S)
        except KeyboardInterrupt:
            print("", file=self.out)
        finally:
            self.close()
        return len(self.transitions)


# -- CLI --------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m amv.sidecar",
        description="Phase 3 sidecar: ingest /feat/* and detect build/drop/breakdown.",
    )
    parser.add_argument(
        "--listen", default="127.0.0.1:9000", help="OSC endpoint to bind (default 127.0.0.1:9000)"
    )
    parser.add_argument(
        "--td", default="127.0.0.1:9001", help="TouchDesigner OSC endpoint (default 127.0.0.1:9001)"
    )
    parser.add_argument("--log", default=None, help="append one JSON line per transition here")
    parser.add_argument(
        "--rate", type=float, default=1.0, help="status lines per second (default 1.0)"
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="scale the clock to match tools/fake_td.py --speed (default 1.0)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help=(
            "stop this many wall-clock seconds after start-up (never scaled by "
            "--speed); 0 runs until Ctrl-C"
        ),
    )
    parser.add_argument(
        "--window", type=float, default=120.0, help="feature history in seconds (default 120)"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    listen_host, listen_port = parse_endpoint(args.listen)
    td_host, td_port = parse_endpoint(args.td)

    clock = ScaledClock(args.speed)
    buffer = FeatureBuffer(window_s=args.window, rate_hz=10, clock=clock)
    receiver = FeatureReceiver(listen_host, listen_port, buffer)
    td = TDClient(td_host, td_port)
    sidecar = Sidecar(
        buffer,
        SectionDetector(),
        td,
        log_path=args.log,
        status_hz=args.rate,
        clock=clock,
    )

    receiver.start()
    print(
        f"listening on {receiver.host}:{receiver.port} → TD {td_host}:{td_port}"
        + (f"  (x{args.speed:g})" if args.speed != 1.0 else "")
        + (f"  log {args.log}" if args.log else ""),
        flush=True,
    )
    try:
        count = sidecar.run(duration=args.duration, sleep=clock.sleep)
    finally:
        receiver.stop()
        td.close()
    print(
        f"stopped — {receiver.received} feature messages, {count} section transitions",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
