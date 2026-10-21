"""Reads Xbox controller input via evdev and produces 54-byte state packets."""

from __future__ import annotations

import logging
import select
import struct
import time
from dataclasses import dataclass, field

import evdev

from . import trace

logger = logging.getLogger("controller")

# Wire protocol version
PROTO_VERSION   = 0x02
PAYLOAD_SIZE    = 54
BATTERY_UNKNOWN = 0xFF
NAME_FIELD_LEN  = 16

# Byte offset of the frame checksum (last byte of the reserved area).
CHECKSUM_OFFSET = 52
# Number of bytes the checksum is computed over (offsets 0..51).
CHECKSUM_SPAN   = 52

# flags byte bits
FLAG_CHARGING   = 0x01   # the battery value is meaningful
FLAG_BATTERY_OK = 0x02   # a battery percentage is known
FLAG_STREAMING  = 0x04   # "I keep streaming the state even when idle"
FLAG_CHECKSUM   = 0x08   # "byte 52 holds a valid checksum"

#: How long the reader waits for new events before handing the current
#: state back anyway.  Every frame is a complete, authoritative snapshot of
#: the controller, so streaming one every 50 ms means a frame that was lost
#: in transit is corrected by the next one instead of latching a button
#: forever.
IDLE_HEARTBEAT_S = 0.05

# ---------------------------------------------------------------------------
# EVDEV button & abs-code mappings for Xbox One / Xbox Series controllers
# ---------------------------------------------------------------------------

# Face / shoulder / menu buttons → bit position in buttons_low (bits 0-7)
_BTN_MAP: dict[int, int] = {
    evdev.ecodes.BTN_SOUTH:  0,  # A
    evdev.ecodes.BTN_EAST:   1,  # B
    evdev.ecodes.BTN_NORTH:  2,  # X
    evdev.ecodes.BTN_WEST:   3,  # Y
    evdev.ecodes.BTN_TL:     4,  # LB
    evdev.ecodes.BTN_TR:     5,  # RB
    evdev.ecodes.BTN_SELECT: 6,  # View / Back
    evdev.ecodes.BTN_START:  7,  # Menu / Start
}

# Thumbstick + trigger abs-axis codes → ControllerState field names
#
# Axis layout for this Xbox Wireless Controller over Bluetooth:
#   ABS_X     (0)  = Left  stick X
#   ABS_Y     (1)  = Left  stick Y
#   ABS_Z     (2)  = Right stick X
#   ABS_RZ    (5)  = Right stick Y
#   ABS_GAS   (9)  = Right trigger (RT)
#   ABS_BRAKE (10) = Left  trigger (LT)
_ABS_AXES: dict[int, str] = {
    evdev.ecodes.ABS_X:     "lthumb_x",  # Left  stick X
    evdev.ecodes.ABS_Y:     "lthumb_y",  # Left  stick Y
    evdev.ecodes.ABS_Z:     "rthumb_x",  # Right stick X
    evdev.ecodes.ABS_RZ:    "rthumb_y",  # Right stick Y
    evdev.ecodes.ABS_GAS:   "rt",         # Right trigger (ABS_GAS  = code 9)
    evdev.ecodes.ABS_BRAKE: "lt",         # Left  trigger (ABS_BRAKE = code 10)
}

# Hat switch (d-pad) axis codes
_HATX = evdev.ecodes.ABS_HAT0X
_HATY = evdev.ecodes.ABS_HAT0Y

