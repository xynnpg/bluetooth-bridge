"""Reads Xbox controller input via evdev and produces 32-byte state packets."""

from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass, field

import evdev

logger = logging.getLogger("controller")

# Wire protocol version
PROTO_VERSION   = 0x02
PAYLOAD_SIZE    = 54
BATTERY_UNKNOWN = 0xFF
NAME_FIELD_LEN  = 16

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
        15       1    flags      uint8 (bit0=charging, bit1=battery_valid)
        16       1    rumble_l   uint8
        17       1    rumble_r   uint8
        18-23   6    mac        6 raw bytes (AA BB CC DD EE FF)
        24-39  16    name       ASCII, NUL-padded
        40-53  14    padding    0x00
        """
        dpad = _dpad_bitmask(self._hat_x, self._hat_y)
        flags = 0
        if self.charging:
            flags |= 0x01
        if self.battery != BATTERY_UNKNOWN:
            flags |= 0x02
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
        pkt += b"\x00" * 14  # trailing pad
        assert len(pkt) == PAYLOAD_SIZE, f"packet is {len(pkt)} bytes, expected {PAYLOAD_SIZE}"
        return pkt

    @staticmethod
    def from_bytes(data: bytes) -> "ControllerState | None":
        """Deserialise a 54-byte v2 packet (inverse of to_bytes)."""
        if data[:1] == b"\xff" or data == b"\xff" * PAYLOAD_SIZE:
            return None  # keepalive PING
        if len(data) < 18:
            return None
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
    lo   = abs_info.min
    hi   = abs_info.max
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
    lo   = abs_info.min if abs_info is not None else 0
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
    """Create a ControllerState pre-seeded with the kernel's current axis values.

    Sticks are seeded from the kernel-cached value (so a held stick doesn't
    snap to centre on the first packet).  Triggers are always initialised to 0
    because the kernel-cached value at BT connect time is unreliable and often
    non-zero even when the physical trigger is fully released.
    """
    state = ControllerState()
    caps = device.capabilities()
    for code, infos in caps.get(evdev.ecodes.EV_ABS, []):
        if code not in _ABS_AXES:
            continue
        if code not in _STICK_CODES:
            # Triggers: always start at 0 — don't trust the kernel cache
            continue
        field_name = _ABS_AXES[code]
        try:
            val = device.absinfo(code).value
        except Exception:
            continue
        ai = abs_info.get(code, infos)
        setattr(state, field_name, _norm_stick(val, ai))
    return state


# ---------------------------------------------------------------------------
# Main event-loop reader  (PERSISTENT STATE — mutates state in place)
# ---------------------------------------------------------------------------

def read_next_state(
    device:   evdev.InputDevice,
    abs_info: dict,
    state:    ControllerState,
) -> ControllerState:
    """Block until the next EV_SYN, update *state* in-place, return it.

    The caller must pass the SAME state object on every call so that
    axis values and button states carry over between event batches.
    """
    while True:
        event = device.read_one()
        if event is None:
            continue  # non-blocking — spin until data arrives

        if event.type == evdev.ecodes.EV_SYN:
            # Only deliver on SYN_REPORT (code 0), skip SYN_DROPPED etc.
            if event.code == evdev.ecodes.SYN_REPORT:
                return state
            continue

        if event.type == evdev.ecodes.EV_ABS:
            _update_abs(state, event, abs_info)
        elif event.type == evdev.ecodes.EV_KEY:
            _update_key(state, event)
        elif event.type == evdev.ecodes.EV_MSC:
            _update_msc(state, event)


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
        logger.debug("ABS  code=%-4d %-12s raw=%-6d → %d",
                     code, field_name, val, new_val)
        setattr(state, field_name, new_val)
        return

    if code == _HATX:
        logger.debug("HAT  X=%d", val)
        state._hat_x = val
    elif code == _HATY:
        logger.debug("HAT  Y=%d", val)
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
        return

    # --- Digital trigger fallback (some firmware sends key events instead of
    #     analog ABS axes for the triggers) ------------------------------------
    if code == evdev.ecodes.BTN_TL2:
        state.lt = 255 if val else 0
        logger.debug("LT   digital val=%d", val)
        return

    if code == evdev.ecodes.BTN_TR2:
        state.rt = 255 if val else 0
        logger.debug("RT   digital val=%d", val)
        return

    # --- Thumbstick clicks (L3 / R3) in buttons_high -------------------------
    if code == evdev.ecodes.BTN_THUMBL:
        mask = 1 << 0
        state.buttons_high = (state.buttons_high & ~mask & 0xFF) | (mask if val else 0)
        logger.debug("L3   val=%d", val)
        return

    if code == evdev.ecodes.BTN_THUMBR:
        mask = 1 << 1
        state.buttons_high = (state.buttons_high & ~mask & 0xFF) | (mask if val else 0)
        logger.debug("R3   val=%d", val)
        return

    # --- Guide / Xbox button in buttons_high ---------------------------------
    if code == evdev.ecodes.BTN_MODE:
        mask = 1 << 2
        state.buttons_high = (state.buttons_high & ~mask & 0xFF) | (mask if val else 0)
        logger.debug("GUIDE val=%d", val)
        return

    # --- Regular face / shoulder / menu buttons in buttons_low ---------------
    if code in _BTN_MAP:
        bit  = _BTN_MAP[code]
        mask = 1 << bit
        state.buttons_low = (state.buttons_low & ~mask & 0xFF) | (mask if val else 0)
        logger.debug("BTN  bit=%d val=%d (code=%d)", bit, val, code)
        return

    # --- Unknown / unmapped — log so missing inputs are easy to identify ------
    logger.debug("KEY  code=%d val=%d (UNMAPPED)", code, val)


# ---------------------------------------------------------------------------
# Battery / charging (EV_MSC + ABS_MISC fallback)
# ---------------------------------------------------------------------------

# Battery levels per Xbox One Input Report spec
# Source: https://learn.microsoft.com/en-us/windows-hardware/design/component-guidelines/xbox-one-controller-input-report
# Byte 14 of the 17-byte report:  (level << 4) | status
#   level:  0=empty, 1=full, 2=medium, 3=low, 4=critical
#   status: bit 0=connected, bit 1=charging
_BATTERY_LEVEL_PCT = {0: 5, 1: 90, 2: 60, 3: 30, 4: 10}


def _update_msc(state: ControllerState, event) -> None:
    """Parse EV_MSC / MSC_INPUT for the Xbox One 17-byte input report.

    The `hid-microsoft` driver pushes the full report through this event;
    we extract battery level + charging from byte 14.
    """
    if event.code != evdev.ecodes.MSC_INPUT:
        return
    report = event.value
    if not isinstance(report, (list, tuple)) or len(report) < 17:
        return
    if report[0] != 0x04:  # only the 0x04 "input" report carries battery
        return
    flags  = report[14]
    level  = (flags >> 4) & 0x0F
    charge = bool(flags & 0x02)   # bit 1 = charging
    pct    = _BATTERY_LEVEL_PCT.get(level)
    if pct is None:
        return
    old_b, old_c = state.battery, state.charging
    state.battery  = pct
    state.charging = charge
    if (old_b, old_c) != (pct, charge):
        logger.info("Battery %d%% charging=%s", pct, charge)