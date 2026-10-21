"""End-to-end: fake evdev -> packet -> TCP -> receiver -> emitter -> vgamepad.

Contract items covered here:

* **C1** — physical -> virtual fidelity: every button press on the fake
  controller ends up pressed on the virtual pad and every release ends up
  released.  The virtual pad's held-button set always equals the physical
  controller's held-button set.
* **C2** — a release is never lost: if the frame that carries the release
  is dropped in transit, the next frame that arrives must still report the
  button as released (state frames are authoritative).
* **C4** — corrupt input must never become ghost input.

The bug this suite reproduces: the Windows emitter never issues an explicit
``release_button()``.  A dropped frame therefore leaves the button latched
on the virtual pad until some later packet happens to reset the pad — the
user sees "I let go of the button and it is still held".
"""

from __future__ import annotations

import struct

import pytest

from conftest import call_with_timeout, tcp_client, wait_until
from fakes import (
    BTN_LOW_BITS,
    DEFAULT_ABS_INFO,
    PROTO_VERSION,
)


def _valid_frame(buttons_low: int = 0) -> bytes:
    """A valid 54-byte v2 state frame (with a controller name, like the
    Linux side sends)."""
    pkt = struct.pack(
        "<HHHHBBBBBBBBBB",
        32768, 32768, 32768, 32768,
        0, 0,
        buttons_low & 0xFF, 0, 0,
        PROTO_VERSION, 0xFF, 0x00, 0, 0,
    )
    return pkt + b"\x00" * 6 + b"Xbox Wireless Co" + b"\x00" * 14


#: 30 bytes of corruption that look like a v2 frame when read at offset 7
#: (every button bit set).  See tests/test_windows_receiver.py for details.
CORRUPTION = (b"\x00" * 17 + b"\xff\xff\xff\x02\xc8\xff" + b"\x00" * 7)

# (physical name, evdev code name, virtual pad name)
KEY_BUTTONS = [
    ("A", "BTN_SOUTH", "A"),
    ("B", "BTN_EAST", "B"),
    ("X", "BTN_NORTH", "X"),
    ("Y", "BTN_WEST", "Y"),
    ("LB", "BTN_TL", "LEFT_SHOULDER"),
    ("RB", "BTN_TR", "RIGHT_SHOULDER"),
    ("Back", "BTN_SELECT", "BACK"),
    ("Start", "BTN_START", "START"),
    ("L3", "BTN_THUMBL", "LEFT_THUMB"),
    ("R3", "BTN_THUMBR", "RIGHT_THUMB"),
    ("Guide", "BTN_MODE", "GUIDE"),
]

# (physical name, evdev axis code name, value when pressed)
HAT_BUTTONS = [
    ("DPAD_UP", "ABS_HAT0Y", -1),
    ("DPAD_RIGHT", "ABS_HAT0X", 1),
    ("DPAD_DOWN", "ABS_HAT0Y", 1),
    ("DPAD_LEFT", "ABS_HAT0X", -1),
]


