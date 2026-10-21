"""Round 3 — verification of the D1..D10 fixes.

Each test below maps to one of the fixes that were announced for this
round.  The tests are written so that they encode the *intent* of the
fix, not the current implementation: when a fix is reverted the test
fails.

Contract items covered here:

* **D1** — the checksum is announced by ``flags`` bit 3 (0x08).
  * a legacy peer (no bit 3) that sends a corrupted stream must have its
    connection dropped — and must not deliver a single state,
  * a checksummed peer that sends a corrupted stream must be re-synced
    and only the *valid* frames may be delivered,
  * a frame whose checksum value happens to be 0 is still protected,
  * a PING as the first frame of a connection must not decide the mode.
* **D2** — the keepalive PING must not be able to block the state frame
  send, and a stalled PING must not leave a half-written frame on the
  wire.
* **D3/D4** — after a ``SYN_DROPPED`` the D-pad direction, the triggers
  *and* the sticks are restored, also on a device that only exposes
  ``absinfo()`` (the real ``python-evdev``) and on a device that exposes
  no axis source at all.
* **D5** — the peer flags are reset for every connection.
* **D6** — the monitor loop leaves the loop as soon as the app stops.
* **D7** — the rumble probe never waits for the socket.
* **D8** — the per-frame logs are DEBUG.
* **D9** — the emitter does not keep an unused state timestamp.
* **D10** — ``ControllerState.from_bytes()`` verifies the checksum.
"""

from __future__ import annotations

import logging
import re
import socket
import struct
import threading
import time

import pytest

from conftest import ROOT, call_with_timeout, listening_receiver, wait_until
from fakes import (
    DEFAULT_ABS_INFO,
    FakeAbsInfo,
    FakeInputDevice,
    OFF_BUTTONS_LOW,
    PING_PAYLOAD,
    PROTO_VERSION,
    ScriptedSocket,
)
#: flags bit 2 — "this sender keeps streaming the state".
FLAG_STREAMING = 0x04
#: flags bit 3 — "byte 52 holds a valid checksum".
FLAG_CHECKSUM = 0x08

OFF_FLAGS = 15
OFF_CHECKSUM = 52


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _frame(buttons_low: int = 0, dpad: int = 0,
           flags: int = FLAG_STREAMING | FLAG_CHECKSUM,
           checksum: bool = True,
           name: bytes = b"Xbox Wireless Co") -> bytes:
    """Build a 54-byte v2 state frame.

    *checksum* controls whether byte 52 carries a checksum; the flag bit
    that announces it is passed separately via *flags* so that a "legacy"
    frame (no bit 3) can be built as well.
    """
    pkt = struct.pack(
        "<HHHHBBBBBBBBBB",
        32768, 32768, 32768, 32768,   # sticks
        0, 0,                          # lt, rt
        buttons_low & 0xFF,
        0,                             # buttons_high
        dpad & 0x0F,
        PROTO_VERSION,
        0xFF,                          # battery unknown
        flags,
        0, 0,                          # rumble
    )
    body = pkt + b"\x00" * 6 + name.ljust(16, b"\x00") + b"\x00" * 12
    body += bytes((sum(body) & 0xFF,)) if checksum else b"\x00"
    body += b"\x00"                     # reserved (53)
    return body


def _legacy_frame(buttons_low: int = 0) -> bytes:
    """A frame of an *old* sender: no checksum flag, no checksum byte."""
    return _frame(buttons_low=buttons_low,
                  flags=FLAG_STREAMING,
                  checksum=False)


def _corrupt(frame: bytes, offset: int, xor: int = 0xFF) -> bytes:
    """Flip some bits in *frame* so that it no longer validates."""
    data = bytearray(frame)
    data[offset] ^= xor
    return bytes(data)


def _serve(receiver_module, script: bytes, chunk_size: int = 16):
    """Feed *script* to a receiver, collect the reported states."""
    states: list[dict] = []
    disconnects: list[str] = []
    r = receiver_module.TCPReceiver("127.0.0.1", 0,
                                    on_state=states.append,
                                    on_disconnect=disconnects.append)
    r._running = True
    sock = ScriptedSocket(script, chunk_size=chunk_size)
    r._conn = sock
    r._conn_generation = 1
    r._serve(sock, ("127.0.0.1", 12345), 1)
    return states, disconnects, sock, r


