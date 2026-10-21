"""End-to-end D-pad and axis fidelity: evdev -> frame -> receiver -> emitter.

Contract items covered here:

* **C9** — the D-pad works as hat axes (``ABS_HAT0X/Y``) *and* as
  ``BTN_DPAD_*`` key events, including the diagonal and the "release one
  direction keeps the other" case, through the whole pipeline.
* **C10** — the triggers return to 0 and the sticks return to the centre
  after a release, end to end.
* **C6** — the emitter never re-presses a button that is still held and
  never releases a button that is still held.

The bug class: a D-pad direction that is stuck (or a trigger that stays
pressed) is the same "it still holds it" symptom as a latched button.
"""

from __future__ import annotations

import pytest

from conftest import call_with_timeout, tcp_client, wait_until
from fakes import (
    BTN_LOW_BITS,
    DEFAULT_ABS_INFO,
    DPAD_DOWN,
    DPAD_LEFT,
    DPAD_RIGHT,
    DPAD_UP,
)


class Pipe:
    """fake evdev -> 54 byte frame -> TCP -> receiver -> emitter -> vgamepad."""

    def __init__(self, controller, receiver_module, emitter_module, device,
                 codes):
        self.controller = controller
        self.device = device
        self.codes = codes
        self.abs_info = dict(DEFAULT_ABS_INFO)
        self.state = controller.ControllerState()
        self.states: list[dict] = []
        self.emitter = emitter_module.XInputEmitter()
        self.pad = self.emitter._pad
        self.receiver = receiver_module.TCPReceiver(
            "127.0.0.1", 0,
            on_state=self._on_state,
        )

    def _on_state(self, state: dict) -> None:
        self.states.append(state)
        self.emitter.apply(state)

    # -- physical side -----------------------------------------------------

    def read_frame(self) -> bytes:
        """Close the current event batch and return the frame for it.

        The kernel ends an event batch with ``SYN_REPORT``; the reader
        returns the state when it sees one.  Adding the SYN here means one
        frame per batch, exactly like the real driver behaves.
        """
        self.device.sync()
        state = call_with_timeout(
            lambda: self.controller.read_next_state(self.device, self.abs_info,
                                                     self.state),
            timeout=1.0,
            what="read_next_state")
        return state.to_bytes()

    def press_key(self, code_name: str) -> None:
        """Queue a key press (the batch is closed by :meth:`sync`)."""
        self.device.press(getattr(self.codes, code_name))

    def release_key(self, code_name: str) -> None:
        """Queue a key release (the batch is closed by :meth:`sync`)."""
        self.device.release(getattr(self.codes, code_name))

    def push_abs(self, code_name: str, value: int) -> None:
        """Queue an axis event (the batch is closed by :meth:`sync`)."""
        self.device.push_abs(getattr(self.codes, code_name), value)

    def sync(self) -> None:
        """Close the event batch — the kernel ends a batch with SYN_REPORT."""
        self.device.sync()

    # -- virtual side ------------------------------------------------------

    def virtual_buttons(self) -> set[str]:
        return self.pad.held_names()


@pytest.fixture
def pipe(controller, receiver, emitter, fake_evdev):
    device = fake_evdev.new_device()
    return Pipe(controller, receiver, emitter, device, fake_evdev.ecodes)


# ---------------------------------------------------------------------------
# C9 — the D-pad as BTN_DPAD_* key events
# ---------------------------------------------------------------------------

def test_dpad_key_event_reaches_the_virtual_pad(pipe):
    """C9: a D-pad key event ends up as a D-pad button on the virtual pad."""
    with tcp_client(pipe.receiver) as client:
        pipe.press_key("BTN_DPAD_UP")
        client.sendall(pipe.read_frame())
        assert wait_until(lambda: pipe.virtual_buttons() == {"DPAD_UP"},
                          timeout=1.0), \
            f"the D-pad press did not arrive ({pipe.virtual_buttons()})"

        pipe.release_key("BTN_DPAD_UP")
        client.sendall(pipe.read_frame())
        assert wait_until(lambda: pipe.virtual_buttons() == set(),
                          timeout=1.0), \
            "the D-pad release did not arrive — the direction stays held"


