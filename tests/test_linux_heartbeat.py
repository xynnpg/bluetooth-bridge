"""The Linux reader: idle heartbeat, batch draining and state persistence.

Contract items covered here:

* **A** — ``read_next_state()`` drains *all* pending evdev events per
  ``select()`` round and hands the current state back on a 50 ms idle
  heartbeat, so the bridge keeps streaming authoritative frames even when
  the controller does nothing.
* **A/C2** — the Linux side keeps the authoritative state across a failed
  send: a release that could not be delivered must not be lost.

The bug class this suite guards against: the reader used to throw away the
rest of an event batch (including button *releases*), so a button that was
let go of stayed latched in the in-memory state and in every following
packet — "I let go of the button and it still holds it".
"""

from __future__ import annotations

import time

import pytest

from conftest import call_with_timeout
from fakes import (
    BTN_LOW_BITS,
    DEFAULT_ABS_INFO,
    OFF_BUTTONS_LOW,
    OFF_PROTO,
    PAYLOAD_SIZE,
)


# ---------------------------------------------------------------------------
# A — the idle heartbeat
# ---------------------------------------------------------------------------

def test_idle_heartbeat_produces_at_least_ten_frames_per_second(
        controller, fake_evdev, receiver):
    """A: an idle controller must still produce >= 10 frames per second.

    Every frame is a complete snapshot of the controller, so a frame that
    was lost in transit is corrected by the next one instead of latching a
    button forever.
    """
    device = fake_evdev.new_device()
    state = controller.ControllerState()

    frames = []
    started = time.monotonic()
    deadline = started + 1.0
    while time.monotonic() < deadline:
        frames.append(controller.read_next_state(device, {}, state).to_bytes())
    elapsed = time.monotonic() - started

    rate = len(frames) / elapsed
    assert rate >= 10.0, \
        f"the idle heartbeat only produced {rate:.1f} frames/s ({len(frames)} " \
        f"frames in {elapsed:.2f}s) — a lost frame could never be healed"

    for frame in frames:
        assert len(frame) == PAYLOAD_SIZE, "the heartbeat frame is truncated"
        assert frame[OFF_PROTO] == 0x02
        assert receiver.is_valid_frame(frame), \
            "the heartbeat frame is not a valid state frame"


def test_the_heartbeat_returns_the_current_state_unchanged(controller,
                                                            fake_evdev):
    """A: the heartbeat hands back the state that is currently held."""
    device = fake_evdev.new_device()
    state = controller.ControllerState()
    codes = fake_evdev.ecodes

    device.press(codes.BTN_SOUTH)
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, {}, state),
                      what="read_next_state (press)")

    # Nothing happens any more — the heartbeat must keep the button held.
    for _ in range(3):
        again = controller.read_next_state(device, {}, state)
        assert again is state
        assert again.buttons_low & (1 << BTN_LOW_BITS["A"]), \
            "the heartbeat lost a button that is still held"


def test_the_bridge_sends_a_frame_per_heartbeat(linux_main, controller,
                                                 fake_evdev):
    """A: the bridge keeps sending frames while the controller is idle."""
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
    app._abs_info = dict(DEFAULT_ABS_INFO)
    app._tcp = FakeTCP()

    for _ in range(3):
        call_with_timeout(app._poll, timeout=1.0, what="BridgeApp._poll")

    assert len(sent) >= 3, "the bridge stopped sending idle frames"
    for frame in sent:
        assert len(frame) == PAYLOAD_SIZE


# ---------------------------------------------------------------------------
# A — every pending event of a batch must be processed
# ---------------------------------------------------------------------------

def test_a_press_and_a_release_in_one_batch_end_up_released(
        controller, fake_evdev):
    """A: a batch is drained completely — a release must never be dropped.

    The press and the release of the same button arrive in one event
    batch (the kernel batches them).  If the reader stops after the first
    event the release is lost and the button stays held forever.
    """
    device = fake_evdev.new_device()
    state = controller.ControllerState()
    codes = fake_evdev.ecodes

    device.push_key(codes.BTN_SOUTH, 1)     # A pressed
    device.push_key(codes.BTN_SOUTH, 0)     # A released again
    device.sync()

    call_with_timeout(lambda: controller.read_next_state(device, {}, state),
                      what="read_next_state (press+release batch)")

    assert not (state.buttons_low & (1 << BTN_LOW_BITS["A"])), \
        "the release in the same batch was lost — the button stays latched"
    assert state.to_bytes()[OFF_BUTTONS_LOW] & (1 << BTN_LOW_BITS["A"]) == 0


