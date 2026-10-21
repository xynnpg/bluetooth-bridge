"""Frame integrity: the Linux checksum and the Windows frame validator.

Contract items covered here:

* **A** — ``ControllerState.to_bytes()`` writes a checksum into byte 52
  (``sum(bytes 0..51) & 0xFF``) and announces it with flag bit 3, so the
  checksum is verified on *every* frame — including one whose checksum
  happens to be 0.  A sender that does not implement checksums (an older
  Linux side) does not set the flag and stays compatible.
* **C** — ``is_valid_frame()`` validates the proto byte, the D-pad mask,
  the battery range, the flags, the reserved bytes and (when present) the
  checksum.  A frame that is *not* valid is a misaligned window and must
  never be turned into controller input.

The bug class this suite guards against: a frame that is cut short on the
wire shifts every following frame.  A shifted window that happens to look
plausible is decoded as button presses nobody made — the "the game crashes
when I play" symptom.  The checksum plus the field validation is what makes
the receiver notice.
"""

from __future__ import annotations

import struct

import pytest

from fakes import (
    OFF_BUTTONS_HIGH,
    OFF_BUTTONS_LOW,
    OFF_DPAD,
    OFF_LT,
    OFF_PROTO,
    OFF_RT,
    PAYLOAD_SIZE,
    PROTO_VERSION,
)

#: Byte offsets of the fields that carry a fixed meaning.
OFF_BATTERY   = 14
OFF_FLAGS     = 15
OFF_RESERVED0 = 40
OFF_CHECKSUM  = 52
OFF_LAST      = 53

#: flags bit 2 — "this sender keeps streaming the state even when nothing
#: happens" (announces the idle heartbeat to the PC side).
FLAG_STREAMING = 0x04

#: flags bit 3 — "byte 52 holds a valid checksum".
FLAG_CHECKSUM = 0x08


def _make_frame(controller, buttons_low: int = 0, buttons_high: int = 0,
                dpad: int = 0, lt: int = 0, rt: int = 0,
                lthumb_x: int = 32768, lthumb_y: int = 32768,
                rthumb_x: int = 32768, rthumb_y: int = 32768,
                battery: int = 0xFF, charging: bool = False,
                mac: str = "", name: str = "") -> bytes:
    """Build a 54-byte frame with the *Linux* serialiser."""
    state = controller.ControllerState(
        lthumb_x=lthumb_x, lthumb_y=lthumb_y,
        rthumb_x=rthumb_x, rthumb_y=rthumb_y,
        lt=lt, rt=rt,
        buttons_low=buttons_low, buttons_high=buttons_high,
        battery=battery, charging=charging,
        controller_mac=mac, controller_name=name,
    )
    if dpad & 0x01:
        state._hat_y = -1
    elif dpad & 0x04:
        state._hat_y = 1
    if dpad & 0x02:
        state._hat_x = 1
    elif dpad & 0x08:
        state._hat_x = -1
    return state.to_bytes()


def _frame_with_checksum(controller, **kwargs) -> bytes:
    """Like :func:`_make_frame` but guarantees a non-zero checksum.

    The checksum is announced by flag bit 3, so a checksum of 0 is
    protected as well — but a *non-zero* checksum keeps the corruption
    tests unambiguous: flipping the checksum byte can then not turn the
    checksum into 0 by accident.
    """
    frame = _make_frame(controller, **kwargs)
    if frame[OFF_CHECKSUM] != 0:
        return frame
    # Nudge the button bits until the checksum is non-zero.
    for bump in (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80):
        kwargs = dict(kwargs)
        kwargs["buttons_low"] = (kwargs.get("buttons_low", 0) | bump)
        frame = _make_frame(controller, **kwargs)
        if frame[OFF_CHECKSUM] != 0:
            return frame
    raise AssertionError("could not build a frame with a non-zero checksum")


# ---------------------------------------------------------------------------
# A — the Linux side writes a checksum
# ---------------------------------------------------------------------------

def test_linux_frame_is_accepted_by_the_windows_validator(controller, receiver):
    """A: every frame the Linux side produces must validate on Windows."""
    frames = [
        _make_frame(controller),
        _make_frame(controller, buttons_low=0xFF, buttons_high=0x07, dpad=0x0F),
        _make_frame(controller, lt=255, rt=128, lthumb_x=0, rthumb_y=65535),
        _make_frame(controller, battery=100, charging=True,
                    mac="aa:bb:cc:dd:ee:ff", name="Xbox Wireless Co"),
    ]
    for frame in frames:
        assert len(frame) == PAYLOAD_SIZE
        assert receiver.is_valid_frame(frame), \
            f"the Windows side rejected a valid Linux frame: {frame.hex()}"