class _RecordingSocket:
    """A socket double that records everything that was written."""

    def __init__(self) -> None:
        self.written = bytearray()
        self.timeouts: list = []
        self.closed = False

    def setsockopt(self, *_a) -> None:
        pass

    def settimeout(self, timeout) -> None:
        self.timeouts.append(timeout)

    def sendall(self, data: bytes) -> None:
        self.written += data

    def recv(self, _n: int) -> bytes:
        return b""

    def shutdown(self, _how) -> None:
        pass

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# D1 — the checksum is announced by a flag
# ---------------------------------------------------------------------------

def test_the_linux_serialiser_always_announces_the_checksum(controller):
    """D1: ``to_bytes()`` always sets flags bit 3 (and bit 2)."""
    frame = controller.ControllerState().to_bytes()
    assert frame[OFF_FLAGS] & FLAG_CHECKSUM, \
        "the checksum is not announced — the receiver cannot verify it"
    assert frame[OFF_FLAGS] & FLAG_STREAMING, \
        "the idle heartbeat is not announced"


def test_a_legacy_peer_with_a_corrupted_stream_is_dropped(receiver):
    """D1: a peer without checksums cannot be re-synced — drop it.

    Nothing tells a shifted window from a real frame without a checksum,
    so the only safe reaction to a misalignment is to tear the connection
    down.  No state may be delivered from such a stream.
    """
    good = _legacy_frame(buttons_low=0x02)
    corrupt = _corrupt(_legacy_frame(buttons_low=0xFF), 13)  # bad proto byte

    states, disconnects, sock, _r = _serve(receiver, corrupt + good)

    assert states == [], \
        f"a misaligned stream was decoded as input: {states}"
    assert disconnects, "the misaligned connection was not dropped"
    assert sock.closed, "the socket was not closed"


def test_a_checksummed_peer_with_a_corrupted_stream_is_resynced(receiver):
    """D1: with checksums the receiver re-syncs and delivers the good frame.

    Every window of a checksummed stream is verified, so the receiver can
    hunt for the next frame boundary byte by byte without ever decoding
    garbage.
    """
    corrupt = _corrupt(_frame(buttons_low=0xFF), 13)      # bad proto byte
    good = _frame(buttons_low=0x02)

    states, _disconnects, _sock, _r = _serve(receiver, corrupt + good)

    assert [s["buttons_low"] for s in states] == [0x02], \
        f"the receiver delivered {states} instead of the one valid frame"


def test_a_zero_checksum_frame_is_protected_end_to_end(receiver):
    """D1: a checksum of 0 is still a checksum.

    The flag announces the checksum, so a frame whose checksum happens to
    be 0 is verified like every other frame: a corrupted copy of it must
    never reach the game.
    """
    # Find a state whose checksum is exactly 0.
    zero_frame = None
    for buttons_low in range(256):
        candidate = _frame(buttons_low=buttons_low)
        if candidate[OFF_CHECKSUM] == 0:
            zero_frame = candidate
            break
    assert zero_frame is not None, "no frame with a zero checksum could be built"
    assert zero_frame[OFF_FLAGS] & FLAG_CHECKSUM

    # It is accepted ...
    states, _d, _s, _r = _serve(receiver, zero_frame)
    assert len(states) == 1, "the valid zero-checksum frame was rejected"

    # ... but a corrupted copy of it is not.
    corrupt = _corrupt(zero_frame, OFF_BUTTONS_LOW)
    assert corrupt[OFF_CHECKSUM] == 0, "the test setup changed"
    states, _d, _s, _r = _serve(receiver, corrupt)
    assert states == [], \
        "a corrupted frame with a zero checksum was delivered as input"


def test_a_ping_as_the_first_frame_does_not_decide_the_peer_mode(receiver):
    """D1: the keepalive must not decide how the stream is validated.

    The Linux side sends a PING every second and a state frame every
    50 ms, so the first frame of a connection is regularly a PING.  The
    mode must be decided by the first *state* frame.
    """
    states, _d, _s, r = _serve(receiver, PING_PAYLOAD + _legacy_frame(0x03))

    assert [s["buttons_low"] for s in states] == [0x03], \
        "the frame after the PING was not delivered"
    assert r._peer_checksums is False, \
        "the peer mode was decided by the PING instead of the state frame"