def test_several_buttons_in_one_batch_are_all_applied(controller, fake_evdev):
    """A: every button of a batch reaches the state."""
    device = fake_evdev.new_device()
    state = controller.ControllerState()
    codes = fake_evdev.ecodes

    device.push_key(codes.BTN_SOUTH, 1)     # A
    device.push_key(codes.BTN_EAST, 1)      # B
    device.push_key(codes.BTN_SOUTH, 0)     # A released again
    device.push_key(codes.BTN_NORTH, 1)     # X
    device.sync()

    call_with_timeout(lambda: controller.read_next_state(device, {}, state),
                      what="read_next_state (multi button batch)")

    assert state.buttons_low & (1 << BTN_LOW_BITS["B"])
    assert state.buttons_low & (1 << BTN_LOW_BITS["X"])
    assert not (state.buttons_low & (1 << BTN_LOW_BITS["A"]))


# ---------------------------------------------------------------------------
# C2 — the authoritative state survives a failed send
# ---------------------------------------------------------------------------

def test_the_state_survives_a_failed_send(linux_main, controller, fake_evdev):
    """A failed send must not lose a release.

    Every frame is a snapshot, so the next frame carries the release even
    when the previous one could not be delivered.
    """
    device = fake_evdev.new_device()
    state = controller.ControllerState()
    codes = fake_evdev.ecodes
    sent: list[bytes] = []

    class FailingTCP:
        def send(self, data: bytes) -> bool:
            sent.append(data)
            return False        # the peer is gone

    app = linux_main.BridgeApp()
    app._device = device
    app._state = state
    app._abs_info = dict(DEFAULT_ABS_INFO)
    app._tcp = FailingTCP()

    device.press(codes.BTN_SOUTH)
    device.sync()
    call_with_timeout(app._poll, timeout=2.0, what="BridgeApp._poll (press)")
    assert sent, "nothing was sent at all"
    assert sent[-1][OFF_BUTTONS_LOW] & (1 << BTN_LOW_BITS["A"]), \
        "the press did not reach the wire"

    # The user lets go of the button — the frame cannot be delivered.
    device.release(codes.BTN_SOUTH)
    device.sync()
    call_with_timeout(app._poll, timeout=2.0, what="BridgeApp._poll (release)")

    assert sent[-1][OFF_BUTTONS_LOW] & (1 << BTN_LOW_BITS["A"]) == 0, \
        "the release was lost after a failed send — the button stays held"


def test_release_all_clears_the_whole_state(controller):
    """``release_all()`` must let go of every button and neutralise the axes."""
    state = controller.ControllerState(
        lthumb_x=0, lthumb_y=65535, rthumb_x=65535, rthumb_y=0,
        lt=255, rt=200, buttons_low=0xFF, buttons_high=0x07,
    )
    state._hat_x = -1
    state._hat_y = -1

    state.release_all()

    assert state.buttons_low == 0
    assert state.buttons_high == 0
    assert state.lt == 0
    assert state.rt == 0
    assert state.lthumb_x == 32768
    assert state.lthumb_y == 32768
    assert state.rthumb_x == 32768
    assert state.rthumb_y == 32768
    assert state._hat_x == 0
    assert state._hat_y == 0


def test_a_vanished_controller_releases_the_buttons(linux_main, controller,
                                                    fake_evdev, tmp_path):
    """A controller that disappears must release everything it held.

    When the controller powers off the kernel removes the device node and
    no further events arrive.  The last known state (with the buttons
    that were held) would then be reported forever.
    """
    # The node exists at first and disappears later — that is exactly what
    # the kernel does when the controller powers off.
    node = tmp_path / "event3"
    node.write_text("", encoding="utf-8")
    device = fake_evdev.new_device(path=str(node))
    state = controller.ControllerState()
    codes = fake_evdev.ecodes
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

    # The user holds A when the controller disappears.
    device.press(codes.BTN_SOUTH)
    device.sync()
    call_with_timeout(app._poll, timeout=2.0, what="BridgeApp._poll (press)")
    assert state.buttons_low & (1 << BTN_LOW_BITS["A"])

    # The controller is unplugged: the kernel removes the node.
    node.unlink()

    # The next poll notices that the node is gone.
    call_with_timeout(app._poll, timeout=2.0, what="BridgeApp._poll (gone)")

    assert not (state.buttons_low & (1 << BTN_LOW_BITS["A"])), \
        "the button is still held after the controller disappeared"
    assert sent[-1][OFF_BUTTONS_LOW] & (1 << BTN_LOW_BITS["A"]) == 0, \
        "the release did not reach the wire"
