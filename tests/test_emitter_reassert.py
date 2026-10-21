"""Emitter: the periodic full re-assert and the tracking of the pad state.

Contract items covered here:

* **D** — the emitter never relies on ``pad.reset()`` to release a button:
  every transition is an explicit ``press_button()`` / ``release_button()``.
* **D** — every few seconds the complete state is re-asserted, so the
  virtual pad can never drift away from the physical controller.  The
  re-assert must *not* release a button that is still held.

The bug class: a button that is latched on the virtual pad (or released
while it is still held) is what the user sees as "it still holds it".
"""

from __future__ import annotations

import time

from fakes import BTN_LOW_BITS, make_state


def _held_state(**kwargs):
    state = make_state(**kwargs)
    return state


def test_the_reassert_repeats_the_pressed_buttons(emitter):
    """D: the periodic re-assert presses every held button again."""
    inst = emitter.XInputEmitter()
    pad = inst._pad
    XUSB = emitter._vg.XUSB_BUTTON

    inst.apply(make_state(buttons_low=1 << BTN_LOW_BITS["A"]))
    pad.presses.clear()
    pad.releases.clear()
    resets_before = pad.resets

    # Pretend the re-assert interval has passed.
    inst._last_reassert_at = time.monotonic() - 3600.0
    inst.apply(make_state(buttons_low=1 << BTN_LOW_BITS["A"]))

    assert pad.resets > resets_before, "the pad was not reset"
    assert XUSB.XUSB_GAMEPAD_A in pad.presses, \
        "the held button was not re-asserted"


def test_the_reassert_does_not_release_a_held_button(emitter):
    """D: the re-assert must not let go of a button that is still held."""
    inst = emitter.XInputEmitter()
    pad = inst._pad
    XUSB = emitter._vg.XUSB_BUTTON

    inst.apply(make_state(buttons_low=1 << BTN_LOW_BITS["A"]))
    pad.releases.clear()

    inst._last_reassert_at = time.monotonic() - 3600.0
    inst.apply(make_state(buttons_low=1 << BTN_LOW_BITS["A"]))

    assert XUSB.XUSB_GAMEPAD_A not in pad.releases, \
        "the re-assert released a button that is still held"
    assert pad.held == {XUSB.XUSB_GAMEPAD_A}


def test_the_reassert_keeps_the_dpad(emitter):
    """D: the re-assert also repeats the D-pad directions."""
    inst = emitter.XInputEmitter()
    pad = inst._pad
    XUSB = emitter._vg.XUSB_BUTTON

    inst.apply(make_state(dpad=0x09))          # UP + LEFT
    pad.presses.clear()

    inst._last_reassert_at = time.monotonic() - 3600.0
    inst.apply(make_state(dpad=0x09))

    assert XUSB.XUSB_GAMEPAD_DPAD_UP in pad.presses
    assert XUSB.XUSB_GAMEPAD_DPAD_LEFT in pad.presses
    assert pad.held == {XUSB.XUSB_GAMEPAD_DPAD_UP, XUSB.XUSB_GAMEPAD_DPAD_LEFT}


def test_clear_resyncs_the_tracked_state(emitter):
    """D: after ``clear()`` the emitter must not think it still holds A."""
    inst = emitter.XInputEmitter()
    pad = inst._pad
    XUSB = emitter._vg.XUSB_BUTTON

    inst.apply(make_state(buttons_low=1 << BTN_LOW_BITS["A"]))
    inst.clear()
    assert pad.held == set()

    # The physical button is still held — the next frame must press it
    # again instead of treating it as "already pressed".
    pad.presses.clear()
    inst.apply(make_state(buttons_low=1 << BTN_LOW_BITS["A"]))
    assert XUSB.XUSB_GAMEPAD_A in pad.presses, \
        "the button was not re-pressed after clear()"
    assert pad.held == {XUSB.XUSB_GAMEPAD_A}


def test_detach_releases_everything(emitter):
    """D: detaching the pad must let go of every button."""
    inst = emitter.XInputEmitter()
    pad = inst._pad
    XUSB = emitter._vg.XUSB_BUTTON

    inst.apply(make_state(buttons_low=0xFF, buttons_high=0x07, dpad=0x0F))
    assert pad.held

    inst.detach()
    assert pad.held == set(), "detach() left buttons held on the pad"
    assert XUSB.XUSB_GAMEPAD_A in pad.releases


def test_a_detached_emitter_ignores_new_states(emitter):
    """D: after detach() no further state may reach the pad."""
    inst = emitter.XInputEmitter()
    pad = inst._pad

    inst.detach()
    pad.presses.clear()
    inst.apply(make_state(buttons_low=1 << BTN_LOW_BITS["A"]))
    assert pad.presses == []
    assert pad.held == set()