# ---------------------------------------------------------------------------
# D2 — the keepalive must not block the state frames
# ---------------------------------------------------------------------------

def test_the_state_frame_send_gives_up_when_the_stream_is_busy(network):
    """D2: a stuck writer must not block the input loop forever.

    The lock is acquired with a timeout: when the keepalive is stuck in a
    write the frame is dropped (the next heartbeat carries the same
    state) instead of blocking the evdev loop.
    """
    streamer = network.TCPStreamer("127.0.0.1", 1)
    sock = _RecordingSocket()
    streamer._sock = sock

    # Simulate a keepalive that is stuck inside sendall().
    streamer._lock.acquire()
    try:
        started = time.monotonic()
        sent = streamer.send(_frame(buttons_low=0x01))
        elapsed = time.monotonic() - started
    finally:
        streamer._lock.release()

    assert sent is False, "the frame was reported as sent"
    assert elapsed < 1.0, \
        f"the send blocked for {elapsed:.2f}s — the input loop stalls"
    assert bytes(sock.written) == b"", "something was written behind the lock"


def test_a_stalled_keepalive_ping_drops_the_connection(network, monkeypatch):
    """D2: a PING that cannot be written must not stay on the wire.

    A half-written PING misaligns the stream just like a half-written
    state frame, so the only safe reaction is to drop the connection.
    """
    sock = _PingStallSocket()

    def _fake_socket(*_a, **_kw):
        return sock

    monkeypatch.setattr(network.socket, "socket", _fake_socket)

    streamer = network.TCPStreamer("127.0.0.1", 1)
    streamer._running = True
    worker = threading.Thread(target=streamer._connect_blocking, daemon=True)
    worker.start()
    try:
        assert wait_until(lambda: sock.closed, timeout=5.0), \
            "the connection was kept alive although the PING could not be " \
            "written — a half-written PING stays on the wire"
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert not streamer.connected
    finally:
        streamer._running = False
        worker.join(timeout=5)


class _PingStallSocket:
    """A socket whose writes stall — the peer stopped reading."""

    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False
        self.timeouts: list = []

    def setsockopt(self, *_a) -> None:
        pass

    def settimeout(self, timeout) -> None:
        self.timeouts.append(timeout)

    def connect(self, _addr) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        # Only part of the frame reaches the wire, then the peer stalls.
        self.written += data[:10]
        raise socket.timeout("timed out")

    def recv(self, _n: int) -> bytes:
        return b""

    def shutdown(self, _how) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_a_legacy_frame_with_a_real_checksum_is_still_accepted(controller,
                                                               receiver):
    """D1: the *old* sender format stays compatible.

    The Linux side before the flag was introduced always wrote
    ``sum(bytes 0..51) & 0xFF`` into byte 52 (a non-zero value in most
    cases) but did not announce it.  Such a frame must still be accepted
    — the receiver must not start verifying the checksum of a peer that
    never promised one.
    """
    frame = bytearray(_frame(buttons_low=0x01))
    frame[OFF_FLAGS] &= ~FLAG_CHECKSUM          # a legacy sender
    frame[OFF_CHECKSUM] = sum(frame[:OFF_CHECKSUM]) & 0xFF
    assert frame[OFF_CHECKSUM] != 0, \
        "the test frame must carry a non-zero checksum"

    assert receiver.is_valid_frame(bytes(frame))
    parsed = receiver.StateParser.parse(bytes(frame))
    assert parsed is not None
    assert parsed["buttons_low"] == 0x01


def test_a_dropped_frame_does_not_lose_a_release(linux_main, controller,
                                                  fake_evdev):
    """D2: a frame that could not be sent is not lost.

    Every frame is an authoritative snapshot, so when the send is skipped
    (the stream lock was busy) the *next* frame carries the same state —
    including the release of a button that was just let go of.
    """
    device = fake_evdev.new_device()
    device.path = "/dev/null"       # the device is "present"
    state = controller.ControllerState()

    sent: list[bytes] = []

    class _FlakyTCP:
        def __init__(self) -> None:
            self.calls = 0

        def send(self, data: bytes) -> bool:
            self.calls += 1
            if self.calls == 1:
                return False        # the frame is dropped (busy lock)
            sent.append(data)
            return True

    app = linux_main.BridgeApp()
    app._device = device
    app._state = state
    app._abs_info = {}
    app._tcp = _FlakyTCP()

    device.press(fake_evdev.ecodes.BTN_SOUTH)
    device.sync()
    call_with_timeout(app._poll, timeout=2.0, what="_poll (press)")
    assert app._tcp.calls == 1 and not sent, "the first frame was not dropped"

    device.release(fake_evdev.ecodes.BTN_SOUTH)
    device.sync()
    call_with_timeout(app._poll, timeout=2.0, what="_poll (release)")

    assert sent, "no frame was sent at all"
    assert not (sent[-1][10] & 0x01), \
        "the release never reached the wire — the dropped frame was not " \
        "self-healing"