# Human readable names for the trace log.
_BUTTON_NAMES: dict[int, str] = {
    evdev.ecodes.BTN_SOUTH:   "A",
    evdev.ecodes.BTN_EAST:    "B",
    evdev.ecodes.BTN_NORTH:   "X",
    evdev.ecodes.BTN_WEST:    "Y",
    evdev.ecodes.BTN_TL:      "LB",
    evdev.ecodes.BTN_TR:      "RB",
    evdev.ecodes.BTN_SELECT:  "Back",
    evdev.ecodes.BTN_START:   "Start",
    evdev.ecodes.BTN_THUMBL:  "L3",
    evdev.ecodes.BTN_THUMBR:  "R3",
    evdev.ecodes.BTN_MODE:    "Guide",
    evdev.ecodes.BTN_TL2:     "LT",
    evdev.ecodes.BTN_TR2:     "RT",
    evdev.ecodes.BTN_DPAD_UP:    "DPadUp",
    evdev.ecodes.BTN_DPAD_DOWN:  "DPadDown",
    evdev.ecodes.BTN_DPAD_LEFT:  "DPadLeft",
    evdev.ecodes.BTN_DPAD_RIGHT: "DPadRight",
}

# The last value that was written to the trace for an axis — a stick
# reports hundreds of events per second, only the interesting ones are
# logged.
_last_traced_axis: dict[str, int] = {}

# Axes that use _norm_stick (signed → unsigned 0-65535).
# Everything else in _ABS_AXES uses _norm_trigger (0-based → 0-255).
_STICK_CODES = frozenset({
    evdev.ecodes.ABS_X,    # Left  stick X
    evdev.ecodes.ABS_Y,    # Left  stick Y
    evdev.ecodes.ABS_Z,    # Right stick X
    evdev.ecodes.ABS_RZ,   # Right stick Y
})

# D-pad as EV_KEY codes (some firmware / hid mappings use these instead of hats)
_DPAD_KEY_MAP: dict[int, tuple[str, int]] = {
    evdev.ecodes.BTN_DPAD_UP:    ("_hat_y", -1),
    evdev.ecodes.BTN_DPAD_DOWN:  ("_hat_y",  1),
    evdev.ecodes.BTN_DPAD_LEFT:  ("_hat_x", -1),
    evdev.ecodes.BTN_DPAD_RIGHT: ("_hat_x",  1),
}

# ---------------------------------------------------------------------------
# Wire protocol constants
# ---------------------------------------------------------------------------

# Dpad bitmask (byte 12 of packet)
DPAD_UP    = 0x01
DPAD_RIGHT = 0x02
DPAD_DOWN  = 0x04
DPAD_LEFT  = 0x08

# ---------------------------------------------------------------------------
# State dataclass
# ---------------------------------------------------------------------------

