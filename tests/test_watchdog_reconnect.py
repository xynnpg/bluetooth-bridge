"""The Windows watchdog and the reconnect behaviour.

Contract items covered here:

* **E** — the state watchdog releases every button when no *state* frame
  arrives for ``network.keepalive_timeout_s`` seconds, **even while the
  PING keepalives keep flowing**.  The release has to happen *while* the
  peer is still alive, otherwise the "release on disconnect" path is never
  exercised.
* **E** — the watchdog is only armed for peers that announce that they
  stream the state continuously (flags bit 2).  An older Linux side only
  reports when the controller is used; watching it would release a button
  that is still physically held.
* **C1** — after the TCP connection drops and comes back the virtual pad's
  held set matches the physical controller's held set: no stale button
  survives the reconnect.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from conftest import listening_receiver, wait_until
from fakes import BTN_LOW_BITS, PROTO_VERSION, make_state

#: flags bit 2 — "this sender keeps streaming the state".
FLAG_STREAMING = 0x04


def _frame(buttons_low: int = 0, flags: int = FLAG_STREAMING) -> bytes:
    """Build a valid 54-byte v2 state frame."""
    pkt = struct.pack(
        "<HHHHBBBBBBBBBB",
        32768, 32768, 32768, 32768,   # sticks
        0, 0,                          # lt, rt
        buttons_low & 0xFF,
        0,                             # buttons_high
        0,                             # dpad
        PROTO_VERSION,
        0xFF,                          # battery unknown
        flags,
        0, 0,                          # rumble
    )
    body = pkt + b"\x00" * 6 + b"\x00" * 16 + b"\x00" * 12
    body += bytes((sum(body) & 0xFF,))   # checksum
    body += b"\x00"                     # reserved
    return body


def _windows_app(windows_main, tmp_path, keepalive_timeout: float = 0.2):
    """A Windows BridgeApp with a short keepalive timeout."""
    config_file = tmp_path / "config.ini"
    config_file.write_text(
        "[network]\nkeepalive_timeout_s = %s\n" % keepalive_timeout,
        encoding="utf-8")
    config = windows_main.Config(str(config_file))
    app = windows_main.BridgeApp(config)
    app._running = True
    return app


def _receiver_for(app, receiver):
    """Give *app* a receiver that looks like the one it would create."""
    app._receiver = receiver.TCPReceiver(
        "127.0.0.1", 0,
        on_state=app._on_state,
        on_connect=app._on_connect,
        on_ping=app._on_ping,
        on_disconnect=app._on_disconnect,
    )
    return app._receiver


# ---------------------------------------------------------------------------
# E — the watchdog
# ---------------------------------------------------------------------------

def test_watchdog_releases_buttons_while_the_pings_are_still_flowing(
        windows_main, tmp_path, receiver):
    """E: the buttons are released while the peer is still sending pings.

    The old watchdog refreshed the same timestamp for pings and for state
    frames, so a peer that only sent keepalives kept the last buttons
    latched forever.  The release must happen *during* the ping flow —
    not only after the peer goes away.
    """
    app = _windows_app(windows_main, tmp_path, keepalive_timeout=0.2)
    _receiver_for(app, receiver)
    app._receiver._peer_streams = True     # the peer announces the heartbeat
    pad = app._emitter._pad

    app._on_state(make_state(buttons_low=1 << BTN_LOW_BITS["A"]))
    assert pad.held, "the test setup did not press the button"

    stop = threading.Event()
    ping_times: list[float] = []

    def pinger() -> None:
        while not stop.is_set():
            app._on_ping()
            ping_times.append(time.monotonic())
            time.sleep(0.01)

    worker = threading.Thread(target=app._monitor_loop, daemon=True)
    pinger_thread = threading.Thread(target=pinger, daemon=True)
    worker.start()
    pinger_thread.start()
    try:
        released = wait_until(lambda: not pad.held, timeout=3.0)
        # Snapshot the situation *while the pings are still flowing* — the
        # monitor loop performs one last iteration after the shutdown flag
        # is cleared, and that iteration must not be part of the assertion.
        reachable_at_release = app._pc_reachable
        pings_at_release = len(ping_times)
        # The pings must keep flowing *after* the release as well.
        kept_flowing = wait_until(lambda: len(ping_times) > pings_at_release,
                                  timeout=1.0)
        still_released = not pad.held
    finally:
        stop.set()
        pinger_thread.join(timeout=2)
        app._running = False
        worker.join(timeout=2)

    assert released, \
        "the button is still held although no state frame arrived"
    assert kept_flowing, "the pings stopped instead of the buttons being released"
    assert still_released, "the button was pressed again after the release"
    assert reachable_at_release is True, \
        "the connection was declared dead — the release did not happen " \
        "while the peer was still alive"


def test_the_watchdog_is_not_armed_for_a_peer_that_does_not_stream(
        windows_main, tmp_path, receiver):
    """E: an old peer (no streaming flag) must not be watched.

    An older Linux side only sends a frame when the controller is used.
    A button that is held for a long time produces no frame at all, so
    the watchdog would release a button that is still physically held.
    """
    app = _windows_app(windows_main, tmp_path, keepalive_timeout=0.2)
    _receiver_for(app, receiver)
    assert app._receiver._peer_streams is False
    pad = app._emitter._pad

    app._on_state(make_state(buttons_low=1 << BTN_LOW_BITS["A"]))
    assert pad.held, "the test setup did not press the button"

    worker = threading.Thread(target=app._monitor_loop, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            app._on_ping()
            time.sleep(0.02)
        # Check *before* the monitor loop is stopped: it performs one last
        # iteration after the shutdown flag is cleared and that iteration
        # sees a silent peer.
        still_held = bool(pad.held)
    finally:
        app._running = False
        worker.join(timeout=2)

    assert still_held, \
        "the watchdog released a button of a peer that does not stream the " \
        "state — the button is still held physically"


# ---------------------------------------------------------------------------
# C1 — reconnect
# ---------------------------------------------------------------------------

def test_reconnect_does_not_leave_a_stale_button_held(windows_main, tmp_path,
                                                      receiver):
    """C1: after a reconnect the virtual pad mirrors the physical pad.

    No stale button may survive the reconnect, and a button that is still
    held must be re-asserted when the link comes back.
    """
    app = _windows_app(windows_main, tmp_path)
    _receiver_for(app, receiver)
    pad = app._emitter._pad

    with listening_receiver(app._receiver) as port:
        client = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        try:
            client.sendall(_frame(buttons_low=1 << BTN_LOW_BITS["A"]))
            assert wait_until(lambda: pad.held_names() == {"A"}, timeout=2.0), \
                "the press did not reach the virtual pad"
        finally:
            client.close()

        # The link drops — nothing may stay held on the virtual pad.
        assert wait_until(lambda: not pad.held, timeout=2.0), \
            "the button stayed held on the virtual pad after the " \
            "connection dropped"

        # ... and comes back with the button still held.
        client2 = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        try:
            client2.sendall(_frame(buttons_low=1 << BTN_LOW_BITS["A"]))
            assert wait_until(lambda: pad.held_names() == {"A"}, timeout=2.0), \
                "the button did not come back after the reconnect"
        finally:
            client2.close()


def test_the_streaming_flag_of_a_previous_peer_is_reset(receiver):
    """The "peer streams" flag must not survive a reconnect.

    The watchdog is only armed for peers that announce the streaming
    flag.  If the flag of a previous (newer) peer is still set when an
    older peer connects, the watchdog releases buttons that are still
    physically held.
    """
    states: list[dict] = []
    r = receiver.TCPReceiver("127.0.0.1", 0, on_state=states.append)

    with listening_receiver(r) as port:
        client = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        try:
            client.sendall(_frame(buttons_low=0x01, flags=FLAG_STREAMING))
            assert wait_until(lambda: r._peer_streams is True, timeout=2.0), \
                "the streaming flag was not picked up"
        finally:
            client.close()

        generation = r._conn_generation
        client2 = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        try:
            assert wait_until(lambda: r._conn_generation > generation,
                              timeout=2.0), "the second peer did not connect"
            # An older peer: it does not announce the streaming flag.
            client2.sendall(_frame(buttons_low=0x01, flags=0x00))
            assert wait_until(lambda: len(states) >= 2, timeout=2.0), \
                "the second peer's frame was not processed"
        finally:
            client2.close()

        assert r._peer_streams is False, \
            "the streaming flag of the previous peer is still set — the " \
            "watchdog would be armed for a peer that does not stream"
