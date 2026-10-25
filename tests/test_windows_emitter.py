"""Windows emitter: explicit press/release calls and the input watchdog.

Contract items covered here:

* **C6** — the emitter must call ``pad.press_button()`` for every button
  that goes released -> held and ``pad.release_button()`` for every button
  that goes held -> released.  The virtual pad's button set must be correct
  *without* relying on ``pad.reset()`` clearing the buttons.
* **C7** — watchdog: if no STATE frame arrives for a while but PING
  keepalives keep arriving, the virtual pad must release all buttons.

The bug this suite reproduces: ``XInputEmitter.apply()`` calls
``pad.reset()`` and then re-presses the buttons that are currently held, but
it *never* calls ``pad.release_button()``.  A button is therefore only
released when the next packet's ``reset()`` happens to clear it — and if
that packet is lost in transit the button stays latched in the game, which
is exactly "when I let go of a button it still holds it".
"""

from __future__ import annotations

import threading
import time

import pytest

from conftest import wait_until
from fakes import BTN_HIGH_BITS, BTN_LOW_BITS, make_state


@pytest.fixture
def XUSB(emitter):
    """The fake ``vgamepad.XUSB_BUTTON`` constants."""
    return emitter._vg.XUSB_BUTTON


def _emitter(emitter, **kwargs):
    return emitter.XInputEmitter(**kwargs)


# ---------------------------------------------------------------------------
# C6 — explicit press / release
# ---------------------------------------------------------------------------

def test_press_is_reported_explicitly(emitter, XUSB):
    """C6: a pressed button must be reported with ``press_button()``."""
    inst = _emitter(emitter)
    pad = inst._pad

    inst.apply(make_state(buttons_low=1 << BTN_LOW_BITS["A"]))
    assert XUSB.XUSB_GAMEPAD_A in pad.presses


def test_release_is_reported_explicitly(emitter, XUSB):
    """C6: a released button must be reported with ``release_button()``.

    Bug: the emitter never calls ``release_button()`` — the button is only
    released as a side effect of the next ``pad.reset()``.
    """
    inst = _emitter(emitter)
    pad = inst._pad

    inst.apply(make_state(buttons_low=1 << BTN_LOW_BITS["A"]))
    pad.presses.clear()
    pad.releases.clear()

    inst.apply(make_state(buttons_low=0))
    assert pad.releases == [XUSB.XUSB_GAMEPAD_A], \
        "the release was never reported to the virtual pad"


def test_release_does_not_depend_on_reset(emitter, XUSB):
    """C6: the pad's ``reset()`` must not be what releases the button.

    The fake pad deliberately does *not* clear its held buttons on
    ``reset()``; the emitter has to release them explicitly.
    """
    inst = _emitter(emitter)
    pad = inst._pad
    assert pad.reset_clears_buttons is False, "the fake pad must not hide the bug"

    inst.apply(make_state(buttons_low=1 << BTN_LOW_BITS["A"]))
    assert pad.held == {XUSB.XUSB_GAMEPAD_A}

    inst.apply(make_state(buttons_low=0))
    assert pad.held == set(), \
        "the button is still held on the virtual pad — the release relied " \
        "on reset() instead of an explicit release_button()"


def test_high_buttons_are_released_explicitly(emitter, XUSB):
    """C6: L3 / R3 / Guide are released explicitly as well."""
    inst = _emitter(emitter)
    pad = inst._pad

    inst.apply(make_state(buttons_high=0x07))
    assert pad.held == {
        XUSB.XUSB_GAMEPAD_LEFT_THUMB,
        XUSB.XUSB_GAMEPAD_RIGHT_THUMB,
        XUSB.XUSB_GAMEPAD_GUIDE,
    }

    inst.apply(make_state(buttons_high=0x00))
    assert pad.held == set()
    assert set(pad.releases) == {
        XUSB.XUSB_GAMEPAD_LEFT_THUMB,
        XUSB.XUSB_GAMEPAD_RIGHT_THUMB,
        XUSB.XUSB_GAMEPAD_GUIDE,
    }