def test_checksum_byte_is_the_documented_sum(controller):
    """A: byte 52 holds ``sum(bytes 0..51) & 0xFF``."""
    frame = _make_frame(controller, buttons_low=0x03, dpad=0x01)
    assert frame[OFF_CHECKSUM] == sum(frame[:OFF_CHECKSUM]) & 0xFF


def test_reserved_bytes_stay_zero(controller):
    """A: bytes 40..51 and byte 53 are reserved and must stay zero."""
    frame = _make_frame(controller, buttons_low=0x03)
    assert frame[OFF_RESERVED0:OFF_CHECKSUM] == b"\x00" * 12
    assert frame[OFF_LAST] == 0


def test_streaming_flag_is_announced(controller):
    """A: the Linux side announces that it streams idle heartbeats.

    The PC side may only watch the input stream with the state watchdog
    when the peer announces that it keeps sending the state.
    """
    frame = _make_frame(controller)
    assert frame[OFF_FLAGS] & FLAG_STREAMING, \
        "the streaming flag (flags bit 2) is not set"


# ---------------------------------------------------------------------------
# C — the Windows validator
# ---------------------------------------------------------------------------

def test_is_valid_frame_rejects_a_wrong_proto_byte(controller, receiver):
    """C: a frame with the wrong proto byte is not a v2 state frame."""
    frame = bytearray(_make_frame(controller, buttons_low=0x01))
    frame[OFF_PROTO] = 0x03
    assert not receiver.is_valid_frame(bytes(frame))


def test_is_valid_frame_rejects_an_impossible_dpad_mask(controller, receiver):
    """C: the D-pad is a 4 bit mask."""
    frame = bytearray(_make_frame(controller))
    frame[OFF_DPAD] = 0x10
    assert not receiver.is_valid_frame(bytes(frame))


def test_is_valid_frame_rejects_an_out_of_range_battery(controller, receiver):
    """C: the battery is 0..100 or "unknown" (0xFF)."""
    frame = bytearray(_make_frame(controller))
    frame[OFF_BATTERY] = 200
    assert not receiver.is_valid_frame(bytes(frame))


def test_is_valid_frame_accepts_the_defined_flags(controller, receiver):
    """C: charging / battery-valid / streaming are all legal."""
    for flags in (0x00, 0x01, 0x02, 0x04, 0x05, 0x06, 0x07):
        frame = bytearray(_make_frame(controller))
        frame[OFF_FLAGS] = flags
        # fix the checksum so only the flag is under test
        frame[OFF_CHECKSUM] = sum(frame[:OFF_CHECKSUM]) & 0xFF
        assert receiver.is_valid_frame(bytes(frame)), \
            f"flags 0x{flags:02x} was rejected"


def test_is_valid_frame_rejects_unknown_flag_bits(controller, receiver):
    """C: a flag bit nobody defined means the frame is not a state frame."""
    frame = bytearray(_make_frame(controller))
    frame[OFF_FLAGS] = 0x80
    frame[OFF_CHECKSUM] = sum(frame[:OFF_CHECKSUM]) & 0xFF
    assert not receiver.is_valid_frame(bytes(frame))


@pytest.mark.parametrize("offset", [OFF_RESERVED0, OFF_RESERVED0 + 5,
                                    OFF_CHECKSUM - 1, OFF_LAST])
def test_is_valid_frame_rejects_non_zero_reserved_bytes(controller, receiver,
                                                         offset):
    """C: the reserved bytes are always zero — a non-zero one means garbage."""
    frame = bytearray(_make_frame(controller))
    frame[offset] = 0x01
    assert not receiver.is_valid_frame(bytes(frame))


def test_is_valid_frame_rejects_a_wrong_checksum(controller, receiver):
    """C: a frame whose checksum does not match is corrupt."""
    frame = bytearray(_frame_with_checksum(controller, buttons_low=0x01))
    frame[OFF_CHECKSUM] ^= 0xFF
    assert frame[OFF_CHECKSUM] != 0
    assert not receiver.is_valid_frame(bytes(frame))


def test_is_valid_frame_rejects_a_short_frame(receiver):
    """C: a truncated frame is never a valid frame."""
    assert not receiver.is_valid_frame(b"\x00" * 53)
    assert not receiver.is_valid_frame(b"")


def test_a_legacy_frame_without_a_checksum_is_still_accepted(controller,
                                                              receiver):
    """C: a sender that does not implement checksums stays compatible.

    A legacy sender does not set flag bit 3 ("checksum present"), so its
    frames are accepted without a checksum comparison (the other field
    checks still apply).
    """
    frame = bytearray(_make_frame(controller, buttons_low=0x01))
    frame[OFF_FLAGS] &= ~FLAG_CHECKSUM      # a legacy sender
    frame[OFF_CHECKSUM] = 0
    assert receiver.is_valid_frame(bytes(frame))


