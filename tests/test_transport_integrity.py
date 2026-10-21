"""Transport: frame atomicity, the send timeout and split frames.

Contract items covered here:

* **B** — ``TCPStreamer.send()`` tears the connection down when only part of
  a frame reached the wire.  A truncated frame must never be followed by
  another frame on the same stream.
* **B** — the send path is bounded by a timeout (0.25 s by default) so a
  stalled peer cannot block the producer.
* **C4** — a frame that arrives split across two TCP reads is delivered
  exactly once and unchanged.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from conftest import wait_until
from fakes import (
    OFF_BUTTONS_LOW,
    OFF_DPAD,
    OFF_PROTO,
    PAYLOAD_SIZE,
    PING_PAYLOAD,
    PROTO_VERSION,
    ScriptedSocket,
)


def _frame(buttons_low: int = 0, buttons_high: int = 0, dpad: int = 0) -> bytes:
    """Build a valid 54-byte v2 state frame (no checksum — legacy sender)."""
    pkt = struct.pack(
        "<HHHHBBBBBBBBBB",
        32768, 32768, 32768, 32768,
        0, 0,
        buttons_low & 0xFF,
        buttons_high & 0xFF,
        dpad & 0x0F,
        PROTO_VERSION,
        0xFF,
        0x00,
        0, 0,
    )
    return pkt + b"\x00" * 6 + b"\x00" * 16 + b"\x00" * 14


# ---------------------------------------------------------------------------
# B — the send timeout
# ---------------------------------------------------------------------------

def test_the_default_send_timeout_is_250ms(network):
    """B: the send timeout must be long enough for a slow link but bounded."""
    streamer = network.TCPStreamer("127.0.0.1", 1)
    assert streamer._send_timeout_s == 0.25


def test_the_socket_timeout_is_restored_after_a_send(network):
    """B: the per-send timeout must not leak into the keepalive path."""
    streamer = network.TCPStreamer("127.0.0.1", 1)
    sock = ScriptedSocket()
    streamer._sock = sock

    assert streamer.send(_frame(buttons_low=0x01)) is True
    assert sock.timeouts[0] == 0.25
    assert sock.timeouts[-1] is None, \
        "the socket was left in timeout mode — the keepalive PING would " \
        "time out as well"


class _FakeSocket:
    """A socket whose keepalive PING blocks forever (the peer stopped reading)."""

    def __init__(self, *args, **kwargs):
        self.sent = bytearray()
        self.closed = False
        self.ping_started = threading.Event()
        self.ping_block = threading.Event()
        self.timeouts = []

    def setsockopt(self, *args):
        pass

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def connect(self, addr):
        pass

    def sendall(self, data):
        if data == PING_PAYLOAD:
            # The peer stopped reading: the keepalive blocks in the kernel.
            self.ping_started.set()
            self.ping_block.wait(5.0)
            return
        self.sent += data

    def recv(self, _n):
        return b""

    def shutdown(self, _how):
        pass

    def close(self):
        self.closed = True


class _FakeSocketModule:
    """Just enough of the ``socket`` module for ``linux/src/network.py``."""

    AF_INET = 2
    SOCK_STREAM = 1
    IPPROTO_TCP = 6
    TCP_NODELAY = 1
    TCP_QUICKACK = 12
    SHUT_RDWR = 2

    class timeout(Exception):
        pass

    def __init__(self):
        self.sockets = []

    def socket(self, *args, **kwargs):
        sock = _FakeSocket()
        self.sockets.append(sock)
        return sock

    @property
    def last_socket(self):
        return self.sockets[-1]


def test_a_stalled_peer_does_not_block_the_state_frame_send(network,
                                                            monkeypatch):
    """B: a stalled peer must not block the state frames.

    The keepalive PING is written with a plain ``sendall()`` and no
    timeout.  When the peer stops reading, that call blocks while it holds
    the streamer's lock — and every state frame then waits for the same
    lock, so the whole input pipeline stalls.
    """
    fake = _FakeSocketModule()
    monkeypatch.setattr(network, "socket", fake)

    streamer = network.TCPStreamer("127.0.0.1", 1)
    streamer.start()
    try:
        assert wait_until(lambda: streamer.connected, timeout=2.0), \
            "the streamer did not connect"
        # The keepalive PING runs after one second and blocks in sendall().
        assert wait_until(lambda: fake.last_socket.ping_started.is_set(),
                          timeout=3.0), \
            "the keepalive PING did not block — the test setup is wrong"

        result: list[bool] = []
        worker = threading.Thread(target=lambda: result.append(
            streamer.send(_frame(buttons_low=0x01))))
        worker.start()
        worker.join(timeout=1.5)
        blocked = worker.is_alive()
    finally:
        fake.last_socket.ping_block.set()
        worker.join(timeout=2)
        streamer.stop()

    assert not blocked, \
        "the state frame send blocked behind the stalled keepalive PING — " \
        "the input pipeline stalls when the peer stops reading"


# ---------------------------------------------------------------------------
# B — frame atomicity
# ---------------------------------------------------------------------------

def test_a_truncated_frame_is_not_followed_by_another_frame(network):
    """B: nothing may be written behind a half-written frame."""
    streamer = network.TCPStreamer("127.0.0.1", 1)

    class _HalfSocket:
        def __init__(self):
            self.written = bytearray()
            self.calls = 0

        def settimeout(self, _t):
            pass

        def setsockopt(self, *_a):
            pass

        def sendall(self, data):
            self.calls += 1
            if self.calls == 1:
                self.written += data[:30]
                raise socket.timeout("timed out")
            self.written += data

        def recv(self, _n):
            return b""

        def shutdown(self, _how):
            pass

        def close(self):
            pass

    sock = _HalfSocket()
    streamer._sock = sock

    streamer.send(_frame(buttons_low=0x01))
    streamer.send(_frame(buttons_low=0x00))

    assert len(sock.written) <= 30, \
        "a second frame was written behind the truncated one"
    assert streamer.connected is False, \
        "the connection stayed alive after a truncated frame"


def test_a_failed_send_takes_the_connection_down(network):
    """B: a failed send must not leave a corrupted stream alive."""
    streamer = network.TCPStreamer("127.0.0.1", 1)
    sock = ScriptedSocket()
    streamer._sock = sock

    class _Boom(Exception):
        pass

    def _explode(_data):
        raise ConnectionResetError("peer went away")

    sock.sendall = _explode

    assert streamer.send(_frame()) is False
    assert streamer.connected is False


# ---------------------------------------------------------------------------
# C4 — a frame split across two reads
# ---------------------------------------------------------------------------

def test_a_frame_split_across_two_reads_is_delivered_once(receiver):
    """C4: a split frame is delivered exactly once and unchanged."""
    states: list[dict] = []
    r = receiver.TCPReceiver("127.0.0.1", 0, on_state=states.append)
    r._running = True

    frame = _frame(buttons_low=0x05, dpad=0x03)
    sock = ScriptedSocket(frame[:20] + frame[20:], chunk_size=20)
    r._conn = sock
    r._conn_generation = 1
    r._serve(sock, ("127.0.0.1", 12345), 1)

    assert len(states) == 1, \
        f"the split frame was delivered {len(states)} times"
    state = states[0]
    assert state["buttons_low"] == 0x05
    assert state["dpad"] == 0x03
    assert state["lthumb_x"] == 32768


def test_a_split_frame_is_not_duplicated_by_the_next_frame(receiver):
    """C4: two frames in two reads must produce two states — no more."""
    states: list[dict] = []
    r = receiver.TCPReceiver("127.0.0.1", 0, on_state=states.append)
    r._running = True

    first = _frame(buttons_low=0x01)
    second = _frame(buttons_low=0x00)
    sock = ScriptedSocket(first + second, chunk_size=30)
    r._conn = sock
    r._conn_generation = 1
    r._serve(sock, ("127.0.0.1", 12345), 1)

    assert [s["buttons_low"] for s in states] == [0x01, 0x00]


def test_a_real_split_frame_over_tcp_is_delivered_once(receiver):
    """C4: the same over a real socket."""
    import socket as _socket

    states: list[dict] = []
    r = receiver.TCPReceiver("127.0.0.1", 0, on_state=states.append)
    r.start()
    try:
        deadline = time.monotonic() + 2.0
        port = None
        while time.monotonic() < deadline:
            listener = getattr(r, "_listener", None)
            if listener is not None:
                try:
                    port = listener.getsockname()[1]
                except OSError:
                    port = None
            if port:
                break
            time.sleep(0.005)
        assert port, "the receiver did not start listening"

        client = _socket.create_connection(("127.0.0.1", port), timeout=5.0)
        try:
            frame = _frame(buttons_low=0x03)
            client.sendall(frame[:17])
            time.sleep(0.05)
            assert not states, "a truncated frame was reported as a state"
            client.sendall(frame[17:])
            assert wait_until(lambda: len(states) == 1, timeout=2.0), \
                "the completed frame was not delivered"
            time.sleep(0.05)
            assert len(states) == 1, "the frame was delivered more than once"
            assert states[0]["buttons_low"] == 0x03
        finally:
            client.close()
    finally:
        r.stop()


# ---------------------------------------------------------------------------
# C — the rumble path must not block the ViGEm notification thread
# ---------------------------------------------------------------------------

def test_the_rumble_path_does_not_block_on_a_stalled_peer(receiver):
    """C: ``send_state()`` must give up instead of blocking the caller.

    The caller is ViGEm's notification thread; it runs while the emitter
    holds the pad lock, so a blocking send there freezes the whole input
    pipeline.
    """
    r = receiver.TCPReceiver("127.0.0.1", 0, on_state=lambda s: None)
    mine, peer = socket.socketpair()
    try:
        # Fill the send buffer so the socket cannot take any more data.
        mine.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        mine.setblocking(False)
        try:
            while True:
                mine.send(b"x" * 4096)
        except BlockingIOError:
            pass
        finally:
            mine.setblocking(True)

        r._conn = mine
        started = time.monotonic()
        assert r.send_state(_frame()) is False, \
            "the rumble packet was accepted although the peer is not reading"
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, \
            f"the rumble send blocked for {elapsed:.2f}s — the ViGEm " \
            "notification thread would freeze the input pipeline"
    finally:
        mine.close()
        peer.close()


def test_the_rumble_path_without_a_connection_is_a_noop(receiver):
    """C: ``send_state()`` without a peer must fail fast."""
    r = receiver.TCPReceiver("127.0.0.1", 0, on_state=lambda s: None)
    started = time.monotonic()
    assert r.send_state(_frame()) is False
    assert time.monotonic() - started < 0.2