def test_dpad_is_released_explicitly(emitter, XUSB):
    """C6: a D-pad direction is released explicitly as well."""
    inst = _emitter(emitter)
    pad = inst._pad

    inst.apply(make_state(dpad=0x01))          # UP
    assert pad.held == {XUSB.XUSB_GAMEPAD_DPAD_UP}

    inst.apply(make_state(dpad=0x00))
    assert pad.held == set()
    assert XUSB.XUSB_GAMEPAD_DPAD_UP in pad.releases


def test_repeated_state_does_not_repress_buttons(emitter, XUSB):
    """C6: only transitions are reported — a held button is not re-pressed.

    The emitter keeps track of the buttons it already reported so that a
    lost release is corrected by the next packet.
    """
    inst = _emitter(emitter)
    pad = inst._pad

    inst.apply(make_state(buttons_low=1 << BTN_LOW_BITS["A"]))
    pad.presses.clear()
    pad.releases.clear()

    inst.apply(make_state(buttons_low=1 << BTN_LOW_BITS["A"]))
    assert pad.presses == [], "a button that is still held was pressed again"
    assert pad.releases == []


# ---------------------------------------------------------------------------
# C10 — triggers and sticks on the virtual pad
# ---------------------------------------------------------------------------

def test_trigger_returns_to_zero_when_released(emitter):
    """C10: a released trigger must be reported as 0.0, never as pressed.

    Bug: a trigger that keeps its last value makes the game fire
    continuously — "the trigger is stuck".
    """
    inst = _emitter(emitter)
    pad = inst._pad

    inst.apply(make_state(lt=255, rt=255))
    assert pad.trigger_values["lt"] == 1.0
    assert pad.trigger_values["rt"] == 1.0

    inst.apply(make_state(lt=0, rt=0))
    assert pad.trigger_values["lt"] == 0.0, "the trigger is still pressed"
    assert pad.trigger_values["rt"] == 0.0, "the trigger is still pressed"


def test_stick_returns_to_centre_when_released(emitter):
    """C10: a released stick must be reported as the centre value (0.0)."""
    inst = _emitter(emitter)
    pad = inst._pad

    inst.apply(make_state(lthumb_x=0, lthumb_y=0))
    assert pad.joystick_values["l"] == (-1.0, 1.0)

    inst.apply(make_state(lthumb_x=32768, lthumb_y=32768))
    assert pad.joystick_values["l"] == (0.0, 0.0), \
        "the stick did not return to the centre"


# ---------------------------------------------------------------------------
# C7 — watchdog
# ---------------------------------------------------------------------------

def test_clear_releases_every_button(emitter, XUSB):
    """C7: ``clear()`` must release the buttons it holds.

    Bug: ``clear()`` only calls ``pad.reset()``; when the pad does not clear
    its buttons on reset the game keeps seeing the held button.
    """
    inst = _emitter(emitter)
    pad = inst._pad

    inst.apply(make_state(buttons_low=1 << BTN_LOW_BITS["A"],
                          buttons_high=1 << BTN_HIGH_BITS["L3"]))
    assert pad.held

    inst.clear()
    assert pad.held == set(), \
        "clear() left buttons held on the virtual pad"


def _windows_app(windows_main, tmp_path, keepalive_timeout: float = 0.2):
    """Build a Windows BridgeApp with a short keepalive timeout."""
    config_file = tmp_path / "config.ini"
    config_file.write_text(
        "[network]\nkeepalive_timeout_s = %s\n" % keepalive_timeout,
        encoding="utf-8")
    config = windows_main.Config(str(config_file))
    app = windows_main.BridgeApp(config)
    app._running = True
    return app


def test_watchdog_releases_buttons_when_the_stream_goes_silent(
        windows_main, tmp_path):
    """C7: when the Linux side stops sending state frames the pad must let
    go of every button.

    Bug: the watchdog calls ``emitter.clear()``, which only resets the pad;
    a pad that does not clear its buttons on reset keeps the button held.
    """
    app = _windows_app(windows_main, tmp_path)
    pad = app._emitter._pad

    app._on_state(make_state(buttons_low=1 << BTN_LOW_BITS["A"]))
    assert pad.held, "the test setup did not press the button"

    worker = threading.Thread(target=app._monitor_loop, daemon=True)
    worker.start()
    try:
        released = wait_until(lambda: not pad.held, timeout=1.0)
    finally:
        app._running = False
        worker.join(timeout=2)

    assert released, \
        "the button is still held although the input stream died"


