"""Linux input pipeline: evdev -> ControllerState -> 54-byte wire packet.

Contract items covered here:

* **C1** — every physical button press ends up as the matching bit in the
  wire packet, and the matching release clears it again.
* **C5** — after ``EV_SYN / SYN_DROPPED`` the digital button state is
  re-read from the device instead of keeping the stale in-memory value.
* **C9** — the D-pad works both as hat axes (``ABS_HAT0X/Y``) and as
  ``BTN_DPAD_*`` key events, including diagonals.
* **C10** — triggers return to 0 and sticks return to centre (32768 in the
  0..65535 wire format) when they are released.

The bug this suite reproduces: "when I let go of a button on the physical
controller it still holds it".  On the Linux side the same class of bug shows
up when the bridge keeps a *stale* digital button state — for example after
the kernel dropped events (``SYN_DROPPED``) — because the stale bit is then
written into every following packet, so the Windows side never learns that
the button was released.
"""

from __future__ import annotations

import pytest

from conftest import call_with_timeout
from fakes import (
    BTN_HIGH_BITS,
    BTN_LOW_BITS,
    DEFAULT_ABS_INFO,
    DPAD_LEFT,
    DPAD_UP,
    OFF_BUTTONS_HIGH,
    OFF_BUTTONS_LOW,
    OFF_DPAD,
    OFF_LT,
    OFF_PROTO,
    OFF_RT,
    PAYLOAD_SIZE,
    PROTO_VERSION,
)

# (evdev code name, wire field, bit position)
BUTTONS = [
    ("BTN_SOUTH", "buttons_low", BTN_LOW_BITS["A"]),
    ("BTN_EAST", "buttons_low", BTN_LOW_BITS["B"]),
    ("BTN_NORTH", "buttons_low", BTN_LOW_BITS["X"]),
    ("BTN_WEST", "buttons_low", BTN_LOW_BITS["Y"]),
    ("BTN_TL", "buttons_low", BTN_LOW_BITS["LB"]),
    ("BTN_TR", "buttons_low", BTN_LOW_BITS["RB"]),
    ("BTN_SELECT", "buttons_low", BTN_LOW_BITS["Back"]),
    ("BTN_START", "buttons_low", BTN_LOW_BITS["Start"]),
    ("BTN_THUMBL", "buttons_high", BTN_HIGH_BITS["L3"]),
    ("BTN_THUMBR", "buttons_high", BTN_HIGH_BITS["R3"]),
    ("BTN_MODE", "buttons_high", BTN_HIGH_BITS["Guide"]),
]

OFFSETS = {"buttons_low": OFF_BUTTONS_LOW, "buttons_high": OFF_BUTTONS_HIGH}


def _make_device(fake_evdev):
    """A fresh fake Xbox controller."""
    return fake_evdev.new_device()


def _abs_info_for(fake_evdev, device):
    """The ``abs_info`` mapping the controller expects (fake device ranges)."""
    return dict(DEFAULT_ABS_INFO)


# ---------------------------------------------------------------------------
# C1 — physical button fidelity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code_name,field,bit", BUTTONS,
                         ids=[b[0] for b in BUTTONS])
def test_button_press_sets_the_wire_bit(controller, fake_evdev,
                                        code_name, field, bit):
    """C1: pressing a physical button must set its bit in the packet.

    Bug: if the bridge never sees (or never forwards) the EV_KEY event the
    button bit stays 0 and the game never sees the press.
    """
    device = _make_device(fake_evdev)
    state = controller.ControllerState()
    code = getattr(fake_evdev.ecodes, code_name)

    device.press(code)
    device.sync()
    result = call_with_timeout(
        lambda: controller.read_next_state(device, _abs_info_for(fake_evdev, device), state),
        what="read_next_state")

    assert getattr(result, field) & (1 << bit), f"{code_name} press was lost"
    packet = result.to_bytes()
    assert packet[OFFSETS[field]] & (1 << bit), f"{code_name} missing from the packet"


@pytest.mark.parametrize("code_name,field,bit", BUTTONS,
                         ids=[b[0] for b in BUTTONS])
def test_button_release_clears_the_wire_bit(controller, fake_evdev,
                                            code_name, field, bit):
    """C1: releasing a physical button must clear its bit in the packet.

    Bug: "when I let go of a button it still holds it" — the release event
    either never reaches the state or the state is not written to the wire.
    """
    device = _make_device(fake_evdev)
    state = controller.ControllerState()
    abs_info = _abs_info_for(fake_evdev, device)
    code = getattr(fake_evdev.ecodes, code_name)

    device.press(code)
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (press)")
    assert getattr(state, field) & (1 << bit)

    device.release(code)
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (release)")

    assert not (getattr(state, field) & (1 << bit)), \
        f"{code_name} release was lost — the button stays latched"
    packet = state.to_bytes()
    assert not (packet[OFFSETS[field]] & (1 << bit)), \
        f"{code_name} release did not reach the wire"


def test_packet_is_a_whole_54_byte_v2_frame(controller, fake_evdev):
    """The wire frame must always be exactly 54 bytes with proto 0x02.

    A short frame would desynchronise the receiver (ghost input).
    """
    device = _make_device(fake_evdev)
    state = controller.ControllerState()
    packet = state.to_bytes()
    assert len(packet) == PAYLOAD_SIZE
    assert packet[OFF_PROTO] == PROTO_VERSION


# ---------------------------------------------------------------------------
# C9 — D-pad
# ---------------------------------------------------------------------------