@dataclass
class ControllerState:
    lthumb_x:     int = 32768  # 0-65535, centre = 32768
    lthumb_y:     int = 32768
    rthumb_x:     int = 32768
    rthumb_y:     int = 32768
    lt:           int = 0      # 0-255
    rt:           int = 0      # 0-255
    buttons_low:  int = 0      # bits 0-7: A B X Y LB RB Back Start
    buttons_high: int = 0      # bits 0=L3  1=R3  2=Guide

    # ── v2 additions ──────────────────────────────────────────────────
    battery:      int = BATTERY_UNKNOWN  # 0-100, 0xFF = unknown
    charging:     bool = False
    rumble_left:  int = 0  # 0-255 (set by host)
    rumble_right: int = 0  # 0-255 (set by host)
    controller_mac:  str = ""            # "aa:bb:cc:dd:ee:ff" or ""
    controller_name: str = ""            # up to 16 chars, NUL-padded

    # Hat-switch tracking (set by ABS_HAT* or BTN_DPAD_* events)
    _hat_x: int = field(default=0, repr=False)
    _hat_y: int = field(default=0, repr=False)


    def to_bytes(self) -> bytes:
        """Serialise to a 54-byte v2 wire packet (little-endian).

        Offset  Size  Field
        ------  ----  -----
         0-1     2    lthumb_x   uint16
         2-3     2    lthumb_y   uint16
         4-5     2    rthumb_x   uint16
         6-7     2    rthumb_y   uint16
         8       1    lt         uint8
         9       1    rt         uint8
        10       1    buttons_low  uint8
        11       1    buttons_high uint8
        12       1    dpad bitmask (UP=0x01 RIGHT=0x02 DOWN=0x04 LEFT=0x08)
        13       1    proto      uint8 (0x02)
        14       1    battery    uint8 (0-100, 0xFF=unknown)
        15       1    flags      uint8 (bit0=charging, bit1=battery_valid,
                                           bit2=idle heartbeat,
                                           bit3=checksum present)
        16       1    rumble_l   uint8
        17       1    rumble_r   uint8
        18-23   6    mac        6 raw bytes (AA BB CC DD EE FF)
        24-39  16    name       ASCII, NUL-padded
        40-51  12    reserved   0x00
        52      1    checksum   sum(bytes 0..51) & 0xFF
                              (announced by flag bit 3 — the value may be 0)
        53      1    reserved   0x00
        """
        dpad = _dpad_bitmask(self._hat_x, self._hat_y)
        flags = 0
        if self.charging:
            flags |= FLAG_CHARGING
        if self.battery != BATTERY_UNKNOWN:
            flags |= FLAG_BATTERY_OK
        # "I keep streaming the state even when nothing happens" — the PC
        # uses this to decide whether it may watch the input stream with a
        # watchdog (an older sender only reports when the controller is
        # used, so a held button must not be released by the PC).
        flags |= FLAG_STREAMING
        # "byte 52 holds a valid checksum" — the receiver verifies every
        # frame, so a frame that was cut short on the wire can never be
        # mistaken for controller input.
        flags |= FLAG_CHECKSUM
        mac_bytes = _mac_to_bytes(self.controller_mac)
        name_bytes = _name_to_bytes(self.controller_name, NAME_FIELD_LEN)
        pkt = struct.pack(
            "<HHHHBBBBBBBBBB",
            self.lthumb_x  & 0xFFFF,
            self.lthumb_y  & 0xFFFF,
            self.rthumb_x  & 0xFFFF,
            self.rthumb_y  & 0xFFFF,
            self.lt        & 0xFF,
            self.rt        & 0xFF,
            self.buttons_low  & 0xFF,
            self.buttons_high & 0xFF,
            dpad           & 0xFF,
            PROTO_VERSION  & 0xFF,
            self.battery   & 0xFF,
            flags          & 0xFF,
            self.rumble_left  & 0xFF,
            self.rumble_right & 0xFF,
        )
        pkt += mac_bytes + name_bytes
        pkt += b"\x00" * 12                   # reserved (40..51)
        pkt += bytes((_frame_checksum(pkt),))  # checksum (52)
        pkt += b"\x00"                         # reserved (53)
        assert len(pkt) == PAYLOAD_SIZE, f"packet is {len(pkt)} bytes, expected {PAYLOAD_SIZE}"
        return pkt

    def release_all(self) -> None:
        """Let go of everything — used when the controller is gone.

        When the controller powers off or the Bluetooth link drops, the
        kernel stops sending events and the last known state would be
        reported forever: the game would keep seeing the button that was
        pressed when the link died.  Resetting the state makes the next
        frame release every button.
        """
        self.lthumb_x = 32768
        self.lthumb_y = 32768
        self.rthumb_x = 32768
        self.rthumb_y = 32768
        self.lt = 0
        self.rt = 0
        self.buttons_low = 0
        self.buttons_high = 0
        self._hat_x = 0
        self._hat_y = 0

    @staticmethod
    def from_bytes(data: bytes) -> "ControllerState | None":
        """Deserialise a 54-byte v2 packet (inverse of to_bytes).

        Returns ``None`` for a keepalive PING and for a frame that does not
        carry a valid checksum — a frame that was cut short on the wire
        must never be decoded as controller input.
        """
        if len(data) >= PAYLOAD_SIZE and data[:PAYLOAD_SIZE] == b"\xff" * PAYLOAD_SIZE:
            return None  # keepalive PING
        if len(data) < 18:
            return None
        if len(data) >= PAYLOAD_SIZE:
            flags = data[15]
            if flags & FLAG_CHECKSUM:
                if data[CHECKSUM_OFFSET] != (
                        sum(data[:CHECKSUM_SPAN]) & 0xFF):
                    return None      # corrupted / misaligned frame
        s = ControllerState()
        (s.lthumb_x, s.lthumb_y, s.rthumb_x, s.rthumb_y,
         s.lt, s.rt, s.buttons_low, s.buttons_high,
         dpad, _proto, s.battery, flags,
         s.rumble_left, s.rumble_right) = struct.unpack(
            "<HHHHBBBBBBBBBB", data[:18]
        )
        s.charging = bool(flags & 0x01)
        if not (flags & 0x02):
            s.battery = BATTERY_UNKNOWN
        if len(data) >= 18 + 6 + NAME_FIELD_LEN:
            s.controller_mac  = _bytes_to_mac(data[18:24])
            s.controller_name = _bytes_to_name(data[24:24 + NAME_FIELD_LEN])
        s._hat_x = (1 if dpad & DPAD_RIGHT else -1 if dpad & DPAD_LEFT else 0)
        s._hat_y = (-1 if dpad & DPAD_UP   else  1 if dpad & DPAD_DOWN else 0)
        return s


