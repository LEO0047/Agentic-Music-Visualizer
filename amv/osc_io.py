"""OSC edges of the sidecar (SPEC §3.1 and §3.3).

Two one-way UDP links, both on localhost, both fire-and-forget:

* **In** — TouchDesigner sends ``/feat/*`` at 10 Hz to port 9000.
  :class:`FeatureReceiver` runs a threaded OSC server and drops every numeric
  argument into a :class:`~amv.features.FeatureBuffer`. It is deliberately
  restricted to the six finite numeric feature streams in SPEC §3.1, so
  malformed UDP cannot poison summaries or allocate unbounded feature keys.
* **Out** — :class:`TDClient` sends the section back on ``/feat/section`` and,
  once the director exists, one message per decision field on ``/director/*``.

The director decision is flattened rather than sent as one JSON blob because
TD's OSC In DAT writes each address straight into a Custom Parameter. The two
exceptions are ``transition`` (split into ``transition_mode`` /
``transition_beats``) and ``on_drop``, which stays a JSON string parked in a
Text DAT until the reflex layer detects a drop.

Nothing here retries or blocks: UDP on loopback either arrives or the show has
bigger problems, and the director must never be stalled by the transport.
"""

from __future__ import annotations

import json
import logging
import math
import socket
import threading
import time
from typing import Any, Callable

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer
from pythonosc.udp_client import SimpleUDPClient

from .features import FeatureBuffer

__all__ = [
    "FeatureReceiver",
    "TDClient",
    "FEATURE_PREFIX",
    "SECTION_ADDRESS",
    "DIRECTOR_ADDRESSES",
    "parse_endpoint",
]

FEATURE_PREFIX = "/feat"
SECTION_ADDRESS = "/feat/section"
FEATURE_KEYS = frozenset({"bass", "mid", "high", "energy", "kick", "centroid"})
_LOG = logging.getLogger(__name__)

#: Every address one director decision produces, in send order (SPEC §3.3).
#: ``heartbeat`` is last so TD can treat it as "the decision is complete".
DIRECTOR_ADDRESSES: tuple[str, ...] = (
    "/director/transition_mode",
    "/director/transition_beats",
    "/director/scene",
    "/director/palette",
    "/director/feedback",
    "/director/symmetry",
    "/director/camera_speed",
    "/director/particle_mode",
    "/director/projectm_mix",
    "/director/on_drop",
    "/director/heartbeat",
)

#: Decision fields sent verbatim, with the type TD expects on the wire.
_SCALAR_FIELDS: tuple[tuple[str, str, Callable[[Any], Any]], ...] = (
    ("/director/scene", "scene", str),
    ("/director/palette", "palette", str),
    ("/director/feedback", "feedback", float),
    ("/director/symmetry", "symmetry", int),
    ("/director/camera_speed", "camera_speed", float),
    ("/director/particle_mode", "particle_mode", str),
    ("/director/projectm_mix", "projectm_mix", float),
)


def parse_endpoint(text: str, default_host: str = "127.0.0.1") -> tuple[str, int]:
    """Parse ``host:port`` (or a bare ``port``) into a tuple.

    Port ``0`` is legal and means "let the OS pick", which is how the tests get
    a free port without racing another process.
    """
    text = text.strip()
    if ":" in text:
        host, _, port = text.rpartition(":")
        host = host.strip("[]") or default_host
    else:
        host, port = default_host, text
    try:
        return host, int(port)
    except ValueError as exc:
        raise ValueError(f"not a host:port endpoint: {text!r}") from exc