def test_dpad_diagonal_reaches_the_virtual_pad(pipe):
    """C9: UP+LEFT is a diagonal — both directions are reported."""
    with tcp_client(pipe.receiver) as client:
        pipe.press_key("BTN_DPAD_UP")
        pipe.press_key("BTN_DPAD_LEFT")
        client.sendall(pipe.read_frame())

        assert wait_until(
            lambda: pipe.virtual_buttons() == {"DPAD_UP", "DPAD_LEFT"},
            timeout=1.0), \
            f"the diagonal did not arrive ({pipe.virtual_buttons()})"


def test_releasing_one_dpad_direction_keeps_the_other(pipe):
    """C9: releasing one direction must not release the other one."""
    with tcp_client(pipe.receiver) as client:
        pipe.press_key("BTN_DPAD_UP")
        pipe.press_key("BTN_DPAD_LEFT")
        client.sendall(pipe.read_frame())
        assert wait_until(
            lambda: pipe.virtual_buttons() == {"DPAD_UP", "DPAD_LEFT"},
            timeout=1.0)

        # Release only UP — LEFT must stay held.
        pipe.release_key("BTN_DPAD_UP")
        client.sendall(pipe.read_frame())

        assert wait_until(
            lambda: pipe.virtual_buttons() == {"DPAD_LEFT"},
            timeout=1.0), \
            f"releasing UP also released LEFT ({pipe.virtual_buttons()})"


def test_switching_dpad_direction_releases_the_old_one(pipe):
    """C9: UP -> RIGHT releases UP and presses RIGHT."""
    with tcp_client(pipe.receiver) as client:
        pipe.press_key("BTN_DPAD_UP")
        client.sendall(pipe.read_frame())
        assert wait_until(lambda: pipe.virtual_buttons() == {"DPAD_UP"},
                          timeout=1.0)

        pipe.release_key("BTN_DPAD_UP")
        pipe.press_key("BTN_DPAD_RIGHT")
        client.sendall(pipe.read_frame())

        assert wait_until(lambda: pipe.virtual_buttons() == {"DPAD_RIGHT"},
                          timeout=1.0), \
            f"the old direction was not released ({pipe.virtual_buttons()})"


# ---------------------------------------------------------------------------
# C9 — the D-pad as hat axes
# ---------------------------------------------------------------------------

def test_dpad_hat_axes_reach_the_virtual_pad(pipe):
    """C9: the hat axes end up as D-pad buttons on the virtual pad."""
    with tcp_client(pipe.receiver) as client:
        pipe.push_abs("ABS_HAT0Y", -1)          # UP
        client.sendall(pipe.read_frame())
        assert wait_until(lambda: pipe.virtual_buttons() == {"DPAD_UP"},
                          timeout=1.0), \
            f"the hat press did not arrive ({pipe.virtual_buttons()})"

        pipe.push_abs("ABS_HAT0Y", 0)           # released
        client.sendall(pipe.read_frame())
        assert wait_until(lambda: pipe.virtual_buttons() == set(),
                          timeout=1.0), \
            "the hat release did not arrive — the direction stays held"


def test_dpad_hat_diagonal_keeps_the_other_direction(pipe):
    """C9: releasing one hat axis keeps the other one."""
    with tcp_client(pipe.receiver) as client:
        pipe.push_abs("ABS_HAT0Y", -1)          # UP
        pipe.push_abs("ABS_HAT0X", -1)          # LEFT
        client.sendall(pipe.read_frame())
        assert wait_until(
            lambda: pipe.virtual_buttons() == {"DPAD_UP", "DPAD_LEFT"},
            timeout=1.0)

        pipe.push_abs("ABS_HAT0Y", 0)           # release UP only
        client.sendall(pipe.read_frame())
        assert wait_until(
            lambda: pipe.virtual_buttons() == {"DPAD_LEFT"},
            timeout=1.0), \
            f"releasing UP also released LEFT ({pipe.virtual_buttons()})"