def _mac_to_bytes(mac: str) -> bytes:
    """Convert 'aa:bb:cc:dd:ee:ff' → 6 raw bytes. Empty/garbage → 6×0x00."""
    if not mac:
        return b"\x00" * 6
    try:
        return bytes(int(p, 16) for p in mac.split(":"))
    except ValueError:
        return b"\x00" * 6


def _bytes_to_mac(b: bytes) -> str:
    """Inverse of _mac_to_bytes."""
    if not b or all(x == 0 for x in b):
        return ""
    return ":".join(f"{x:02x}" for x in b)


def _name_to_bytes(name: str, length: int) -> bytes:
    raw = (name or "").encode("ascii", errors="replace")[:length]
    return raw.ljust(length, b"\x00")


def _bytes_to_name(b: bytes) -> str:
    try:
        return b.rstrip(b"\x00").decode("ascii", errors="replace")
    except Exception:
        return ""


def _frame_checksum(data: bytes) -> int:
    """Additive checksum over the first CHECKSUM_SPAN bytes of a frame.

    The receiver uses it to tell a real frame apart from a window that is
    shifted because a previous frame was cut short on the wire.  Whether a
    frame carries a checksum is announced by flag bit 3, not by the value
    of this byte — a checksum of 0 is a perfectly normal checksum.
    """
    return sum(data[:CHECKSUM_SPAN]) & 0xFF


def _dpad_bitmask(hat_x: int, hat_y: int) -> int:
    """Combine hat X/Y (-1/0/1) into a 4-bit bitmask for the wire packet."""
    result = 0
    if hat_y == -1: result |= DPAD_UP
    if hat_x ==  1: result |= DPAD_RIGHT
    if hat_y ==  1: result |= DPAD_DOWN
    if hat_x == -1: result |= DPAD_LEFT
    return result


# ---------------------------------------------------------------------------
# Device discovery
# ---------------------------------------------------------------------------

def find_controller() -> evdev.InputDevice:
    """Return the first detected Xbox-compatible gamepad."""
    devices = [evdev.InputDevice(p) for p in evdev.list_devices()]
    candidates: list[evdev.InputDevice] = []
    for dev in devices:
        name_lower = dev.name.lower()
        phys       = dev.phys.lower()
        if any(kw in name_lower or kw in phys
               for kw in ("xbox", "microsoft", "controller")):
            candidates.append(dev)

    if not candidates:
        raise RuntimeError(
            "No Xbox controller found. Make sure it is paired over Bluetooth "
            "and the kernel hid-xbox driver is loaded."
        )

    for dev in candidates:
        if "bluetooth" in dev.phys.lower() or dev.info.bustype == 0x0005:
            logger.info("Using controller: %s at %s", dev.name, dev.path)
            return dev

    chosen = candidates[0]
    logger.info("Using controller: %s at %s (bustype=%s)",
                chosen.name, chosen.path, hex(chosen.info.bustype))
    return chosen