# ---------------------------------------------------------------------------
# D3/D4 — SYN_DROPPED: the axes have to be restored
# ---------------------------------------------------------------------------

def _abs_info() -> dict:
    return dict(DEFAULT_ABS_INFO)


def _syn_dropped(controller, fake_evdev, device, state, abs_info):
    device.push_syn(fake_evdev.ecodes.SYN_DROPPED)
    device.sync()
    return call_with_timeout(
        lambda: controller.read_next_state(device, abs_info, state),
        what="read_next_state (SYN_DROPPED)")


def test_the_dpad_the_triggers_and_the_sticks_survive_a_syn_dropped(
        controller, fake_evdev):
    """D3/D4: the *complete* state is restored, not only the buttons."""
    device = fake_evdev.new_device()
    state = controller.ControllerState()
    abs_info = _abs_info()
    codes = fake_evdev.ecodes

    device.push_abs(codes.ABS_HAT0Y, -1)      # D-pad UP
    device.push_abs(codes.ABS_BRAKE, 1023)    # LT fully pulled
    device.push_abs(codes.ABS_GAS, 512)       # RT half pulled
    device.push_abs(codes.ABS_X, 32767)       # left stick hard right
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (input)")
    assert state.to_bytes()[12] == 0x01       # DPAD_UP
    assert state.lt == 255
    assert state.lthumb_x == 65535

    _syn_dropped(controller, fake_evdev, device, state, abs_info)

    assert state.to_bytes()[12] == 0x01, \
        "the D-pad direction was lost by the resync"
    assert state.lt == 255, \
        "the held trigger reads as released after the resync"
    assert state.rt == 128, \
        "the held trigger reads as released after the resync"
    assert state.lthumb_x == 65535, \
        "the stick position was lost by the resync"


def test_the_axes_survive_a_syn_dropped_on_a_real_python_evdev_device(
        controller, fake_evdev):
    """D3/D4: the real ``python-evdev`` device exposes ``absinfo()``.

    ``python-evdev``'s ``AbsInfo`` **has** a ``value`` attribute ("latest
    reported value for the axis") and ``device.absinfo(code)`` performs a
    fresh ``EVIOCGABS`` — that is where the current axis value comes
    from.  The library has **no** ``state`` property at all, so a resync
    that only looks at ``device.state`` restores nothing on a real
    device.
    """
    device = _RealEvdevDevice()
    state = controller.ControllerState()
    abs_info = _abs_info()
    codes = fake_evdev.ecodes

    device.push_abs(codes.ABS_HAT0Y, -1)
    device.push_abs(codes.ABS_BRAKE, 1023)
    device.push_abs(codes.ABS_X, 32767)
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (input)")

    _syn_dropped(controller, fake_evdev, device, state, abs_info)

    assert state.to_bytes()[12] == 0x01, \
        "the D-pad direction was lost — absinfo() was not used"
    assert state.lt == 255, \
        "the held trigger reads as released — absinfo() was not used"
    assert state.lthumb_x == 65535, \
        "the stick position was lost — absinfo() was not used"


class _RealEvdevDevice(FakeInputDevice):
    """A device that behaves like the real ``python-evdev``.

    * ``absinfo(code)`` returns the *current* value of the axis,
    * there is no ``state`` property (python-evdev has none).
    """

    @property
    def state(self):
        raise AttributeError("'InputDevice' object has no attribute 'state'")

    def absinfo(self, code: int):
        info = self._abs_info.get(code)
        if info is None:
            raise OSError(f"no such axis: {code}")
        return FakeAbsInfo(min=info.min, max=info.max, flat=info.flat,
                           fuzz=info.fuzz, resolution=info.resolution,
                           value=self._axis_values.get(code, 0))