class Bridge:
    """Fake evdev -> 54 byte packet -> TCP -> receiver -> emitter -> pad."""

    def __init__(self, controller, receiver_module, emitter_module, device,
                 codes):
        self.controller = controller
        self.device = device
        self.codes = codes
        self.abs_info = dict(DEFAULT_ABS_INFO)
        self.state = controller.ControllerState()
        self.states: list[dict] = []
        self.disconnects: list[str] = []
        self.hat = {"ABS_HAT0X": 0, "ABS_HAT0Y": 0}
        self.emitter = emitter_module.XInputEmitter()
        self.pad = self.emitter._pad
        self.receiver = receiver_module.TCPReceiver(
            "127.0.0.1", 0,
            on_state=self._on_state,
            on_disconnect=self._on_disconnect,
        )

    # -- callbacks ---------------------------------------------------------

    def _on_state(self, state: dict) -> None:
        self.states.append(state)
        self.emitter.apply(state)

    def _on_disconnect(self, peer_ip: str) -> None:
        self.disconnects.append(peer_ip)

    # -- physical side -----------------------------------------------------

    def read_state(self):
        """Read the next state batch from the fake controller."""
        return call_with_timeout(
            lambda: self.controller.read_next_state(self.device, self.abs_info,
                                                     self.state),
            timeout=1.0,
            what="read_next_state")

    def frame(self) -> bytes:
        """Read the state and return the wire frame for it."""
        return self.read_state().to_bytes()

    def press(self, code_name: str) -> None:
        """Physically press a button (and let the kernel report it)."""
        self.device.press(getattr(self.codes, code_name))
        self.device.sync()

    def release(self, code_name: str) -> None:
        """Physically release a button (and let the kernel report it)."""
        self.device.release(getattr(self.codes, code_name))
        self.device.sync()

    def press_hat(self, code_name: str, value: int) -> None:
        self.device.push_abs(getattr(self.codes, code_name), value)
        self.hat[code_name] = value
        self.device.sync()

    def release_hat(self, code_name: str) -> None:
        self.device.push_abs(getattr(self.codes, code_name), 0)
        self.hat[code_name] = 0
        self.device.sync()

    # -- virtual side ------------------------------------------------------

    def physical_buttons(self) -> set[str]:
        """The buttons that are currently held on the fake controller.

        The names are the ones the virtual pad uses, so the two sets can be
        compared directly.
        """
        held = set()
        for _name, code_name, virtual_name in KEY_BUTTONS:
            if getattr(self.codes, code_name) in self.device.held_keys():
                held.add(virtual_name)
        for name, code_name, value in HAT_BUTTONS:
            if self.hat[code_name] == value:
                held.add(name)
        return held

    def virtual_buttons(self) -> set[str]:
        """The buttons that are currently held on the virtual pad."""
        return self.pad.held_names()


@pytest.fixture
def bridge(controller, receiver, emitter, fake_evdev):
    """A full fake bridge: controller + receiver + emitter + fake pad."""
    device = fake_evdev.new_device()
    return Bridge(controller, receiver, emitter, device, fake_evdev.ecodes)


# ---------------------------------------------------------------------------
# C1 — physical -> virtual fidelity
# ---------------------------------------------------------------------------

def test_every_button_reaches_the_virtual_pad(bridge):
    """C1: every physical button press/release reaches the virtual pad.

    Bug: the emitter re-presses the buttons that are held but never releases
    them, so the virtual pad's button set does not follow the physical one.
    """
    with tcp_client(bridge.receiver) as client:
        for name, code_name, virtual_name in KEY_BUTTONS:
            bridge.press(code_name)
            client.sendall(bridge.frame())

            assert wait_until(lambda: bridge.virtual_buttons() == {virtual_name},
                              timeout=1.0), \
                f"{name} did not reach the virtual pad " \
                f"(pad holds {bridge.virtual_buttons()})"

            bridge.release(code_name)
            client.sendall(bridge.frame())

            assert wait_until(lambda: bridge.virtual_buttons() == set(),
                              timeout=1.0), \
                f"{name} is still held on the virtual pad after the release"


def test_dpad_reaches_the_virtual_pad(bridge):
    """C1: the D-pad (hat axes) reaches the virtual pad and is released."""
    with tcp_client(bridge.receiver) as client:
        for name, code_name, value in HAT_BUTTONS:
            bridge.press_hat(code_name, value)
            client.sendall(bridge.frame())
            assert wait_until(lambda: bridge.virtual_buttons() == {name},
                              timeout=1.0), \
                f"{name} did not reach the virtual pad " \
                f"(pad holds {bridge.virtual_buttons()})"

            bridge.release_hat(code_name)
            client.sendall(bridge.frame())
            assert wait_until(lambda: bridge.virtual_buttons() == set(),
                              timeout=1.0), \
                f"{name} is still held on the virtual pad after the release"