# ---------------------------------------------------------------------------
# Axis-info helpers
# ---------------------------------------------------------------------------

def build_abs_info(device: evdev.InputDevice) -> dict:
    """Return {axis_code: AbsInfo} for all abs axes on *device*.

    Logs every mapped axis with its evdev code number, field name, and range
    so the startup log immediately reveals any axis mis-mapping.
    Called once at device-open time — never re-queries the kernel in the hot loop.
    """
    result: dict = {}
    caps = device.capabilities()
    logger.info("Controller ABS axis map:")
    for code, infos in caps.get(evdev.ecodes.EV_ABS, []):
        result[code] = infos
        if code in _ABS_AXES:
            field_name = _ABS_AXES[code]
            kind = "STICK  " if code in _STICK_CODES else "TRIGGER"
            logger.info("  code=%-3d  %-8s  → %-12s  min=%-7d max=%-7d flat=%d",
                        code, kind, field_name, infos.min, infos.max, infos.flat)
        else:
            name = evdev.ecodes.ABS.get(code, f"ABS_{code}")
            logger.info("  code=%-3d  (other)   → %-12s  min=%-7d max=%-7d",
                        code, name, infos.min, infos.max)
    return result


def _norm_stick(val: int, abs_info) -> int:
    """Scale a signed evdev stick axis to 0-65535 (unsigned wire format)."""
    lo   = abs_info.min if abs_info is not None else -32768
    hi   = abs_info.max if abs_info is not None else 32767
    span = hi - lo
    if span <= 0:
        return 32768
    clamped = max(lo, min(hi, val))
    return round((clamped - lo) * 65535 / span)


def _norm_trigger(val: int, abs_info) -> int:
    """Scale a trigger axis from its reported range to 0-255.

    Xbox BT firmware often reports triggers as 0-1023. Without this scaling,
    the trigger appears permanently "stuck at max" until the raw value drops
    below 255.
    """
    # A signed axis (-32768..32767) would map a resting value of 0 to
    # "half pressed", so the lower end is clamped to 0.
    lo   = max(0, abs_info.min) if abs_info is not None else 0
    hi   = abs_info.max if abs_info is not None else 1023  # safe Xbox BT default
    span = hi - lo
    if span <= 0:
        return 0
    clamped = max(lo, min(hi, val))
    return round((clamped - lo) * 255 / span)


# ---------------------------------------------------------------------------
# Persistent state factory
# ---------------------------------------------------------------------------

def make_state(device: evdev.InputDevice, abs_info: dict) -> ControllerState:
    """Create a ControllerState with all axes at neutral defaults.

    Sticks are intentionally NOT seeded from the kernel cache.  The Xbox
    Bluetooth driver often caches a stale non-centre value (typically -32768,
    i.e. hard-left / hard-up) for ABS_X and ABS_Y immediately after pairing,
    before the controller has sent any real events.  If we seed from that
    cached value the first packet shows the left stick pegged at -1.0 and the
    user sees phantom drift until they physically touch the stick.

    The same reasoning already applied to triggers ("don't trust the kernel
    cache" — see make_state prior art for triggers).  We now apply it to
    sticks too.  The first genuine evdev EV_ABS event will update the value
    within milliseconds of the bridge starting.
    """
    return ControllerState()  # lthumb_x=32768, lthumb_y=32768, rthumb_x=32768, rthumb_y=32768


# ---------------------------------------------------------------------------
# Main event-loop reader  (PERSISTENT STATE — mutates state in place)
# ---------------------------------------------------------------------------