def test_a_syn_dropped_without_any_axis_source_keeps_the_last_values(
        controller, fake_evdev):
    """D3/D4: no axis source at all must not crash and must not zero.

    A device that offers neither ``absinfo()`` nor ``state`` cannot tell
    the bridge what the axes are doing.  The best that can be done is to
    keep the last known values — zeroing them would report a held
    trigger as released.
    """
    device = _NoAxisSourceDevice()
    state = controller.ControllerState()
    abs_info = _abs_info()
    codes = fake_evdev.ecodes

    device.push_abs(codes.ABS_BRAKE, 1023)
    device.push_abs(codes.ABS_X, 32767)
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (input)")
    assert state.lt == 255
    assert state.lthumb_x == 65535

    _syn_dropped(controller, fake_evdev, device, state, abs_info)

    assert state.lt == 255, \
        "the held trigger was zeroed by the resync — it reads as released"
    assert state.lthumb_x == 65535, \
        "the stick was zeroed by the resync"


class _NoAxisSourceDevice(FakeInputDevice):
    """A device that cannot tell the bridge what its axes are doing."""

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("caps_expose_value", False)
        super().__init__(**kwargs)

    @property
    def state(self):
        raise AttributeError("'InputDevice' object has no attribute 'state'")


def test_a_resync_without_axis_info_does_not_crash(controller, fake_evdev):
    """D3/D4: the ``abs_info`` argument of the resync is optional.

    ``_resync_keys()`` declares ``abs_info`` as optional, so calling it
    without one must work — a missing axis description must fall back to
    a sane default instead of raising ``AttributeError`` (which would
    abort the whole read loop and leave the stale state in place).
    """
    device = fake_evdev.new_device()
    device.set_axis_value(fake_evdev.ecodes.ABS_X, 30000)
    state = controller.ControllerState()

    controller._resync_keys(state, device)     # no abs_info at all

# ---------------------------------------------------------------------------
# D5 — the peer flags belong to a connection
# ---------------------------------------------------------------------------

def test_the_peer_mode_is_reset_for_every_connection(receiver):
    """D5: a new connection starts with a clean slate.

    The first frame of the *new* connection decides again whether the
    peer sends checksums — the previous peer's mode must not be
    inherited.
    """
    states: list[dict] = []
    r = receiver.TCPReceiver("127.0.0.1", 0, on_state=states.append)

    with listening_receiver(r) as port:
        # 1 — a modern peer (with checksums).
        client = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        try:
            client.sendall(_frame(buttons_low=0x01))
            assert wait_until(lambda: r._peer_checksums is True, timeout=2.0), \
                "the checksummed peer was not recognised"
        finally:
            client.close()

        assert wait_until(lambda: r._conn is None, timeout=2.0), \
            "the first connection was not closed"

        # 2 — an old peer (without checksums) connects.
        client2 = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        try:
            client2.sendall(_legacy_frame(buttons_low=0x02))
            assert wait_until(lambda: len(states) >= 2, timeout=2.0), \
                "the second peer's frame was not delivered — the mode of " \
                "the previous peer was inherited"
        finally:
            client2.close()

    assert r._peer_checksums is False, \
        "the peer mode was not reset for the new connection"


# ---------------------------------------------------------------------------
# D6 — the monitor loop
# ---------------------------------------------------------------------------

def _windows_app(windows_main, tmp_path, keepalive_timeout: float = 0.2):
    config_file = tmp_path / "config.ini"
    config_file.write_text(
        "[network]\nkeepalive_timeout_s = %s\n" % keepalive_timeout,
        encoding="utf-8")
    config = windows_main.Config(str(config_file))
    app = windows_main.BridgeApp(config)
    app._running = True
    return app


