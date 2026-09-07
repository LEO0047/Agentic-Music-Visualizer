"""Tests for amv.osc_io — real UDP on loopback, ephemeral ports, no TD."""

from __future__ import annotations

import json
import threading
import time

import pytest
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

from amv.features import FeatureBuffer
from amv.osc_io import (
    DIRECTOR_ADDRESSES,
    SECTION_ADDRESS,
    FeatureReceiver,
    TDClient,
    parse_endpoint,
)

TIMEOUT_S = 5.0


class Recorder:
    """A tiny OSC server on port 0 that remembers every message it sees."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, tuple]] = []
        self._lock = threading.Lock()
        dispatcher = Dispatcher()
        dispatcher.set_default_handler(self._on_message)
        self._server = ThreadingOSCUDPServer(("127.0.0.1", 0), dispatcher)
        self.host, self.port = self._server.server_address[:2]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    def _on_message(self, address: str, *args) -> None:
        with self._lock:
            self.messages.append((address, args))

    def wait_for(self, count: int, timeout: float = TIMEOUT_S) -> list[tuple[str, tuple]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.messages) >= count:
                    return list(self.messages)
            time.sleep(0.005)
        with self._lock:
            return list(self.messages)

    def addresses(self) -> list[str]:
        with self._lock:
            return [a for a, _ in self.messages]

    def close(self) -> None:
        self._server.shutdown()
        self._thread.join(TIMEOUT_S)
        self._server.server_close()


@pytest.fixture
def recorder():
    rec = Recorder()
    try:
        yield rec
    finally:
        rec.close()


@pytest.fixture
def receiver():
    buffer = FeatureBuffer()
    rx = FeatureReceiver("127.0.0.1", 0, buffer).start()
    try:
        yield rx
    finally:
        rx.stop()


def wait_until(predicate, timeout: float = TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def base_decision() -> dict:
    return {
        "scene": "tunnel",
        "palette": "violet_cyan",
        "feedback": 0.42,
        "symmetry": 6,
        "camera_speed": 0.3,
        "particle_mode": "spiral",
        "projectm_mix": 0.0,
        "transition": {"mode": "glide", "beats": 4},
        "on_drop": {
            "scene": "particle_field",
            "palette": "infrared",
            "particle_mode": "burst",
        },
        "intent": "build tension into the drop",
    }


# -- endpoints --------------------------------------------------------------


def test_parse_endpoint():
    assert parse_endpoint("127.0.0.1:9000") == ("127.0.0.1", 9000)
    assert parse_endpoint("9001") == ("127.0.0.1", 9001)
    assert parse_endpoint("0.0.0.0:0") == ("0.0.0.0", 0)
    assert parse_endpoint("localhost:9000") == ("localhost", 9000)
    with pytest.raises(ValueError):
        parse_endpoint("127.0.0.1:not-a-port")


def test_port_zero_binds_something_real(receiver):
    assert receiver.port != 0
    assert receiver.address == ("127.0.0.1", receiver.port)


def test_stop_is_idempotent():
    rx = FeatureReceiver("127.0.0.1", 0, FeatureBuffer())
    rx.start().start()  # starting twice must not spawn a second thread
    rx.stop()
    rx.stop()


# -- roundtrip --------------------------------------------------------------


def test_thirty_messages_land_in_the_buffer(receiver):
    client = TDClient("127.0.0.1", receiver.port)
    for i in range(10):
        client.send("/feat/bass", 0.5 + i / 100)
        client.send("/feat/energy", 0.6)
        client.send("/feat/kick", 1 if i % 2 == 0 else 0)
    client.close()

    assert wait_until(lambda: receiver.received >= 30), f"only got {receiver.received}"
    buffer = receiver.buffer
    assert set(buffer.keys()) == {"bass", "energy", "kick"}
    assert buffer.avg("energy", 60) == pytest.approx(0.6)
    assert buffer.latest("bass") == pytest.approx(0.59)
    assert buffer.count_above("kick", 60, 0.5) == 5


def test_key_is_the_last_path_segment(receiver):
    client = TDClient("127.0.0.1", receiver.port)
    client.send("/feat/centroid", 2200.0)
    client.close()
    assert wait_until(lambda: receiver.buffer.latest("centroid") is not None)
    assert receiver.buffer.latest("centroid") == pytest.approx(2200.0)


def test_non_numeric_arguments_are_ignored(receiver):
    """The sidecar's own /feat/section echo must not become a feature."""
    client = TDClient("127.0.0.1", receiver.port)
    client.send_section("build")
    client.send("/feat/bass", 0.5)
    client.close()
    assert wait_until(lambda: receiver.received >= 1)
    assert wait_until(lambda: receiver.ignored >= 1)
    assert "section" not in receiver.buffer.keys()
    assert receiver.buffer.latest("bass") == pytest.approx(0.5)