def read_next_state(
    device:   evdev.InputDevice,
    abs_info: dict,
    state:    ControllerState,
    timeout:  float = IDLE_HEARTBEAT_S,
) -> ControllerState:
    """Block until the next EV_SYN, update *state* in-place, return it.

    The caller must pass the SAME state object on every call so that
    axis values and button states carry over between event batches.

    Uses ``select()`` on the underlying fd to block in the kernel rather
    than spin-polling ``read_one()``: the spin-loop version pegs a CPU
    core at 100% when the controller is idle and increases BT-stack
    latency.

    Two details matter for correctness:

    * **Every pending event is drained.**  The kernel hands out events in
      batches that end with ``SYN_REPORT``; reading a single event per
      ``select()`` throws the rest of the batch away — including button
      *releases*, which is exactly how "I let go of the button and it is
      still held" happens.

    * **The state is returned even when nothing happens.**  After *timeout*
      seconds without a new event the current state is handed back, so the
      caller keeps streaming authoritative frames.  A frame that was lost in
      transit is then corrected by the next one instead of latching a
      button forever.
    """
    fd = device.fd
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Idle heartbeat — the state is unchanged but still current.
            return state

        try:
            r, _, _ = select.select([fd], [], [], remaining)
        except (OSError, ValueError) as exc:
            raise OSError(f"device select failed: {exc}")
        if not r:
            continue

        # Drain the complete queue snapshot: one event per select() loses
        # the remainder of the batch.
        while True:
            event = device.read_one()
            if event is None:
                break

            if event.type == evdev.ecodes.EV_SYN:
                if event.code == evdev.ecodes.SYN_DROPPED:
                    # The kernel dropped events — our idea of what is held
                    # is stale, so re-read it from the device.
                    _resync_keys(state, device, abs_info)
                elif event.code == evdev.ecodes.SYN_REPORT:
                    return state
                continue

            if event.type == evdev.ecodes.EV_ABS:
                _update_abs(state, event, abs_info)
            elif event.type == evdev.ecodes.EV_KEY:
                _update_key(state, event)


def _current_axis_values(device) -> dict:
    """Read the *current* value of every absolute axis from the device.

    The kernel caches the axis values; after a ``SYN_DROPPED`` they are the
    only trustworthy source for what the controller is holding (a pulled
    trigger or a held D-pad direction produce no new events).

    Three sources are tried, because the available API differs between
    evdev wrappers:

    1. ``device.absinfo(code)`` — a fresh ``EVIOCGABS`` per axis
       (python-evdev).
    2. ``device.state`` — the current ``(code, value)`` pairs.
    3. ``device.capabilities()`` — may be stale or carry no value at all.
    """
    values: dict = {}

    absinfo = getattr(device, "absinfo", None)
    if callable(absinfo):
        try:
            caps = device.capabilities(absinfo=True)
            fresh: dict = {}
            for code, _info in caps.get(evdev.ecodes.EV_ABS, []):
                fresh[code] = absinfo(code).value
            if fresh:
                return fresh
        except Exception as exc:        # pragma: no cover - defensive
            logger.debug("Could not read the axis values after SYN_DROPPED: %s", exc)

    try:
        for code, value in device.state:
            values[code] = value
        if values:
            return values
    except Exception as exc:            # pragma: no cover - defensive
        logger.debug("Could not read the device state after SYN_DROPPED: %s", exc)

    try:
        caps = device.capabilities(absinfo=True)
        for code, info in caps.get(evdev.ecodes.EV_ABS, []):
            value = getattr(info, "value", None)
            if value is not None:
                values[code] = value
    except Exception as exc:            # pragma: no cover - defensive
        logger.debug("Could not read the axis capabilities after SYN_DROPPED: %s", exc)

    return values