def test_the_monitor_loop_stops_as_soon_as_the_app_stops(windows_main,
                                                         tmp_path, receiver):
    """D6: the monitor loop must not run another iteration after the stop.

    ``_monitor_loop`` sleeps for half a second; the flag has to be
    re-checked *after* the sleep, otherwise one more iteration runs (and
    one more "the peer is gone" / "release all buttons" decision is
    taken) after the application was told to shut down.
    """
    app = _windows_app(windows_main, tmp_path)
    app._receiver = receiver.TCPReceiver("127.0.0.1", 0,
                                         on_state=app._on_state)

    updates: list[dict] = []
    app._tray.update = lambda **kwargs: updates.append(kwargs)

    worker = threading.Thread(target=app._monitor_loop, daemon=True)
    worker.start()
    try:
        assert wait_until(lambda: len(updates) >= 2, timeout=3.0), \
            "the monitor loop did not run"
        app._running = False
        time.sleep(0.05)
        seen = len(updates)
        # Let the sleeping iteration wake up (the loop sleeps 0.5 s).
        time.sleep(1.0)
        assert len(updates) == seen, \
            "the monitor loop kept running after the application stopped"
        worker.join(timeout=2)
        assert not worker.is_alive(), "the monitor loop did not terminate"
    finally:
        app._running = False
        worker.join(timeout=2)


# ---------------------------------------------------------------------------
# D7 — the rumble probe must not wait
# ---------------------------------------------------------------------------

def test_the_rumble_probe_never_waits_for_the_socket(receiver, monkeypatch):
    """D7: the rumble write must not block the ViGEm notification thread."""
    calls: list = []

    class _Select:
        @staticmethod
        def select(r, w, x, timeout):
            calls.append(timeout)
            return ([], list(w), [])

    monkeypatch.setattr(receiver, "select", _Select)

    r = receiver.TCPReceiver("127.0.0.1", 0, on_state=lambda s: None)
    sock = _RecordingSocket()
    r._conn = sock

    assert r.send_state(_frame(buttons_low=0x01)) is True
    assert calls == [0], \
        f"the rumble probe waited {calls} — the notification thread stalls"
    assert receiver._SEND_TIMEOUT_S == 0.0, \
        "the rumble send timeout is not 0 s"


# ---------------------------------------------------------------------------
# D8 — the per-frame logs
# ---------------------------------------------------------------------------

def test_the_tx_button_frame_log_is_debug_only(network, caplog):
    """D8: the per-frame log must not flood the log file at INFO."""
    streamer = network.TCPStreamer("127.0.0.1", 1)
    sock = _RecordingSocket()
    streamer._sock = sock

    with caplog.at_level(logging.INFO, logger="network"):
        streamer.send(_frame(buttons_low=0x01))
    assert not [r for r in caplog.records
                if "TX button frame" in r.getMessage()], \
        "the per-frame log is emitted at INFO"

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="network"):
        streamer.send(_frame(buttons_low=0x01))
    assert [r for r in caplog.records
            if "TX button frame" in r.getMessage()], \
        "the per-frame log is not emitted at DEBUG"


def test_the_rx_button_frame_log_is_debug_only(receiver, caplog):
    """D8: the same on the Windows side."""
    with caplog.at_level(logging.INFO, logger="receiver"):
        _serve(receiver, _frame(buttons_low=0x01))
    assert not [r for r in caplog.records
                if "RX button frame" in r.getMessage()], \
        "the per-frame log is emitted at INFO"

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="receiver"):
        _serve(receiver, _frame(buttons_low=0x01))
    assert [r for r in caplog.records
            if "RX button frame" in r.getMessage()], \
        "the per-frame log is not emitted at DEBUG"


def test_the_button_press_log_is_debug_only(controller, fake_evdev, caplog):
    """D8: the per-button log on the Linux side."""
    device = fake_evdev.new_device()
    state = controller.ControllerState()
    device.press(fake_evdev.ecodes.BTN_SOUTH)
    device.sync()

    with caplog.at_level(logging.INFO, logger="controller"):
        call_with_timeout(
            lambda: controller.read_next_state(device, _abs_info(), state),
            what="read_next_state")
    assert not [r for r in caplog.records
                if "BTN " in r.getMessage() and "pressed" in r.getMessage()], \
        "the per-button log is emitted at INFO"


# ---------------------------------------------------------------------------
# D9 — no dead state in the emitter
# ---------------------------------------------------------------------------

def test_the_emitter_keeps_no_unused_state_timestamp(emitter):
    """D9: the unused ``_last_state_at`` must be gone from the emitter."""
    app = emitter.XInputEmitter(slot=0)
    assert not hasattr(app, "_last_state_at"), \
        "the emitter still keeps an unused state timestamp"


# ---------------------------------------------------------------------------
# D10 — from_bytes() verifies the checksum
# ---------------------------------------------------------------------------