def test_non_feature_addresses_are_not_buffered(receiver):
    client = TDClient("127.0.0.1", receiver.port)
    client.send("/director/feedback", 0.9)
    client.send("/feat/mid", 0.4)
    client.close()
    assert wait_until(lambda: receiver.buffer.latest("mid") is not None)
    assert "feedback" not in receiver.buffer.keys()


def test_receiver_works_as_a_context_manager():
    buffer = FeatureBuffer()
    with FeatureReceiver("127.0.0.1", 0, buffer) as rx:
        TDClient("127.0.0.1", rx.port).send("/feat/high", 0.25)
        assert wait_until(lambda: buffer.latest("high") is not None)


# -- outgoing contract ------------------------------------------------------


def test_send_section_uses_the_spec_address(recorder):
    with TDClient("127.0.0.1", recorder.port) as td:
        td.send_section("breakdown")
    messages = recorder.wait_for(1)
    assert messages[0][0] == SECTION_ADDRESS
    assert messages[0][1] == ("breakdown",)


def test_send_director_emits_exactly_the_spec_address_set(recorder):
    with TDClient("127.0.0.1", recorder.port) as td:
        td.send_director(base_decision())
    recorder.wait_for(len(DIRECTOR_ADDRESSES))
    assert recorder.addresses() == list(DIRECTOR_ADDRESSES)


def test_send_director_values_and_wire_types(recorder):
    with TDClient("127.0.0.1", recorder.port) as td:
        td.send_director(base_decision())
    messages = dict(recorder.wait_for(len(DIRECTOR_ADDRESSES)))
    assert messages["/director/scene"] == ("tunnel",)
    assert messages["/director/palette"] == ("violet_cyan",)
    assert messages["/director/feedback"][0] == pytest.approx(0.42, abs=1e-6)
    assert messages["/director/symmetry"] == (6,)
    assert isinstance(messages["/director/symmetry"][0], int)
    assert messages["/director/camera_speed"][0] == pytest.approx(0.3, abs=1e-6)
    assert messages["/director/particle_mode"] == ("spiral",)
    assert messages["/director/projectm_mix"][0] == pytest.approx(0.0)
    assert messages["/director/transition_mode"] == ("glide",)
    assert messages["/director/transition_beats"] == (4,)
    assert messages["/director/heartbeat"] == (1,)


def test_on_drop_travels_as_a_json_string(recorder):
    with TDClient("127.0.0.1", recorder.port) as td:
        td.send_director(base_decision())
    messages = dict(recorder.wait_for(len(DIRECTOR_ADDRESSES)))
    payload = messages["/director/on_drop"][0]
    assert isinstance(payload, str)
    assert json.loads(payload) == base_decision()["on_drop"]


def test_heartbeat_increments_per_decision(recorder):
    with TDClient("127.0.0.1", recorder.port) as td:
        assert td.send_director(base_decision()) == 1
        assert td.send_director(base_decision()) == 2
        assert td.send_director(base_decision(), heartbeat=99) == 99
        td.send_heartbeat(100)
    recorder.wait_for(3 * len(DIRECTOR_ADDRESSES) + 1)
    beats = [a[0] for addr, a in recorder.messages if addr == "/director/heartbeat"]
    assert beats == [1, 2, 99, 100]


def test_heartbeat_is_sent_last(recorder):
    """TD treats the heartbeat as 'this decision is complete'."""
    with TDClient("127.0.0.1", recorder.port) as td:
        td.send_director(base_decision())
    recorder.wait_for(len(DIRECTOR_ADDRESSES))
    assert recorder.addresses()[-1] == "/director/heartbeat"


def test_client_counts_what_it_sent(recorder):
    with TDClient("127.0.0.1", recorder.port) as td:
        td.send_director(base_decision())
        td.send_section("drop")
    assert td.sent == len(DIRECTOR_ADDRESSES) + 1