def test_dpad_mask_matches_the_wire_format(pipe):
    """C9: the D-pad bit mask on the wire is the documented one."""
    with tcp_client(pipe.receiver) as client:
        expected = {
            ("ABS_HAT0Y", -1): DPAD_UP,
            ("ABS_HAT0X", 1): DPAD_RIGHT,
            ("ABS_HAT0Y", 1): DPAD_DOWN,
            ("ABS_HAT0X", -1): DPAD_LEFT,
        }
        for (code_name, value), mask in expected.items():
            other = "ABS_HAT0X" if code_name == "ABS_HAT0Y" else "ABS_HAT0Y"
            pipe.push_abs(other, 0)        # neutralise the other axis
            pipe.push_abs(code_name, value)
            frame = pipe.read_frame()
            client.sendall(frame)
            assert frame[12] == mask, \
                f"{code_name}={value} produced dpad 0x{frame[12]:02x}, " \
                f"expected 0x{mask:02x}"


# ---------------------------------------------------------------------------
# C10 — triggers and sticks, end to end
# ---------------------------------------------------------------------------

def test_triggers_return_to_zero_end_to_end(pipe):
    """C10: a released trigger reads as 0 on the virtual pad."""
    with tcp_client(pipe.receiver) as client:
        pipe.push_abs("ABS_BRAKE", 1023)        # LT fully pulled
        pipe.push_abs("ABS_GAS", 1023)          # RT fully pulled
        client.sendall(pipe.read_frame())
        assert wait_until(lambda: pipe.pad.trigger_values["lt"] == 1.0,
                          timeout=1.0), "the trigger did not reach the pad"
        assert pipe.pad.trigger_values["rt"] == 1.0

        pipe.push_abs("ABS_BRAKE", 0)           # released
        pipe.push_abs("ABS_GAS", 0)
        client.sendall(pipe.read_frame())
        assert wait_until(lambda: pipe.pad.trigger_values["lt"] == 0.0,
                          timeout=1.0), "the trigger is still pressed"
        assert pipe.pad.trigger_values["rt"] == 0.0


def test_sticks_return_to_centre_end_to_end(pipe):
    """C10: a released stick reads as the centre on the virtual pad."""
    with tcp_client(pipe.receiver) as client:
        pipe.push_abs("ABS_X", -32768)          # hard left
        client.sendall(pipe.read_frame())
        assert wait_until(lambda: pipe.pad.joystick_values["l"][0] == -1.0,
                          timeout=1.0), "the stick did not reach the pad"

        pipe.push_abs("ABS_X", 0)               # released -> centre
        client.sendall(pipe.read_frame())
        assert wait_until(lambda: pipe.pad.joystick_values["l"] == (0.0, 0.0),
                          timeout=1.0), \
            f"the stick did not return to the centre " \
            f"({pipe.pad.joystick_values['l']})"


# ---------------------------------------------------------------------------
# C6 — the emitter only reports transitions
# ---------------------------------------------------------------------------

def test_the_emitter_never_represses_or_releases_a_held_button(emitter):
    """C6: a button that is still held produces neither a press nor a release."""
    inst = emitter.XInputEmitter()
    pad = inst._pad
    XUSB = emitter._vg.XUSB_BUTTON

    inst.apply({"lthumb_x": 32768, "lthumb_y": 32768,
                "rthumb_x": 32768, "rthumb_y": 32768,
                "lt": 0, "rt": 0,
                "buttons_low": 1 << BTN_LOW_BITS["A"], "buttons_high": 0,
                "dpad": 0})
    pad.presses.clear()
    pad.releases.clear()

    for _ in range(5):
        inst.apply({"lthumb_x": 32768, "lthumb_y": 32768,
                    "rthumb_x": 32768, "rthumb_y": 32768,
                    "lt": 0, "rt": 0,
                    "buttons_low": 1 << BTN_LOW_BITS["A"], "buttons_high": 0,
                    "dpad": 0})

    assert pad.presses == [], "a button that is still held was pressed again"
    assert pad.releases == [], "a button that is still held was released"
    assert pad.held == {XUSB.XUSB_GAMEPAD_A}