def test_from_bytes_round_trips_a_valid_frame(controller):
    """D10: a valid frame is decoded."""
    state = controller.ControllerState(buttons_low=0x03, buttons_high=0x01,
                                       lt=200, rt=50, battery=42,
                                       charging=True,
                                       controller_mac="aa:bb:cc:dd:ee:ff",
                                       controller_name="Xbox Wireless Co")
    state._hat_x = 1
    frame = state.to_bytes()

    parsed = controller.ControllerState.from_bytes(frame)
    assert parsed is not None, "a valid frame was rejected"
    assert parsed.buttons_low == 0x03
    assert parsed.buttons_high == 0x01
    assert parsed.lt == 200
    assert parsed.rt == 50
    assert parsed.battery == 42
    assert parsed.charging is True
    assert parsed.controller_mac == "aa:bb:cc:dd:ee:ff"
    assert parsed.controller_name == "Xbox Wireless Co"
    assert parsed.to_bytes() == frame


def test_from_bytes_rejects_a_corrupted_frame(controller):
    """D10: a frame with a broken checksum is never decoded."""
    frame = bytearray(controller.ControllerState(buttons_low=0x01).to_bytes())
    frame[OFF_BUTTONS_LOW] ^= 0xFF
    assert controller.ControllerState.from_bytes(bytes(frame)) is None, \
        "a corrupted frame was decoded as controller input"


def test_from_bytes_accepts_a_legacy_frame_without_a_checksum(controller):
    """D10: a frame without the checksum flag stays decodable."""
    frame = bytearray(controller.ControllerState(buttons_low=0x01).to_bytes())
    frame[OFF_FLAGS] &= ~FLAG_CHECKSUM
    frame[OFF_CHECKSUM] = 0

    parsed = controller.ControllerState.from_bytes(bytes(frame))
    assert parsed is not None, "a legacy frame was rejected"
    assert parsed.buttons_low == 0x01


def test_from_bytes_rejects_a_ping_and_a_short_frame(controller):
    """D10: the keepalive and a truncated frame are not decoded."""
    assert controller.ControllerState.from_bytes(PING_PAYLOAD) is None
    assert controller.ControllerState.from_bytes(b"\x00" * 17) is None
    assert controller.ControllerState.from_bytes(b"") is None


def test_from_bytes_does_not_drop_a_frame_that_starts_with_0xff(controller):
    """D10: a valid frame must never be mistaken for a keepalive.

    ``lthumb_x = 65535`` (the stick hard right) makes the *first* byte of
    the frame 0xFF.  The Windows parser compares the whole 54 bytes with
    the PING pattern, the Linux parser only looks at the first byte — so
    a perfectly valid frame is thrown away.
    """
    frame = controller.ControllerState(lthumb_x=65535).to_bytes()
    assert frame[0] == 0xFF

    parsed = controller.ControllerState.from_bytes(frame)
    assert parsed is not None, \
        "a valid frame was dropped because it starts with 0xFF"
    assert parsed.lthumb_x == 65535


def test_a_misaligned_window_is_never_parsed_as_a_v1_frame(receiver,
                                                            controller):
    """D1: a 54-byte window with a wrong proto byte must not be decoded.

    ``StateParser.parse()`` falls back to the legacy v1 parser for every
    frame that is not v2 — and a *misaligned* window is exactly such a
    frame.  The caller validates the frame before it parses it, so the
    bug is not reachable today, but the parser itself is unsafe: any
    future caller that forgets the validation decodes 14 bytes of garbage
    as button presses.
    """
    frame = bytearray(controller.ControllerState(buttons_low=0xFF).to_bytes())
    frame[13] = 0x03           # wrong proto byte -> "not a v2 frame"

    assert not receiver.is_valid_frame(bytes(frame))
    parsed = receiver.StateParser.parse(bytes(frame))
    assert parsed is None, \
        "a misaligned window was decoded as a v1 state — ghost buttons!"


# ---------------------------------------------------------------------------
# The installer
# ---------------------------------------------------------------------------

def _compose_from_install_script() -> str:
    src = (ROOT / "linux" / "install.sh").read_text(encoding="utf-8")
    match = re.search(
        r"cat > \"\$INSTALL_DIR/docker-compose\.yml\" <<YML\n(.*?)\nYML\n",
        src, re.S)
    assert match, "the installer does not write a docker-compose.yml any more"
    return match.group(1)