class FeatureReceiver:
    """Threaded OSC server mapping ``/feat/*`` into a :class:`FeatureBuffer`.

    Args:
        host: Address to bind (``127.0.0.1``).
        port: Port to bind; ``0`` binds a free port, readable afterwards as
            :attr:`port`.
        buffer: Destination buffer. One is created if omitted.

    Non-numeric arguments are ignored — the sidecar's own ``/feat/section``
    echo is a string, and booleans are not features. A message with no
    arguments is ignored too.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9000,
        buffer: FeatureBuffer | None = None,
    ) -> None:
        self.buffer = buffer if buffer is not None else FeatureBuffer()
        self.received = 0
        self.ignored = 0
        self._counter_lock = threading.Lock()
        dispatcher = Dispatcher()
        dispatcher.map(f"{FEATURE_PREFIX}/*", self._on_feature)
        self._server = ThreadingOSCUDPServer((host, port), dispatcher)
        self._server.daemon_threads = True
        # 10 Hz needs nothing, but a compressed replay (tools/fake_td.py
        # --speed 20) pushes a thousand datagrams a second and the default
        # receive buffer starts dropping them.
        try:
            self._server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:  # pragma: no cover - platform dependent, never fatal
            pass
        self._thread: threading.Thread | None = None
        #: socketserver polls this often for a shutdown request; the default
        #: 0.5 s makes stop() (and therefore Ctrl-C) feel stuck.
        self.poll_interval = 0.05
        self.host, self.port = self._server.server_address[:2]

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> "FeatureReceiver":
        """Start serving on a daemon thread. Idempotent."""
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                kwargs={"poll_interval": self.poll_interval},
                name="amv-osc-in",
                daemon=True,
            )
            self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the server and release the socket. Safe to call twice."""
        thread, self._thread = self._thread, None
        if thread is not None:
            self._server.shutdown()
            thread.join(timeout)
        self._server.server_close()

    def __enter__(self) -> "FeatureReceiver":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def address(self) -> tuple[str, int]:
        return self.host, self.port

    # -- handler ------------------------------------------------------------

    def _on_feature(self, address: str, *args: Any) -> None:
        key = address.rsplit("/", 1)[-1]
        value = args[0] if args else None
        valid = (key in FEATURE_KEYS and address == f"{FEATURE_PREFIX}/{key}"
                 and not isinstance(value, bool) and isinstance(value, (int, float)))
        if valid:
            try:
                value = float(value)
                valid = math.isfinite(value) and value >= 0
                if key == "kick":
                    valid = valid and value in (0.0, 1.0)
                elif key != "centroid":
                    valid = valid and value <= 1.0
            except (ValueError, OverflowError):
                valid = False
        with self._counter_lock:
            if not valid:
                self.ignored += 1
                return
            self.buffer.push(key, value)
            self.received += 1


class TDClient:
    """Outgoing OSC to TouchDesigner (SPEC §3.3), plus the section echo."""

    def __init__(self, host: str = "127.0.0.1", port: int = 9001) -> None:
        self.host = host
        self.port = int(port)
        self._client = SimpleUDPClient(self.host, self.port)
        self.sent = 0
        self.send_errors = 0
        self.last_error: str | None = None
        self._last_warning: float | None = None
        self._send_lock = threading.RLock()
        self._heartbeat = 0

    # -- primitives ---------------------------------------------------------

    def send(self, address: str, value: Any) -> bool:
        """Best-effort UDP send, with observable errors and bounded warnings.

        A successful send means the local socket accepted it, not that TD
        acknowledged delivery. Socket pressure must not stop the audio loop.
        """
        with self._send_lock:
            try:
                self._client.send_message(address, value)
            except OSError as exc:
                self.send_errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                now = time.monotonic()
                if self._last_warning is None or now - self._last_warning >= 5.0:
                    _LOG.warning("OSC send failed to %s:%s (%s); %s send errors",
                                 self.host, self.port, self.last_error, self.send_errors)
                    self._last_warning = now
                return False
            self.sent += 1
            return True

    def close(self) -> None:
        sock = getattr(self._client, "_sock", None)
        if sock is not None:
            sock.close()

    def __enter__(self) -> "TDClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the contract -------------------------------------------------------

    def send_section(self, name: str) -> bool:
        """Echo the detected section back to TD on ``/feat/section``."""
        return self.send(SECTION_ADDRESS, str(name))

    def send_heartbeat(self, n: int) -> bool:
        """``/director/heartbeat`` — TD warns if this stops for 45 s."""
        if not self.send("/director/heartbeat", int(n)):
            return False
        self._heartbeat = int(n)
        return True

    def send_director(self, decision: dict, heartbeat: int | None = None) -> int | None:
        """Publish one validated decision as the full ``/director/*`` set.

        Sends exactly the addresses in :data:`DIRECTOR_ADDRESSES`, in order.
        ``decision`` is expected to have been through
        :func:`amv.schema.validate_and_clamp` already; the coercions here are
        about OSC wire types (int32 vs float32), not about trusting the LLM.

        Returns the heartbeat value, or None on a socket error. A partial
        decision never gets a completion heartbeat. Transition controls come
        first because TD applies each target using its current transition mode.
        """
        transition = decision.get("transition") or {}
        messages = [
            ("/director/transition_mode", str(transition.get("mode", "glide"))),
            ("/director/transition_beats", int(transition.get("beats", 1))),
            *((address, cast(decision[field])) for address, field, cast in _SCALAR_FIELDS),
            ("/director/on_drop", json.dumps(decision.get("on_drop", {}), sort_keys=True)),
        ]
        with self._send_lock:
            for address, value in messages:
                if not self.send(address, value):
                    return None
            beat = self._heartbeat + 1 if heartbeat is None else int(heartbeat)
            return beat if self.send_heartbeat(beat) else None