def test_a_legacy_frame_is_rejected_when_a_checksum_is_required(controller,
                                                                receiver):
    """C: for a peer that announces checksums, every frame must carry one.

    This is what makes the byte-by-byte re-sync safe: a window that is
    shifted can never pass the validation.
    """
    frame = bytearray(_make_frame(controller, buttons_low=0x01))
    frame[OFF_FLAGS] &= ~FLAG_CHECKSUM
    assert not receiver.is_valid_frame(bytes(frame),
                                       require_checksum=True)


@pytest.mark.parametrize("offset", list(range(OFF_CHECKSUM)))
def test_a_single_flipped_byte_is_rejected(controller, receiver, offset):
    """C: one corrupted byte must invalidate the frame.

    The checksum covers bytes 0..51, so flipping a single bit anywhere in
    that range must be detected.
    """
    frame = bytearray(_frame_with_checksum(controller, buttons_low=0x03,
                                            dpad=0x01))
    assert frame[OFF_CHECKSUM] != 0, "the test frame must carry a checksum"
    frame[offset] ^= 0x01
    assert not receiver.is_valid_frame(bytes(frame)), \
        f"a frame with a corrupted byte {offset} was accepted"


def test_a_corrupted_checksum_byte_is_rejected(controller, receiver):
    """C: the checksum byte itself is protected (a flipped checksum fails)."""
    frame = bytearray(_frame_with_checksum(controller, buttons_low=0x03))
    assert frame[OFF_CHECKSUM] != 0
    frame[OFF_CHECKSUM] ^= 0x01
    if frame[OFF_CHECKSUM] == 0:
        # Keep the checksum non-zero so that the corruption is
        # unambiguous (a checksum of 0 is protected as well, but then the
        # test would no longer prove that the *checksum byte* is checked).
        frame[OFF_CHECKSUM] ^= 0x02
    assert not receiver.is_valid_frame(bytes(frame))


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_round_trip_preserves_the_whole_state(controller, receiver):
    """The wire format must not lose anything on the way through.

    Buttons, D-pad, triggers and sticks have to survive
    ``to_bytes() -> StateParser.parse()`` exactly.
    """
    frame = _make_frame(
        controller,
        buttons_low=0x81, buttons_high=0x05, dpad=0x09,
        lt=200, rt=50,
        lthumb_x=1000, lthumb_y=60000, rthumb_x=0, rthumb_y=65535,
        battery=42, charging=True,
        mac="aa:bb:cc:dd:ee:ff", name="Xbox Wireless Co",
    )
    parsed = receiver.StateParser.parse(frame)
    assert parsed is not None, "the Windows side rejected a valid frame"
    assert parsed["buttons_low"] == 0x81
    assert parsed["buttons_high"] == 0x05
    assert parsed["dpad"] == 0x09
    assert parsed["lt"] == 200
    assert parsed["rt"] == 50
    assert parsed["lthumb_x"] == 1000
    assert parsed["lthumb_y"] == 60000
    assert parsed["rthumb_x"] == 0
    assert parsed["rthumb_y"] == 65535
    assert parsed["battery"] == 42
    assert parsed["charging"] is True
    assert parsed["controller_mac"] == "aa:bb:cc:dd:ee:ff"
    assert parsed["controller_name"] == "Xbox Wireless Co"


def test_round_trip_keeps_the_buttons_and_the_dpad_separate(controller,
                                                            receiver):
    """The D-pad must not bleed into the button bits (and vice versa)."""
    frame = _make_frame(controller, buttons_low=0x03, dpad=0x03)  # UP|RIGHT
    parsed = receiver.StateParser.parse(frame)
    assert parsed["buttons_low"] == 0x03
    assert parsed["buttons_high"] == 0
    assert parsed["dpad"] == 0x03


def test_a_ping_frame_is_never_parsed_as_a_state(receiver):
    """C: the all-0xFF keepalive must not be decoded as controller input."""
    assert receiver.StateParser.parse(b"\xff" * PAYLOAD_SIZE) is None


# ---------------------------------------------------------------------------
# C — the receiver reacts to a corrupt frame
# ---------------------------------------------------------------------------

def test_a_frame_with_a_wrong_checksum_is_not_parsed(controller, receiver):
    """C: a frame whose checksum does not match is never turned into a state."""
    frame = bytearray(_frame_with_checksum(controller, buttons_low=0xFF))
    frame[OFF_CHECKSUM] = (frame[OFF_CHECKSUM] + 1) & 0xFF
    assert receiver.StateParser.parse(bytes(frame)) is None