def test_physical_and_virtual_button_sets_always_match(bridge):
    """C1: the virtual pad mirrors the physical controller at all times."""
    with tcp_client(bridge.receiver) as client:
        sequence = [
            ("press", "BTN_SOUTH"),
            ("press", "BTN_EAST"),
            ("press", "BTN_THUMBL"),
            ("release", "BTN_SOUTH"),
            ("press", "BTN_SELECT"),
            ("release", "BTN_THUMBL"),
            ("release", "BTN_EAST"),
            ("release", "BTN_SELECT"),
        ]
        for action, code_name in sequence:
            if action == "press":
                bridge.press(code_name)
            else:
                bridge.release(code_name)
            client.sendall(bridge.frame())

            expected = bridge.physical_buttons()
            assert wait_until(lambda: bridge.virtual_buttons() == expected,
                              timeout=1.0), \
                f"virtual pad holds {bridge.virtual_buttons()}, " \
                f"physical controller holds {expected}"


# ---------------------------------------------------------------------------
# C2 — a lost release frame is corrected by the next frame
# ---------------------------------------------------------------------------

def test_dropped_release_frame_is_corrected_by_the_next_frame(bridge):
    """C2: state frames are authoritative — a dropped release self-heals.

    Bug: the emitter only releases a button as a side effect of the next
    ``pad.reset()``.  When the release frame is lost the button stays held
    on the virtual pad, and the *next* frame (which correctly reports the
    button as released) does not fix it.
    """
    with tcp_client(bridge.receiver) as client:
        # A is pressed and the frame arrives.
        bridge.press("BTN_SOUTH")
        client.sendall(bridge.frame())
        assert wait_until(lambda: bridge.virtual_buttons() == {"A"},
                          timeout=1.0)

        # The user releases A — but this frame never reaches the receiver.
        bridge.release("BTN_SOUTH")
        dropped = bridge.frame()
        assert dropped[10] & BTN_LOW_BITS["A"] == 0, \
            "the dropped frame must report the button as released"

        # The next frame that arrives must still report the release.
        bridge.device.sync()
        client.sendall(bridge.frame())

        assert wait_until(lambda: bridge.virtual_buttons() == set(),
                          timeout=1.0), \
            "the button is still held although the next frame reports it " \
            "as released"


# ---------------------------------------------------------------------------
# C4 — corrupted stream
# ---------------------------------------------------------------------------

def test_corrupted_stream_does_not_create_ghost_buttons(bridge):
    """C4: corrupted bytes must never turn into a phantom button press.

    The corrupted bytes look like a v2 frame when they are read at the wrong
    offset — with every button bit set.  The receiver must not hand that
    window to the emitter.
    """
    with tcp_client(bridge.receiver) as client:
        client.sendall(CORRUPTION + _valid_frame())

        # Wait until the receiver either reported something or gave up on
        # the misaligned connection.
        wait_until(lambda: bridge.states or bridge.disconnects, timeout=1.0)

        for state in bridge.states:
            assert state["buttons_low"] == 0, \
                "the corrupted window was reported as a state — ghost buttons!"
            assert state["buttons_high"] == 0
            assert state["dpad"] == 0

        assert bridge.virtual_buttons() == set(), \
            "the virtual pad holds buttons nobody pressed"


# ---------------------------------------------------------------------------
# Full stack through the real TCP streamer
# ---------------------------------------------------------------------------

def test_full_stack_through_the_real_tcp_streamer(
        bridge, network, fake_evdev):
    """C1/C2: the real sender, the real receiver and the real emitter.

    The physical controller is read with the real ``read_next_state()``, the
    frames are sent with the real ``TCPStreamer`` and the virtual pad is
    driven by the real ``XInputEmitter``.
    """
    from conftest import listening_receiver

    with listening_receiver(bridge.receiver) as port:
        streamer = network.TCPStreamer("127.0.0.1", port)
        streamer.start()
        try:
            assert wait_until(lambda: streamer.connected, timeout=3.0), \
                "the streamer did not connect"

            bridge.press("BTN_SOUTH")
            assert streamer.send(bridge.frame()) is True
            assert wait_until(lambda: bridge.virtual_buttons() == {"A"},
                              timeout=1.0), \
                "the press did not reach the virtual pad"

            bridge.release("BTN_SOUTH")
            assert streamer.send(bridge.frame()) is True
            assert wait_until(lambda: bridge.virtual_buttons() == set(),
                              timeout=1.0), \
                "the release did not reach the virtual pad — the button " \
                "stays held"
        finally:
            streamer.stop()
