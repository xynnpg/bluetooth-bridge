"""Regression test for the stuck-button bug.

The emitter used to call ``pad.reset()`` at the top of every ``apply()``
and only re-``press_button()`` for the bits that were set.  There was no
matching ``release_button()`` call anywhere — a button was only released
when the *next* packet's ``reset()`` happened to zero the report.  If the
release packet was lost on the wire, the button stayed latched on the
virtual gamepad (and therefore in the game) until some later packet
arrived, which is exactly the reported bug: *"when I let go of a button it
still holds it"* — followed by a game that crashes because it keeps
receiving a button that nobody is holding.

The emitter now tracks the button state it has already reported and issues
an explicit ``press_button()`` / ``release_button()`` for every
transition, so a missing release is corrected by the next packet that
arrives with the bit clear — and the virtual pad never depends on
``reset()`` to let go of a button.

The shared fakes live in :mod:`tests.fakes`.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import load_package_module  # noqa: E402
from fakes import make_state  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_SRC = ROOT / "windows" / "src"


def _load_emitter_with_fake_vgamepad():
    """Load the emitter module with a fake vgamepad injected first."""
    from fakes import make_fake_vgamepad
    import sys as _sys

    _sys.modules["vgamepad"] = make_fake_vgamepad()
    # Imported as part of a synthetic package so that the relative imports
    # (the trace helper) work exactly like in production.
    return load_package_module("bt_emitter_under_test", WINDOWS_SRC, "emitter")


@pytest.fixture
def emitter_module():
    return _load_emitter_with_fake_vgamepad()


@pytest.fixture
def pad(emitter_module):
    """A fresh emitter and the fake pad it drives."""
    emitter = emitter_module.XInputEmitter()
    return emitter, emitter._pad


def test_press_then_release_emits_press_and_release(emitter_module, pad):
    """A press is reported with press_button(), a release with release_button()."""
    emitter, fake_pad = pad
    XUSB = emitter_module._vg.XUSB_BUTTON

    emitter.apply(make_state(buttons_low=0x01))                 # A pressed
    assert fake_pad.presses == [XUSB.XUSB_GAMEPAD_A]
    assert fake_pad.releases == []

    emitter.apply(make_state(buttons_low=0x00))                 # A released
    assert fake_pad.presses == [XUSB.XUSB_GAMEPAD_A]
    assert fake_pad.releases == [XUSB.XUSB_GAMEPAD_A]
    assert fake_pad.held == set()


def test_lost_release_packet_is_recovered_from_next_packet(emitter_module, pad):
    """The regression: a dropped release event must not leave the button latched.

    The frame that carries the release never arrives; the next frame that
    does arrive still reports the button as released, and the emitter MUST
    release it then.
    """
    emitter, fake_pad = pad
    XUSB = emitter_module._vg.XUSB_BUTTON

    # Frame 1: A is pressed.  The packet that releases it is dropped.
    emitter.apply(make_state(buttons_low=0x01))
    assert XUSB.XUSB_GAMEPAD_A in fake_pad.presses
    assert XUSB.XUSB_GAMEPAD_A not in fake_pad.releases

    # Frame 2 (the very next packet to actually arrive): all buttons clear.
    emitter.apply(make_state(buttons_low=0x00))
    assert XUSB.XUSB_GAMEPAD_A in fake_pad.releases
    assert fake_pad.held == set(), "the button is still latched on the pad"


def test_held_button_emits_no_delta_on_repeated_state(emitter_module, pad):
    """A button that is still held is not pressed again."""
    emitter, fake_pad = pad
    XUSB = emitter_module._vg.XUSB_BUTTON

    emitter.apply(make_state(buttons_low=0x01))   # press
    fake_pad.presses.clear()
    fake_pad.releases.clear()

    emitter.apply(make_state(buttons_low=0x01))   # still held
    assert fake_pad.presses == []
    assert fake_pad.releases == []


def test_simultaneous_buttons_use_per_bit_deltas(emitter_module, pad):
    """Every button is tracked on its own bit."""
    emitter, fake_pad = pad
    XUSB = emitter_module._vg.XUSB_BUTTON

    emitter.apply(make_state(buttons_low=0x05))   # A + X
    pressed = set(fake_pad.presses)
    assert XUSB.XUSB_GAMEPAD_A in pressed
    assert XUSB.XUSB_GAMEPAD_X in pressed

    fake_pad.presses.clear()
    fake_pad.releases.clear()

    # Release A, keep X held
    emitter.apply(make_state(buttons_low=0x04))
    assert fake_pad.releases == [XUSB.XUSB_GAMEPAD_A]
    assert fake_pad.presses == []
    assert fake_pad.held == {XUSB.XUSB_GAMEPAD_X}


def test_dpad_buttons_use_deltas(emitter_module, pad):
    """The D-pad is released explicitly as well."""
    emitter, fake_pad = pad
    XUSB = emitter_module._vg.XUSB_BUTTON

    emitter.apply(make_state(dpad=0x01))              # UP
    assert XUSB.XUSB_GAMEPAD_DPAD_UP in fake_pad.presses

    fake_pad.presses.clear()
    fake_pad.releases.clear()

    # Switch from UP to RIGHT — UP is released, RIGHT is pressed.
    emitter.apply(make_state(dpad=0x02))
    assert fake_pad.releases == [XUSB.XUSB_GAMEPAD_DPAD_UP]
    assert fake_pad.presses == [XUSB.XUSB_GAMEPAD_DPAD_RIGHT]
    assert fake_pad.held == {XUSB.XUSB_GAMEPAD_DPAD_RIGHT}


def test_l3_r3_guide_use_deltas(emitter_module, pad):
    """L3 / R3 / Guide are released explicitly as well."""
    emitter, fake_pad = pad
    XUSB = emitter_module._vg.XUSB_BUTTON

    emitter.apply(make_state(buttons_high=0x07))     # L3 + R3 + Guide
    assert set(fake_pad.presses) == {
        XUSB.XUSB_GAMEPAD_LEFT_THUMB,
        XUSB.XUSB_GAMEPAD_RIGHT_THUMB,
        XUSB.XUSB_GAMEPAD_GUIDE,
    }

    fake_pad.presses.clear()
    fake_pad.releases.clear()

    emitter.apply(make_state(buttons_high=0x04))     # Guide only
    assert set(fake_pad.releases) == {
        XUSB.XUSB_GAMEPAD_LEFT_THUMB,
        XUSB.XUSB_GAMEPAD_RIGHT_THUMB,
    }
    assert fake_pad.held == {XUSB.XUSB_GAMEPAD_GUIDE}


def test_long_silence_preserves_hold_and_releases_clear_frame(emitter_module, pad):
    """A quiet input stream preserves holds but never suppresses a release."""
    import time as _time

    emitter, fake_pad = pad
    XUSB = emitter_module._vg.XUSB_BUTTON

    emitter.apply(make_state(buttons_low=0x01))
    fake_pad.presses.clear()
    fake_pad.releases.clear()

    # Simulate >1 s gap by rewinding the watchdog timestamp.
    emitter._last_state_at = _time.monotonic() - 2.0

    emitter.apply(make_state(buttons_low=0x01))       # still held
    assert fake_pad.releases == []
    assert fake_pad.presses == []

    emitter.apply(make_state(buttons_low=0x00))       # released after silence
    assert fake_pad.releases == [XUSB.XUSB_GAMEPAD_A]
    assert fake_pad.held == set()


def test_inactive_emitter_is_noop(emitter_module, pad):
    """A detached emitter does not touch the pad."""
    emitter, fake_pad = pad
    emitter._active = False

    emitter.apply(make_state(buttons_low=0x01))
    assert fake_pad.presses == []
    assert fake_pad.releases == []
    assert fake_pad.updates == 0


def test_clear_releases_every_button(emitter_module, pad):
    """clear() must let go of every button it holds."""
    emitter, fake_pad = pad
    XUSB = emitter_module._vg.XUSB_BUTTON

    emitter.apply(make_state(buttons_low=0x01, buttons_high=0x04, dpad=0x01))
    assert fake_pad.held

    emitter.clear()
    assert fake_pad.held == set(), "clear() left buttons held on the pad"
    assert XUSB.XUSB_GAMEPAD_A in fake_pad.releases
    assert XUSB.XUSB_GAMEPAD_DPAD_UP in fake_pad.releases


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