def test_dpad_hat_axes_press_and_release(controller, fake_evdev):
    """C9: the D-pad works as hat axes and releasing clears the direction.

    Bug: a hat axis that keeps its old value leaves the D-pad stuck in one
    direction, which in a game looks like a permanently held direction.
    """
    device = _make_device(fake_evdev)
    state = controller.ControllerState()
    abs_info = _abs_info_for(fake_evdev, device)
    codes = fake_evdev.ecodes

    device.push_abs(codes.ABS_HAT0Y, -1)   # UP
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (hat up)")
    assert state.to_bytes()[OFF_DPAD] == DPAD_UP

    device.push_abs(codes.ABS_HAT0Y, 0)    # released
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (hat release)")
    assert state.to_bytes()[OFF_DPAD] == 0, "hat release did not clear the D-pad"


def test_dpad_key_events_press_and_release(controller, fake_evdev):
    """C9: the D-pad also works when the kernel reports it as key events."""
    device = _make_device(fake_evdev)
    state = controller.ControllerState()
    abs_info = _abs_info_for(fake_evdev, device)
    codes = fake_evdev.ecodes

    device.press(codes.BTN_DPAD_UP)
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (dpad up)")
    assert state.to_bytes()[OFF_DPAD] == DPAD_UP

    device.release(codes.BTN_DPAD_UP)
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (dpad release)")
    assert state.to_bytes()[OFF_DPAD] == 0, "dpad key release did not clear the D-pad"


def test_dpad_diagonal_is_reported(controller, fake_evdev):
    """C9: UP+LEFT must be reported as a diagonal (both bits set)."""
    device = _make_device(fake_evdev)
    state = controller.ControllerState()
    abs_info = _abs_info_for(fake_evdev, device)
    codes = fake_evdev.ecodes

    device.push_abs(codes.ABS_HAT0Y, -1)   # UP
    device.push_abs(codes.ABS_HAT0X, -1)   # LEFT
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (diagonal)")
    assert state.to_bytes()[OFF_DPAD] == (DPAD_UP | DPAD_LEFT)

    # Releasing one direction must keep the other one.
    device.push_abs(codes.ABS_HAT0Y, 0)
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (diagonal release)")
    assert state.to_bytes()[OFF_DPAD] == DPAD_LEFT


# ---------------------------------------------------------------------------
# C5 — SYN_DROPPED resync
# ---------------------------------------------------------------------------

def test_syn_dropped_resyncs_button_state_from_the_device(controller, fake_evdev):
    """C5: after SYN_DROPPED the held buttons are re-read from the device.

    Bug: the bridge keeps its in-memory button state when the kernel
    reports that events were dropped.  A button that was released while the
    events were lost therefore stays held forever — exactly the reported
    "let go but it still holds" symptom.
    """
    device = _make_device(fake_evdev)
    state = controller.ControllerState()
    abs_info = _abs_info_for(fake_evdev, device)
    codes = fake_evdev.ecodes

    # A is pressed, B is pressed.
    device.press(codes.BTN_SOUTH)
    device.press(codes.BTN_WEST)
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (press)")
    assert state.buttons_low & (1 << BTN_LOW_BITS["A"])
    assert state.buttons_low & (1 << BTN_LOW_BITS["Y"])

    # The user releases A — but the event never reaches the bridge.
    device.release(codes.BTN_SOUTH)
    device.drop_pending()

    # The kernel tells us that events were lost.
    device.push_syn(codes.SYN_DROPPED)
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (SYN_DROPPED resync)")

    assert not (state.buttons_low & (1 << BTN_LOW_BITS["A"])), \
        "the released button is still held after SYN_DROPPED — the state " \
        "was not re-read from the device"
    assert state.buttons_low & (1 << BTN_LOW_BITS["Y"]), \
        "a still-held button was dropped by the resync"


# ---------------------------------------------------------------------------
# C10 — triggers and sticks
# ---------------------------------------------------------------------------

def test_trigger_returns_to_zero_when_released(controller, fake_evdev):
    """C10: a released trigger must read as 0, never as permanently pressed."""
    device = _make_device(fake_evdev)
    state = controller.ControllerState()
    abs_info = _abs_info_for(fake_evdev, device)
    codes = fake_evdev.ecodes

    device.push_abs(codes.ABS_BRAKE, 1023)   # fully pulled
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (trigger down)")
    assert state.lt == 255
    assert state.to_bytes()[OFF_LT] == 255

    device.push_abs(codes.ABS_BRAKE, 0)      # released
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (trigger release)")
    assert state.lt == 0, "the released trigger still reads as pressed"
    assert state.to_bytes()[OFF_LT] == 0


def test_trigger_resting_value_is_not_reported_as_pressed(controller, fake_evdev):
    """C10: a trigger that rests at 0 must not read as pressed."""
    device = _make_device(fake_evdev)
    state = controller.ControllerState()
    abs_info = _abs_info_for(fake_evdev, device)
    codes = fake_evdev.ecodes

    device.push_abs(codes.ABS_GAS, 0)
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (resting trigger)")
    assert state.rt == 0
    assert state.to_bytes()[OFF_RT] == 0


def test_stick_returns_to_centre_when_released(controller, fake_evdev):
    """C10: a released stick must return to the centre value 32768."""
    device = _make_device(fake_evdev)
    state = controller.ControllerState()
    abs_info = _abs_info_for(fake_evdev, device)
    codes = fake_evdev.ecodes

    device.push_abs(codes.ABS_X, -32768)      # hard left
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (stick left)")
    assert state.lthumb_x == 0

    device.push_abs(codes.ABS_X, 0)          # released -> centre
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (stick release)")
    assert state.lthumb_x == 32768, "the released stick did not return to centre"