def _resync_keys(state: ControllerState, device: evdev.InputDevice,
                 abs_info: dict | None = None) -> None:
    """Rebuild the state after evdev reported that events were lost.

    ``SYN_DROPPED`` means the kernel threw events away, so the in-memory
    state can be wrong — a button that was released while the events were
    lost would stay latched forever and every following packet would keep
    reporting it as pressed.  The kernel still knows what is held, so
    re-read it from the device instead of trusting the stale state.

    The digital buttons come from ``active_keys()``; the analog axes come
    from the values the kernel caches for the device.
    """
    abs_info = abs_info or {}

    state.buttons_low  = 0
    state.buttons_high = 0

    # The analog axes keep their last value unless the device can tell us
    # the current one — zeroing them would report a held trigger or a held
    # stick as released.
    axes = _current_axis_values(device)
    if axes:
        if _HATX in axes:
            state._hat_x = axes[_HATX]
        if _HATY in axes:
            state._hat_y = axes[_HATY]
        for code, field_name in ((evdev.ecodes.ABS_BRAKE, "lt"),
                                 (evdev.ecodes.ABS_GAS,   "rt")):
            if code in axes:
                setattr(state, field_name,
                        _norm_trigger(axes[code], abs_info.get(code)))
        for code, field_name in ((evdev.ecodes.ABS_X,  "lthumb_x"),
                                 (evdev.ecodes.ABS_Y,  "lthumb_y"),
                                 (evdev.ecodes.ABS_Z,  "rthumb_x"),
                                 (evdev.ecodes.ABS_RZ, "rthumb_y")):
            if code in axes:
                setattr(state, field_name,
                        _norm_stick(axes[code], abs_info.get(code)))

    try:
        held = device.active_keys()
    except (OSError, AttributeError) as exc:
        logger.warning("Could not resync controller buttons after SYN_DROPPED: %s", exc)
        return

    logger.warning("Resynced controller state after dropped events (%d keys held)",
                   len(held))
    trace.event("RESYNC after dropped events — %d keys held", len(held))
    for code in held:
        _update_key(state, _KeyEvent(code))


class _KeyEvent:
    """A synthetic evdev key event (used when re-reading the device state)."""

    __slots__ = ("type", "code", "value")

    def __init__(self, code: int, value: int = 1) -> None:
        self.type  = evdev.ecodes.EV_KEY
        self.code  = code
        self.value = value


# ---------------------------------------------------------------------------
# Internal update helpers
# ---------------------------------------------------------------------------

def _update_abs(state: ControllerState, event, abs_info: dict) -> None:
    code = event.code
    val  = event.value

    if code in _ABS_AXES:
        field_name = _ABS_AXES[code]
        ai = abs_info.get(code)
        if code in _STICK_CODES:
            if ai is None:
                class _FB:
                    min = -32768
                    max =  32767
                ai = _FB()
            new_val = _norm_stick(val, ai)
        else:
            # Trigger — scale from device range (often 0-1023) to 0-255
            new_val = _norm_trigger(val, ai)
        # Axis events arrive at high frequency; logging every one blocks the
        # evdev loop and turns directly into packet lag. Keep this opt-in.
        logger.debug("ABS  code=%-4d %-12s raw=%-6d -> %d",
                 code, field_name, val, new_val)
        # A stick reports hundreds of events per second — only log the
        # changes that matter for a diagnosis.
        step = 8 if field_name in ("lt", "rt") else 1024
        if abs(new_val - _last_traced_axis.get(field_name, -99999)) >= step:
            _last_traced_axis[field_name] = new_val
            trace.event("AXIS %-10s raw=%-7d -> %d", field_name, val, new_val)
        setattr(state, field_name, new_val)
        return

    if code == _HATX:
        logger.debug("HAT  X=%d", val)
        if val != state._hat_x:
            trace.event("DPAD x=%d y=%d", val, state._hat_y)
        state._hat_x = val
    elif code == _HATY:
        logger.debug("HAT  Y=%d", val)
        if val != state._hat_y:
            trace.event("DPAD x=%d y=%d", state._hat_x, val)
        state._hat_y = val
    elif code == evdev.ecodes.ABS_MISC:
        # Some Xbox firmware reports battery through ABS_MISC
        # Scale linearly: most report 0-100 already, but some use 0-255
        try:
            ai = abs_info.get(code)
            hi = ai.max if ai is not None else 100
            lo = ai.min if ai is not None else 0
            span = max(1, hi - lo)
            pct = max(0, min(100, round((val - lo) * 100 / span)))
            if state.battery != pct:
                logger.info("Battery %d%% (ABS_MISC)", pct)
            state.battery = pct
        except Exception:
            pass
    else:
        logger.debug("ABS  code=%d val=%d (unmapped)", code, val)