def test_watchdog_releases_buttons_when_only_pings_arrive(
        windows_main, tmp_path):
    """C7: PING keepalives must not keep a dead input stream alive.

    Bug: the keepalive refreshes the same timestamp the watchdog uses, so
    a Linux side that only sends PINGs (no state frames) keeps the last
    buttons latched forever.
    """
    app = _windows_app(windows_main, tmp_path)
    pad = app._emitter._pad

    app._on_state(make_state(buttons_low=1 << BTN_LOW_BITS["A"]))
    assert pad.held, "the test setup did not press the button"

    worker = threading.Thread(target=app._monitor_loop, daemon=True)
    worker.start()
    try:
        # The Linux side is alive but does not send any state frame.
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            app._on_ping()
            time.sleep(0.02)
        released = wait_until(lambda: not pad.held, timeout=1.0)
    finally:
        app._running = False
        worker.join(timeout=2)

    assert released, \
        "the button is still held although only PING keepalives arrive"


class _RumbleReceiver:
    def __init__(self):
        self.packets: list[bytes] = []

    def send_state(self, packet: bytes) -> bool:
        self.packets.append(packet)
        return True


def test_test_rumble_sends_a_pulse_then_stops(windows_main, tmp_path):
    """The dashboard's Test motors action must drive the real path."""
    app = _windows_app(windows_main, tmp_path)
    receiver = _RumbleReceiver()
    app._receiver = receiver
    app._pc_reachable = True

    assert app._test_rumble() is True
    assert wait_until(lambda: len(receiver.packets) >= 2, timeout=2.0), \
        "the rumble test did not send both the pulse and the stop"

    pulse, stop = receiver.packets[0], receiver.packets[-1]
    assert (pulse[16], pulse[17]) == (255, 255)
    assert pulse[40:48] == b"RUMBTEST", \
        "the test pulse must be marked so Linux can run its direct self-test"
    assert (stop[16], stop[17]) == (0, 0)
    assert (app._state.rumble_left, app._state.rumble_right) == (0, 0)


def test_test_rumble_respects_the_vibration_toggle(windows_main, tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("[controller]\nrumble_enabled = false\n",
                           encoding="utf-8")
    app = windows_main.BridgeApp(windows_main.Config(str(config_file)))
    app._receiver = _RumbleReceiver()
    app._pc_reachable = True

    with pytest.raises(RuntimeError, match="turned off"):
        app._test_rumble()
    assert app._receiver.packets == []


def test_turning_vibration_off_stops_running_motors(windows_main, tmp_path):
    """The dashboard switch must stop a motor that is already rumbling.

    The monitor loop periodically re-sends the last rumble values.  Clearing
    the emitter's flag alone left the bridge's own ``_last_rumble`` intact,
    so the next resend started the motors again.
    """
    config_file = tmp_path / "config.ini"
    config_file.write_text("[controller]\nrumble_enabled = false\n",
                           encoding="utf-8")
    app = windows_main.BridgeApp(windows_main.Config(str(config_file)))
    receiver = _RumbleReceiver()
    app._receiver = receiver
    app._pc_reachable = True
    app._last_rumble = (180, 90)
    app._state.on_rumble(180, 90)

    app._on_settings_applied({"controller.rumble_enabled": False})

    assert app._emitter._rumble_enabled is False
    assert app._last_rumble == (0, 0)
    assert (app._state.rumble_left, app._state.rumble_right) == (0, 0)
    assert receiver.packets, "no stop packet was sent"
    stop = receiver.packets[-1]
    assert (stop[16], stop[17]) == (0, 0)


def test_stop_rumble_action_sends_zero_and_clears_state(windows_main, tmp_path):
    app = _windows_app(windows_main, tmp_path)
    receiver = _RumbleReceiver()
    app._receiver = receiver
    app._pc_reachable = True
    app._last_rumble = (200, 120)
    app._state.on_rumble(200, 120)

    assert app._stop_rumble() is True

    assert app._last_rumble == (0, 0)
    assert (app._state.rumble_left, app._state.rumble_right) == (0, 0)
    assert receiver.packets
    assert (receiver.packets[-1][16], receiver.packets[-1][17]) == (0, 0)