def _sequence_indent_problems(text: str) -> list[str]:
    """Find block sequences whose items are not indented consistently.

    ``docker compose`` refuses to start with such a file, so the
    installer would produce a container that never comes up.
    """
    problems: list[str] = []
    seq_indent: int | None = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if raw.lstrip().startswith("- "):
            if seq_indent is None:
                seq_indent = indent
            elif indent != seq_indent:
                problems.append(
                    f"line {lineno}: the sequence item is indented {indent} "
                    f"spaces, the sequence uses {seq_indent}")
        else:
            seq_indent = None
    return problems


def test_the_generated_compose_file_has_a_consistent_indentation():
    """The ``docker-compose.yml`` of the installer must be valid YAML.

    Every item of a block sequence has to be indented the same way —
    otherwise ``docker compose up`` fails and the Linux bridge never
    starts.
    """
    text = _compose_from_install_script()
    problems = _sequence_indent_problems(text)
    assert not problems, \
        "the generated docker-compose.yml is not valid YAML: " + \
        "; ".join(problems)


def test_the_generated_compose_file_lists_the_env_file_as_file_names():
    """``env_file`` must be a list of file names.

    ``environment:`` is a *sibling* of ``env_file`` — when it is nested
    inside the ``env_file`` list the file is a mapping and docker compose
    rejects it.
    """
    text = _compose_from_install_script()
    lines = text.splitlines()

    env_file_at = None
    for lineno, raw in enumerate(lines):
        if raw.strip() == "env_file:":
            env_file_at = lineno
            break
    assert env_file_at is not None, "the compose file has no env_file"

    base_indent = len(lines[env_file_at]) - len(lines[env_file_at].lstrip())
    block: list[str] = []
    for raw in lines[env_file_at + 1:]:
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent <= base_indent:
            break
        block.append(raw.strip())

    assert block, "env_file has no value"
    for entry in block:
        assert entry.startswith("- ") and ": " not in entry, \
            f"'env_file' must be a list of file names, found {entry!r}"


# ---------------------------------------------------------------------------
# Fixed defects (these tests document the bugs that were fixed)
# ---------------------------------------------------------------------------

def test_a_corrupt_first_window_does_not_kill_the_input_pipeline(receiver):
    """A garbage first window must not decide how the whole stream is read.

    The receiver decides "checksums or no checksums" from the flags byte
    of the *first* window — before that window has been validated.  When
    the first window is garbage whose flags byte happens to have bit 3
    set, the peer is classified as a "checksummed" peer.  A legacy peer
    then never matches any more: every frame is rejected and discarded
    byte by byte, the connection is never dropped and the input pipeline
    stays dead for good.
    """
    garbage = _corrupt(_frame(buttons_low=0xFF), 13)   # bit 3 is set here
    legacy = _legacy_frame(buttons_low=0x02)

    states: list[dict] = []
    r = receiver.TCPReceiver("127.0.0.1", 0, on_state=states.append)

    with listening_receiver(r) as port:
        client = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        try:
            client.sendall(garbage + legacy)
            # Give the receiver time to chew through the stream.
            time.sleep(0.3)
            delivered = list(states)
            dropped = r._conn is None
        finally:
            client.close()

    assert delivered or dropped, \
        "the receiver silently swallowed the whole stream — neither a " \
        "state was delivered nor was the connection dropped"


def test_a_peer_that_stops_announcing_the_checksum_is_disconnected(receiver):
    """A peer that violates the announced mode must be disconnected.

    The receiver requires the checksum flag on *every* frame of a
    "checksummed" connection, but when a frame arrives without the flag
    it just re-syncs — and every following frame is rejected as well, so
    the connection stays open and silently delivers nothing.
    """
    good = _frame(buttons_low=0x02)
    without_flag = _frame(buttons_low=0x04, flags=FLAG_STREAMING,
                          checksum=False)

    states: list[dict] = []
    disconnects: list[str] = []
    r = receiver.TCPReceiver("127.0.0.1", 0, on_state=states.append,
                             on_disconnect=disconnects.append)

    with listening_receiver(r) as port:
        client = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        try:
            client.sendall(good + without_flag)
            time.sleep(0.3)
            delivered = list(states)
            dropped = bool(disconnects)
        finally:
            client.close()

    assert dropped, \
        "the broken peer was not disconnected — the stream is discarded " \
        "silently, forever"
    assert len(delivered) <= 1