def _update_key(state: ControllerState, event) -> None:
    code = event.code
    val  = event.value   # 1 = press, 0 = release

    # --- D-pad as EV_KEY (some hid mappings report dpad as buttons) -----------
    if code in _DPAD_KEY_MAP:
        attr, direction = _DPAD_KEY_MAP[code]
        current = getattr(state, attr)
        if val:
            setattr(state, attr, direction)
        else:
            if current == direction:
                setattr(state, attr, 0)
        logger.debug("DPAD KEY code=%d val=%d", code, val)
        trace.event("BTN %-8s %s", _BUTTON_NAMES.get(code, "DPad"),
                    "press" if val else "release")
        return

    # --- Digital trigger fallback (some firmware sends key events instead of
    #     analog ABS axes for the triggers) ------------------------------------
    if code == evdev.ecodes.BTN_TL2:
        state.lt = 255 if val else 0
        logger.debug("LT   digital val=%d", val)
        trace.event("BTN %-8s %s", "LT", "press" if val else "release")
        return

    if code == evdev.ecodes.BTN_TR2:
        state.rt = 255 if val else 0
        logger.debug("RT   digital val=%d", val)
        trace.event("BTN %-8s %s", "RT", "press" if val else "release")
        return

    # --- Thumbstick clicks (L3 / R3) in buttons_high -------------------------
    if code == evdev.ecodes.BTN_THUMBL:
        mask = 1 << 0
        state.buttons_high = (state.buttons_high & ~mask & 0xFF) | (mask if val else 0)
        logger.debug("L3   val=%d", val)
        trace.event("BTN %-8s %s", "L3", "press" if val else "release")
        return

    if code == evdev.ecodes.BTN_THUMBR:
        mask = 1 << 1
        state.buttons_high = (state.buttons_high & ~mask & 0xFF) | (mask if val else 0)
        logger.debug("R3   val=%d", val)
        trace.event("BTN %-8s %s", "R3", "press" if val else "release")
        return

    # --- Guide / Xbox button in buttons_high ---------------------------------
    if code == evdev.ecodes.BTN_MODE:
        mask = 1 << 2
        state.buttons_high = (state.buttons_high & ~mask & 0xFF) | (mask if val else 0)
        logger.debug("GUIDE val=%d", val)
        trace.event("BTN %-8s %s", "Guide", "press" if val else "release")
        return

    # --- Regular face / shoulder / menu buttons in buttons_low ---------------
    if code in _BTN_MAP:
        bit  = _BTN_MAP[code]
        mask = 1 << bit
        state.buttons_low = (state.buttons_low & ~mask & 0xFF) | (mask if val else 0)
        logger.debug("BTN  bit=%d %s (code=%d)", bit,
                "pressed" if val else "released", code)
        trace.event("BTN %-8s %s", _BUTTON_NAMES.get(code, str(code)),
                    "press" if val else "release")
        return

    # --- Unknown / unmapped — log so missing inputs are easy to identify ------
    logger.debug("KEY  code=%d val=%d (UNMAPPED)", code, val)
