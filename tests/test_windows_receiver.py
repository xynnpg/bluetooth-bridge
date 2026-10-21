"""Windows receiver: frame parsing, PING handling and stream corruption.

Contract items covered here:

* **C4** — corrupt input must never become ghost input.  A misaligned or
  truncated frame in the byte stream must not be handed to ``on_state``;
  the receiver has to detect the corruption and recover (re-sync to a valid
  frame boundary or drop the connection).

The bug this suite reproduces: the receiver only validates the *proto byte*
when it looks for a frame boundary.  When a frame was cut short in transit
the whole stream is shifted, and a shifted window that happens to carry the
right proto byte is parsed as if it were a real frame — the game then sees
button presses nobody made ("it crashes when I play").

Note on the protocol: the v2 frame carries no checksum, so a shifted window
can look exactly like a real frame.  The only way to *guarantee* that no
phantom press ever reaches the game is to validate a candidate frame before
accepting it (proto byte **and** the reserved padding bytes) and to drop the
connection when the stream is misaligned and no valid frame can be found.
"""

from __future__ import annotations

import struct

from fakes import (
    OFF_BUTTONS_LOW,
    PING_PAYLOAD,
    PROTO_VERSION,
    ScriptedSocket,
)

#: A non-empty controller name — the Linux side always sends one.
NAME = b"Xbox Wireless Co"


def _frame(buttons_low: int = 0, buttons_high: int = 0, dpad: int = 0) -> bytes:
    """Build a valid 54-byte v2 state frame."""
    pkt = struct.pack(
        "<HHHHBBBBBBBBBB",
        32768, 32768, 32768, 32768,
        0, 0,
        buttons_low & 0xFF,
        buttons_high & 0xFF,
        dpad & 0x0F,
        PROTO_VERSION,
        0xFF,
        0x00,
        0, 0,
    )
    return pkt + b"\x00" * 6 + NAME.ljust(16, b"\x00") + b"\x00" * 14


#: 30 bytes of corruption.  Read as a 54 byte window starting at offset 7 the
#: bytes look like a v2 frame:
#:     byte 10 = 0xFF  -> every low button "pressed"
#:     byte 11 = 0xFF  -> every high button "pressed"
#:     byte 12 = 0xFF  -> an impossible d-pad mask
#:     byte 13 = 0x02  -> a plausible proto byte
#:     byte 14 = 0xC8  -> an impossible battery level
#:     byte 15 = 0xFF  -> impossible flags
#: The very first window (offset 0) has an invalid proto byte, so the
#: receiver *does* notice that the stream is misaligned.
CORRUPTION = (b"\x00" * 17 + b"\xff\xff\xff\x02\xc8\xff" + b"\x00" * 7)


class _Result:
    def __init__(self, states, disconnects, sock):
        self.states = states
        self.disconnects = disconnects
        self.sock = sock

    @property
    def dropped(self) -> bool:
        """True when the receiver tore the (misaligned) connection down."""
        return bool(self.disconnects) or self.sock.closed


def _run_receiver(receiver_module, script: bytes, chunk_size: int = 16) -> _Result:
    """Feed *script* to a receiver and collect what it reported."""
    states: list[dict] = []
    disconnects: list[str] = []
    receiver = receiver_module.TCPReceiver("127.0.0.1", 0,
                                           on_state=states.append,
                                           on_disconnect=disconnects.append)
    receiver._running = True

    sock = ScriptedSocket(script, chunk_size=chunk_size)
    receiver._conn = sock
    receiver._conn_generation = 7
    receiver._serve(sock, ("127.0.0.1", 12345), 7)
    return _Result(states, disconnects, sock)


def _expected_state(packet: bytes) -> dict:
    """The state dict a *correct* receiver produces for *packet*."""
    return {
        "lthumb_x": 32768, "lthumb_y": 32768,
        "rthumb_x": 32768, "rthumb_y": 32768,
        "lt": 0, "rt": 0,
        "buttons_low": packet[OFF_BUTTONS_LOW],
        "buttons_high": packet[11],
        "dpad": packet[12],
        "battery": -1, "charging": False,
        "rumble_left": 0, "rumble_right": 0,
        "controller_mac": "",
        "controller_name": NAME.decode("ascii"),
    }


# ---------------------------------------------------------------------------
# Parsing / PING
# ---------------------------------------------------------------------------

def test_valid_state_frame_is_parsed(receiver):
    """A well formed v2 frame is turned into the state dict."""
    state = receiver.StateParser.parse(_frame(buttons_low=0x03))
    assert state is not None
    assert state["buttons_low"] == 0x03
    assert state["buttons_high"] == 0
    assert state["dpad"] == 0


def test_ping_frame_is_not_reported_as_state(receiver):
    """The keepalive PING must never be parsed as a controller state."""
    assert receiver.StateParser.parse(PING_PAYLOAD) is None


def test_partial_frame_is_buffered_until_it_is_complete(receiver):
    """A frame that arrives in pieces must not be reported half-way.

    Bug: a 24 byte tail could be mis-read as a legacy v1 frame, which
    produces phantom button presses.
    """
    frame = _frame(buttons_low=0x01)
    result = _run_receiver(receiver, frame[:30])
    assert result.states == [], "a truncated frame was reported as a state"

    result = _run_receiver(receiver, frame[:30] + frame[30:])
    assert len(result.states) == 1
    assert result.states[0]["buttons_low"] == 0x01


# ---------------------------------------------------------------------------
# C4 — corrupted stream
# ---------------------------------------------------------------------------

def test_corrupted_stream_never_produces_a_ghost_button_state(receiver):
    """C4: a corrupted frame in the middle of the stream must not create
    phantom button presses.

    The corruption is chosen so that the *shifted* window looks like a v2
    frame (right proto byte, every button bit set).  A receiver that only
    looks at the proto byte hands that window to ``on_state`` and the game
    sees a button press nobody made.  A correct receiver validates the
    candidate frame before accepting it.
    """
    valid = _frame(buttons_low=0x00)

    result = _run_receiver(receiver, CORRUPTION + valid)

    expected = _expected_state(valid)
    for state in result.states:
        assert state == expected, \
            "a corrupted window was reported as a state — ghost buttons!"


def test_receiver_recovers_after_corruption(receiver):
    """C4: after corrupted bytes the receiver must recover.

    Recovering means either re-syncing to the next valid frame boundary (the
    real frame is then reported) or dropping the misaligned connection.
    What must not happen is that the receiver keeps reporting ghost states.
    """
    valid = _frame(buttons_low=0x02)

    result = _run_receiver(receiver, CORRUPTION + valid + valid)

    expected = _expected_state(valid)
    for state in result.states:
        assert state == expected, \
            "a corrupted window was reported as a state — ghost buttons!"

    recovered = any(state == expected for state in result.states)
    assert recovered or result.dropped, \
        "the receiver neither re-synced nor dropped the corrupted connection"


def test_aligned_stream_is_never_misread(receiver):
    """C4: a healthy stream is reported frame by frame, in order."""
    frames = [_frame(buttons_low=0x01), _frame(buttons_low=0x00),
              _frame(buttons_low=0x04)]
    result = _run_receiver(receiver, b"".join(frames))
    assert [s["buttons_low"] for s in result.states] == [0x01, 0x00, 0x04]


def test_ping_between_frames_is_not_treated_as_state(receiver):
    """C4: PING keepalives must not disturb the frame stream."""
    frames = _frame(buttons_low=0x01) + PING_PAYLOAD + _frame(buttons_low=0x00)
    result = _run_receiver(receiver, frames)
    assert [s["buttons_low"] for s in result.states] == [0x01, 0x00]