def test_the_receiver_skips_a_corrupt_frame_and_recovers(receiver, controller):
    """C: a corrupt frame is skipped, the next valid frame is delivered."""
    states: list[dict] = []
    r = receiver.TCPReceiver("127.0.0.1", 0, on_state=states.append)
    r._running = True

    corrupt = bytearray(_frame_with_checksum(controller, buttons_low=0xFF))
    corrupt[OFF_CHECKSUM] = (corrupt[OFF_CHECKSUM] + 1) & 0xFF
    good = _make_frame(controller, buttons_low=0x02)

    from fakes import ScriptedSocket
    sock = ScriptedSocket(bytes(corrupt) + good, chunk_size=16)
    r._conn = sock
    r._conn_generation = 1
    r._serve(sock, ("127.0.0.1", 12345), 1)

    for state in states:
        assert state["buttons_low"] != 0xFF, \
            "the corrupt frame was reported as a state — ghost buttons!"
    assert [s["buttons_low"] for s in states] == [0x02], \
        f"the receiver delivered {states} instead of the one valid frame — " \
        "a misaligned window was accepted"


def test_a_shifted_window_is_never_delivered_as_a_state(receiver, controller):
    """C: the receiver must never deliver a misaligned window.

    The window the receiver latches onto when it re-syncs byte by byte is
    shifted by one byte: its checksum byte is an ordinary data byte and
    happens to be 0.  A receiver that used the *value* of the checksum to
    decide whether the checksum is present would skip the comparison and
    accept the window — the game would then see a "Start" press that
    nobody made, the exact symptom the whole checksum was added for.

    The checksum is announced by flag bit 3 now, so the window is
    rejected because its checksum does not match — the value 0 is
    irrelevant.
    """
    good = _make_frame(controller, buttons_low=0x02)
    corrupt = bytearray(_frame_with_checksum(controller, buttons_low=0xFF))
    corrupt[OFF_CHECKSUM] = (corrupt[OFF_CHECKSUM] + 1) & 0xFF
    stream = bytes(corrupt) + good

    # The window the receiver latches onto when it re-syncs byte by byte.
    shifted = stream[51:51 + PAYLOAD_SIZE]
    assert shifted[OFF_CHECKSUM] == 0, \
        "the test setup changed — the shifted window no longer has a zero " \
        "checksum byte"

    states: list[dict] = []
    r = receiver.TCPReceiver("127.0.0.1", 0, on_state=states.append)
    r._running = True

    from fakes import ScriptedSocket
    sock = ScriptedSocket(stream, chunk_size=16)
    r._conn = sock
    r._conn_generation = 1
    r._serve(sock, ("127.0.0.1", 12345), 1)

    for state in states:
        assert state["buttons_low"] != 0x80, \
            "a misaligned window was delivered as a state — the game sees " \
            "a button press nobody made"
    assert [s["buttons_low"] for s in states] == [0x02], \
        f"the receiver delivered {states} instead of the one valid frame"


def test_a_shifted_window_is_rejected(controller, receiver):
    """C: the classic misalignment — a window that starts one byte early.

    This is what a frame that was cut short on the wire looks like to the
    receiver: every following frame is read at the wrong offset.
    """
    first = _make_frame(controller, buttons_low=0x01)
    second = _make_frame(controller, buttons_low=0x02)
    stream = first + second

    shifted = stream[1:1 + PAYLOAD_SIZE]
    assert not receiver.is_valid_frame(shifted), \
        "a misaligned window was accepted as a state frame"



def test_a_zero_checksum_is_still_protected(controller, receiver):
    """The checksum is announced by a flag, not by its value.

    A frame whose checksum happens to be 0 is still verified: the sender
    announces the checksum with flag bit 3, so there is no "0 means
    unchecked" hole any more.
    """
    frame = None
    for buttons_low in range(256):
        candidate = _make_frame(controller, buttons_low=buttons_low)
        if candidate[OFF_CHECKSUM] == 0:
            frame = candidate
            break
    assert frame is not None, "no state with a zero checksum could be built"
    assert frame[OFF_FLAGS] & FLAG_CHECKSUM, \
        "the sender must announce the checksum"

    # The frame is accepted — and a corrupted copy of it is rejected.
    assert receiver.is_valid_frame(frame)
    corrupted = bytearray(frame)
    corrupted[OFF_BUTTONS_LOW] ^= 0xFF
    assert not receiver.is_valid_frame(bytes(corrupted)), \
        "a corrupted frame with a zero checksum was accepted"

