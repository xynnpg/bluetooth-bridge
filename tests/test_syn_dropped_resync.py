"""``SYN_DROPPED``: the state must be re-read from the device.

Contract item covered here:

* **C5** — after ``EV_SYN / SYN_DROPPED`` the *complete* state has to be
  re-read from the device, not just the digital buttons: the D-pad
  direction and the analog triggers are still held by the kernel and must
  not be reported as released.

The bug class: a stale in-memory state latches a button (or drops a
release) forever, because every following packet repeats the stale value.
"""

from __future__ import annotations

from conftest import call_with_timeout
from fakes import BTN_LOW_BITS, DEFAULT_ABS_INFO, DPAD_UP


def _abs_info():
    return dict(DEFAULT_ABS_INFO)


def _syn_dropped(controller, fake_evdev, device, state, abs_info):
    """Deliver a SYN_DROPPED and read the resynchronised state."""
    device.push_syn(fake_evdev.ecodes.SYN_DROPPED)
    device.sync()
    return call_with_timeout(
        lambda: controller.read_next_state(device, abs_info, state),
        what="read_next_state (SYN_DROPPED)")


# ---------------------------------------------------------------------------
# The digital buttons
# ---------------------------------------------------------------------------

def test_a_still_held_button_survives_a_syn_dropped(controller, fake_evdev):
    """C5: the buttons the device still holds must be restored."""
    device = fake_evdev.new_device()
    state = controller.ControllerState()
    abs_info = _abs_info()
    codes = fake_evdev.ecodes

    device.press(codes.BTN_SOUTH)
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (press)")

    _syn_dropped(controller, fake_evdev, device, state, abs_info)

    assert state.buttons_low & (1 << BTN_LOW_BITS["A"]), \
        "a button that is still held was dropped by the resync"


# ---------------------------------------------------------------------------
# The D-pad
# ---------------------------------------------------------------------------

def test_the_dpad_direction_survives_a_syn_dropped(controller, fake_evdev):
    """C5: the D-pad direction must survive a resync.

    The kernel caches the hat axes; the bridge must read them back.
    """
    device = fake_evdev.new_device()
    state = controller.ControllerState()
    abs_info = _abs_info()
    codes = fake_evdev.ecodes

    device.push_abs(codes.ABS_HAT0Y, -1)     # UP
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (hat up)")
    assert state.to_bytes()[12] == DPAD_UP

    _syn_dropped(controller, fake_evdev, device, state, abs_info)

    assert state.to_bytes()[12] == DPAD_UP, \
        "the D-pad direction was lost by the resync — the physical " \
        "controller still holds it"


def test_the_dpad_direction_survives_a_syn_dropped_on_a_real_device(
        controller, fake_evdev):
    """C5: the same, with a device that behaves like ``python-evdev``.

    ``python-evdev``'s ``AbsInfo`` named tuple carries only
    ``min / max / fuzz / flat / resolution`` — there is **no** ``value``
    attribute, so the current axis value has to come from
    ``device.state``.  A resync that only looks at ``capabilities()``
    cannot restore the D-pad.
    """
    device = fake_evdev.new_device(caps_expose_value=False)
    state = controller.ControllerState()
    abs_info = _abs_info()
    codes = fake_evdev.ecodes

    device.push_abs(codes.ABS_HAT0Y, -1)     # UP
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (hat up)")
    assert state.to_bytes()[12] == DPAD_UP

    _syn_dropped(controller, fake_evdev, device, state, abs_info)

    assert state.to_bytes()[12] == DPAD_UP, \
        "the D-pad direction was lost by the resync — the current axis " \
        "value is not read from the device"


# ---------------------------------------------------------------------------
# The analog triggers
# ---------------------------------------------------------------------------

def test_a_held_trigger_survives_a_syn_dropped(controller, fake_evdev):
    """C5: a trigger that is held must not read as released after a resync.

    The kernel still knows that the trigger is pulled; reporting it as 0
    would look to the game like a trigger that was let go of.
    """
    device = fake_evdev.new_device()
    state = controller.ControllerState()
    abs_info = _abs_info()
    codes = fake_evdev.ecodes

    device.push_abs(codes.ABS_BRAKE, 1023)   # LT fully pulled
    device.sync()
    call_with_timeout(lambda: controller.read_next_state(device, abs_info, state),
                      what="read_next_state (trigger down)")
    assert state.lt == 255

    _syn_dropped(controller, fake_evdev, device, state, abs_info)

    assert state.lt == 255, \
        "the held trigger reads as released after the resync — the " \
        "physical controller still has it pulled"
