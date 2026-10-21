"""Linux transport: frame atomicity, keepalive and the idle heartbeat.

Contract items covered here:

* **C3** — the byte stream is never corrupted: a frame is either fully
  written or not started, and two frames never interleave.
* **C8** — when the physical controller produces no new events the Linux
  side still emits a state frame at least every ~250 ms, so a frame that was
  lost in transit self-heals.

The bug this suite reproduces: when the OS accepts only *part* of a frame
(congested link, stalled peer) the bridge keeps the connection alive and
keeps writing further frames behind the half-written one.  The receiver is
then permanently misaligned and decodes garbage as button presses — the
"phantom input" that crashes the game.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time

from conftest import call_with_timeout
from fakes import (
    OFF_PROTO,
    PAYLOAD_SIZE,
    PING_PAYLOAD,
    PROTO_VERSION,
    PartialWriteSocket,
)


def _frame(buttons_low: int = 0, buttons_high: int = 0, dpad: int = 0) -> bytes:
    """Build a valid 54-byte v2 state frame."""
    pkt = struct.pack(
        "<HHHHBBBBBBBBBB",
        32768, 32768, 32768, 32768,   # sticks
        0, 0,                          # lt, rt
        buttons_low & 0xFF,
        buttons_high & 0xFF,
        dpad & 0x0F,
        PROTO_VERSION,
        0xFF,                          # battery unknown
        0x00,                          # flags
        0, 0,                          # rumble
    )
    return pkt + b"\x00" * 6 + b"\x00" * 16 + b"\x00" * 14


# ---------------------------------------------------------------------------
# C3 — frame atomicity
# ---------------------------------------------------------------------------

def test_partial_frame_write_tears_the_connection_down(network):
    """C3: a half-written frame must not stay on a live connection.

    Bug: ``TCPStreamer.send()`` catches ``socket.timeout`` and returns
    ``False`` while the connection (and therefore the truncated frame in the
    kernel buffer) stays alive.  The next frame is appended behind the
    truncated one and the receiver's stream is misaligned forever.
    """
    streamer = network.TCPStreamer("127.0.0.1", 1)
    sock = PartialWriteSocket(accept_bytes=30, exc=socket.timeout("timed out"))
    streamer._sock = sock

    assert streamer.send(_frame(buttons_low=0x01)) is False

    assert streamer.connected is False, \
        "the connection stayed alive after a half-written frame — the next " \
        "frame would be appended to the truncated one"
    assert sock.closed is True, "the socket was not torn down"


def test_no_second_frame_is_written_behind_a_truncated_one(network):
    """C3: no frame may ever follow a truncated frame on the same stream.

    Bug: because the connection survives a partial write, the following
    frame lands directly behind the truncated one — the peer sees
    ``54 + 54`` bytes that no longer decode as two frames.
    """
    streamer = network.TCPStreamer("127.0.0.1", 1)
    sock = PartialWriteSocket(accept_bytes=30, exc=socket.timeout("timed out"))
    streamer._sock = sock

    streamer.send(_frame(buttons_low=0x01))
    streamer.send(_frame(buttons_low=0x00))

    assert len(sock.written) <= 30, \
        "a second frame was written behind the truncated frame"
    assert len(sock.written) % PAYLOAD_SIZE != 0 or len(sock.written) == 0, \
        "the byte stream does not decode as whole frames"


def test_frames_never_interleave(network):
    """C3: two concurrent sends must not interleave their bytes.

    The state frames and the keepalive PING share one socket; a PING that
    lands in the middle of a state frame desynchronises the receiver.
    """
    streamer = network.TCPStreamer("127.0.0.1", 1)

    class SlowSocket:
        """Records everything that is written, in order."""

        def __init__(self):
            self.written = bytearray()
            self._lock = threading.Lock()

        def settimeout(self, _t):
            pass

        def setsockopt(self, *_a):
            pass

        def sendall(self, data):
            time.sleep(0.005)          # give the other thread a chance
            with self._lock:
                self.written += data

        def recv(self, _n):
            return b""

        def shutdown(self, _how):
            pass

        def close(self):
            pass

    sock = SlowSocket()
    streamer._sock = sock

    threads = [
        threading.Thread(target=streamer.send, args=(_frame(buttons_low=0x01),)),
        threading.Thread(target=streamer.send, args=(_frame(buttons_low=0x02),)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    assert len(sock.written) == 2 * PAYLOAD_SIZE
    for offset in (0, PAYLOAD_SIZE):
        chunk = bytes(sock.written[offset:offset + PAYLOAD_SIZE])
        assert chunk[OFF_PROTO] == PROTO_VERSION, \
            "the frames interleaved — the stream is corrupt"


def test_ping_payload_is_a_whole_frame(network):
    """C3: the keepalive PING is a complete 54-byte frame."""
    assert network._PING_PAYLOAD == PING_PAYLOAD
    assert len(network._PING_PAYLOAD) == PAYLOAD_SIZE


# ---------------------------------------------------------------------------
# C8 — idle heartbeat
# ---------------------------------------------------------------------------

def test_idle_controller_still_returns_a_state(controller, fake_evdev):
    """C8: with no input events the reader must still return the state.

    Bug: ``read_next_state()`` loops forever when ``select()`` times out, so
    the Linux side never emits a frame while the controller is idle.  A frame
    that was lost in transit can therefore never be healed.
    """
    device = fake_evdev.new_device()
    state = controller.ControllerState()

    started = time.monotonic()
    result = call_with_timeout(
        lambda: controller.read_next_state(device, {}, state),
        timeout=1.0,
        what="read_next_state on an idle controller")
    elapsed = time.monotonic() - started

    assert result is state
    assert elapsed < 1.0, f"the idle heartbeat took {elapsed:.2f}s"


def test_bridge_poll_sends_a_frame_while_the_controller_is_idle(
        linux_main, controller, fake_evdev, caplog):
    """C8: the bridge keeps emitting state frames while nothing happens.

    Bug: the main loop only sends a frame when the reader returns, and the
    reader never returns while the controller is idle — the Windows side
    therefore stops hearing from the Linux side.
    """
    device = fake_evdev.new_device()
    state = controller.ControllerState()

    sent: list[bytes] = []

    class FakeTCP:
        def send(self, data: bytes) -> bool:
            sent.append(data)
            return True

    app = linux_main.BridgeApp()
    app._device = device
    app._state = state
    app._abs_info = {}
    app._tcp = FakeTCP()

    with caplog.at_level(logging.WARNING):
        call_with_timeout(app._poll, timeout=1.0, what="BridgeApp._poll")

    swallowed = [r.getMessage() for r in caplog.records
                 if r.levelno >= logging.WARNING]
    assert not swallowed, f"the poll loop swallowed an error: {swallowed}"
    assert sent, "no state frame was emitted while the controller was idle"
    assert len(sent[0]) == PAYLOAD_SIZE
