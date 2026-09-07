"""OSC edges of the sidecar (SPEC §3.1 and §3.3).

Two one-way UDP links, both on localhost, both fire-and-forget:

* **In** — TouchDesigner sends ``/feat/*`` at 10 Hz to port 9000.
  :class:`FeatureReceiver` runs a threaded OSC server and drops every numeric
  argument into a :class:`~amv.features.FeatureBuffer`. It is deliberately
  incurious: the key is the last path segment, so a TD patch can add
  ``/feat/whatever`` and the sidecar starts buffering it with no code change.
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
import socket
import threading
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

#: Every address one director decision produces, in send order (SPEC §3.3).
#: ``heartbeat`` is last so TD can treat it as "the decision is complete".
DIRECTOR_ADDRESSES: tuple[str, ...] = (
    "/director/scene",
    "/director/palette",
    "/director/feedback",
    "/director/symmetry",
    "/director/camera_speed",
    "/director/particle_mode",
    "/director/projectm_mix",
    "/director/transition_mode",
    "/director/transition_beats",
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
        if not key or not args:
            self.ignored += 1
            return
        value = args[0]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.ignored += 1
            return
        self.buffer.push(key, float(value))
        self.received += 1


class TDClient:
    """Outgoing OSC to TouchDesigner (SPEC §3.3), plus the section echo."""

    def __init__(self, host: str = "127.0.0.1", port: int = 9001) -> None:
        self.host = host
        self.port = int(port)
        self._client = SimpleUDPClient(self.host, self.port)
        self.sent = 0
        self._heartbeat = 0

    # -- primitives ---------------------------------------------------------

    def send(self, address: str, value: Any) -> None:
        """Send one message; never raises on a dead listener (UDP)."""
        self._client.send_message(address, value)
        self.sent += 1

    def close(self) -> None:
        sock = getattr(self._client, "_sock", None)
        if sock is not None:
            sock.close()

    def __enter__(self) -> "TDClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the contract -------------------------------------------------------

    def send_section(self, name: str) -> None:
        """Echo the detected section back to TD on ``/feat/section``."""
        self.send(SECTION_ADDRESS, str(name))

    def send_heartbeat(self, n: int) -> None:
        """``/director/heartbeat`` — TD warns if this stops for 45 s."""
        self._heartbeat = int(n)
        self.send("/director/heartbeat", int(n))

    def send_director(self, decision: dict, heartbeat: int | None = None) -> int:
        """Publish one validated decision as the full ``/director/*`` set.

        Sends exactly the addresses in :data:`DIRECTOR_ADDRESSES`, in order.
        ``decision`` is expected to have been through
        :func:`amv.schema.validate_and_clamp` already; the coercions here are
        about OSC wire types (int32 vs float32), not about trusting the LLM.

        Returns the heartbeat value that was sent.
        """
        for address, field, cast in _SCALAR_FIELDS:
            self.send(address, cast(decision[field]))
        transition = decision.get("transition") or {}
        self.send("/director/transition_mode", str(transition.get("mode", "glide")))
        self.send("/director/transition_beats", int(transition.get("beats", 1)))
        self.send("/director/on_drop", json.dumps(decision.get("on_drop", {}), sort_keys=True))
        beat = self._heartbeat + 1 if heartbeat is None else int(heartbeat)
        self.send_heartbeat(beat)
        return beat
