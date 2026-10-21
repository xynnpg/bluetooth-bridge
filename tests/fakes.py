"""Reusable in-process fakes for the bluetooth-bridge regression suite.

Neither ``evdev`` nor ``vgamepad`` is installed in the test environment (and
neither can be: they need a real Linux input device / a real Windows
ViGEmBus driver).  Both modules are therefore replaced by fakes that expose
exactly the surface the bridge uses:

``fake evdev``
    * ``ecodes`` with the numeric constants the bridge looks up,
    * a scriptable :class:`FakeInputDevice` that yields queued events and
      reports the keys that are *currently held* (needed for the
      ``SYN_DROPPED`` resync contract).

``fake vgamepad``
    * :class:`FakePad` recording every ``press_button`` / ``release_button`` /
      ``reset`` / ``update`` call and maintaining the set of buttons that are
      currently held on the virtual pad.

      ``reset()`` deliberately does **not** clear the held-button set: the
      contract requires the Windows emitter to release buttons explicitly and
      forbids relying on ``reset()`` to do it.  A test that wants the
      "reset clears everything" model can ask for it explicitly.

Plus a couple of socket doubles used by the transport tests:

``ScriptedSocket``
    a socket whose ``recv()`` hands out a pre-baked byte script.

``PartialWriteSocket``
    a socket that accepts only *N* bytes of a frame and then fails, which is
    how a congested/stalled TCP connection behaves on the wire.
"""

from __future__ import annotations

import collections
import os
import socket
import threading
import types

# ---------------------------------------------------------------------------
# Wire protocol constants (54-byte v2 frames)
# ---------------------------------------------------------------------------

PAYLOAD_SIZE = 54
PING_PAYLOAD = b"\xff" * PAYLOAD_SIZE

# Byte offsets inside a v2 frame.
OFF_LTHUMB_X = 0
OFF_LTHUMB_Y = 2
OFF_RTHUMB_X = 4
OFF_RTHUMB_Y = 6
OFF_LT = 8
OFF_RT = 9
OFF_BUTTONS_LOW = 10
OFF_BUTTONS_HIGH = 11
OFF_DPAD = 12
OFF_PROTO = 13
OFF_BATTERY = 14
OFF_FLAGS = 15
OFF_RUMBLE_LEFT = 16
OFF_RUMBLE_RIGHT = 17
OFF_MAC = 18
OFF_NAME = 24
OFF_PADDING = 40

PROTO_VERSION = 0x02

# D-pad bit mask (byte 12).
DPAD_UP = 0x01
DPAD_RIGHT = 0x02
DPAD_DOWN = 0x04
DPAD_LEFT = 0x08

# buttons_low bit positions.
BTN_LOW_BITS = {
    "A": 0,
    "B": 1,
    "X": 2,
    "Y": 3,
    "LB": 4,
    "RB": 5,
    "Back": 6,
    "Start": 7,
}

# buttons_high bit positions.
BTN_HIGH_BITS = {
    "L3": 0,
    "R3": 1,
    "Guide": 2,
}


# ---------------------------------------------------------------------------
# evdev fake
# ---------------------------------------------------------------------------

class FakeEvent:
    """Minimal stand-in for ``evdev.InputEvent``."""

    __slots__ = ("type", "code", "value", "timestamp")

    def __init__(self, type: int, code: int, value: int) -> None:
        self.type = type
        self.code = code
        self.value = value
        self.timestamp = 0.0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"FakeEvent(type={self.type}, code={self.code}, "
                f"value={self.value})")


class FakeAbsInfo:
    """Stand-in for ``evdev.AbsInfo``."""

    __slots__ = ("value", "min", "max", "fuzz", "flat", "resolution")

    def __init__(self, min: int = 0, max: int = 0, flat: int = 0,
                 fuzz: int = 0, resolution: int = 0, value: int = 0) -> None:
        self.min = min
        self.max = max
        self.flat = flat
        self.fuzz = fuzz
        self.resolution = resolution
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"FakeAbsInfo(min={self.min}, max={self.max}, flat={self.flat})"


class AbsInfoNoValue:
    """An ``AbsInfo`` **without** a ``value`` attribute.

    Upstream ``python-evdev`` *does* carry ``value`` ("latest reported
    value for the axis"), so this class is not a model of the real
    library — it models the *stale* case: a wrapper that only knows the
    axis range.  The bridge must not rely on ``capabilities()`` for the
    current value (that snapshot is taken when the device is opened); it
    uses ``device.absinfo(code)`` instead.
    """

    __slots__ = ("min", "max", "fuzz", "flat", "resolution")

    def __init__(self, min: int = 0, max: int = 0, flat: int = 0,
                 fuzz: int = 0, resolution: int = 0) -> None:
        self.min = min
        self.max = max
        self.flat = flat
        self.fuzz = fuzz
        self.resolution = resolution

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AbsInfoNoValue(min={self.min}, max={self.max})"


def _copy_abs_info(abs_info: dict) -> dict:
    """Copy an abs_info mapping so the devices do not share the objects."""
    copied = {}
    for code, info in abs_info.items():
        if isinstance(info, AbsInfoNoValue):
            copied[code] = AbsInfoNoValue(info.min, info.max, info.flat,
                                          info.fuzz, info.resolution)
        else:
            copied[code] = FakeAbsInfo(info.min, info.max, info.flat,
                                       info.fuzz, info.resolution, info.value)
    return copied


class _Ecodes:
    """The subset of ``evdev.ecodes`` the bridge touches."""

    # event types
    EV_SYN = 0x00
    EV_KEY = 0x01
    EV_ABS = 0x03
    EV_MSC = 0x04
    EV_FF = 0x15

    # synchronisation codes
    SYN_REPORT = 0x00
    SYN_CONFIG = 0x01
    SYN_MT_REPORT = 0x02
    SYN_DROPPED = 0x03

    # absolute axes
    ABS_X = 0x00
    ABS_Y = 0x01
    ABS_Z = 0x02
    ABS_RZ = 0x05
    ABS_GAS = 0x09
    ABS_BRAKE = 0x0A
    ABS_HAT0X = 0x10
    ABS_HAT0Y = 0x11
    ABS_MISC = 0x40

    # digital buttons
    BTN_SOUTH = 0x130
    BTN_EAST = 0x131
    BTN_NORTH = 0x133
    BTN_WEST = 0x134
    BTN_TL = 0x136
    BTN_TR = 0x137
    BTN_TL2 = 0x138
    BTN_TR2 = 0x139
    BTN_SELECT = 0x13A
    BTN_START = 0x13B
    BTN_MODE = 0x13C
    BTN_THUMBL = 0x13D
    BTN_THUMBR = 0x13E

    BTN_DPAD_UP = 0x220
    BTN_DPAD_DOWN = 0x221
    BTN_DPAD_LEFT = 0x222
    BTN_DPAD_RIGHT = 0x223

    # force feedback
    FF_RUMBLE = 0x50

    # name lookup used by controller.build_abs_info()
    ABS = {
        ABS_X: "ABS_X",
        ABS_Y: "ABS_Y",
        ABS_Z: "ABS_Z",
        ABS_RZ: "ABS_RZ",
        ABS_GAS: "ABS_GAS",
        ABS_BRAKE: "ABS_BRAKE",
        ABS_HAT0X: "ABS_HAT0X",
        ABS_HAT0Y: "ABS_HAT0Y",
        ABS_MISC: "ABS_MISC",
    }

    @staticmethod
    def get(code, default=None):
        return _Ecodes.ABS.get(code, default)


#: Axis ranges of a typical Xbox controller over Bluetooth.
DEFAULT_ABS_INFO = {
    _Ecodes.ABS_X: FakeAbsInfo(min=-32768, max=32767),
    _Ecodes.ABS_Y: FakeAbsInfo(min=-32768, max=32767),
    _Ecodes.ABS_Z: FakeAbsInfo(min=-32768, max=32767),
    _Ecodes.ABS_RZ: FakeAbsInfo(min=-32768, max=32767),
    _Ecodes.ABS_GAS: FakeAbsInfo(min=0, max=1023),
    _Ecodes.ABS_BRAKE: FakeAbsInfo(min=0, max=1023),
    _Ecodes.ABS_HAT0X: FakeAbsInfo(min=-1, max=1),
    _Ecodes.ABS_HAT0Y: FakeAbsInfo(min=-1, max=1),
}


class FakeInputDevice:
    """A scriptable ``evdev.InputDevice`` double.

    Events are queued explicitly (``push`` / ``press`` / ``release``) and are
    handed to the caller by ``read_one()``.  A real pipe backs the ``fd``
    attribute so ``select.select()`` behaves exactly as it does with a real
    evdev node: the descriptor only becomes readable once an event has been
    queued.

    The device also tracks the keys that are *currently held* — that is the
    information the kernel keeps for us and that the bridge must re-read
    after a ``SYN_DROPPED`` event.
    """

    def __init__(self, path: str = "/dev/input/event3",
                 name: str = "Xbox Wireless Controller",
                 phys: str = "usb-0000:00:14.0-1.2/input0",
                 abs_info: dict | None = None,
                 held: set | None = None,
                 caps_expose_value: bool = True) -> None:
        self.path = path
        self.name = name
        self.phys = phys
        self._abs_info = _copy_abs_info(
            DEFAULT_ABS_INFO if abs_info is None else abs_info)
        self._queue: collections.deque[FakeEvent] = collections.deque()
        self._held: set[int] = set(held or ())
        # Current value of every axis (the kernel caches them).
        self._axis_values: dict[int, int] = {}
        # ``python-evdev``'s ``AbsInfo`` has no ``value`` attribute — set
        # this to False to model the real library.
        self._caps_expose_value = caps_expose_value
        self._rfd, self._wfd = os.pipe()
        os.set_blocking(self._rfd, False)
        os.set_blocking(self._wfd, False)
        self.reads = 0

    # -- evdev API ---------------------------------------------------------

    @property
    def fd(self) -> int:
        """File descriptor the bridge selects() on."""
        return self._rfd

    def capabilities(self, absinfo: bool = True) -> dict:
        caps = {
            _Ecodes.EV_ABS: [(code, info) for code, info in self._abs_info.items()],
            _Ecodes.EV_KEY: [],
        }
        if not self._caps_expose_value:
            caps[_Ecodes.EV_ABS] = [
                (code, AbsInfoNoValue(info.min, info.max, info.flat,
                                      info.fuzz, info.resolution))
                for code, info in self._abs_info.items()
            ]
        return caps

    def read_one(self) -> FakeEvent | None:
        """Return the next queued event, or ``None`` when the queue is empty."""
        if not self._queue:
            return None
        self.reads += 1
        event = self._queue.popleft()
        self._drain_pipe(1)
        return event

    def active_keys(self) -> list[int]:
        """Codes of the keys that are currently held down (evdev API)."""
        return sorted(self._held)

    def key_active(self, code: int) -> bool:
        """True when *code* is currently held down (evdev API)."""
        return code in self._held

    @property
    def state(self) -> list[tuple[int, int]]:
        """Current device state as ``(code, value)`` pairs (evdev API).

        This is where the kernel's *current* axis values live — the
        ``capabilities()`` query only describes the ranges.
        """
        pairs = [(code, value) for code, value in sorted(self._axis_values.items())]
        pairs += [(code, 1) for code in sorted(self._held)]
        return pairs

    def close(self) -> None:
        for fd in (self._rfd, self._wfd):
            try:
                os.close(fd)
            except OSError:
                pass

    # -- test helpers ------------------------------------------------------

    def push(self, type: int, code: int, value: int) -> FakeEvent:
        """Queue a raw event."""
        event = FakeEvent(type, code, value)
        self._queue.append(event)
        try:
            os.write(self._wfd, b"\x01")
        except OSError:
            pass
        return event

    def push_key(self, code: int, value: int) -> FakeEvent:
        return self.push(_Ecodes.EV_KEY, code, value)

    def push_abs(self, code: int, value: int) -> FakeEvent:
        event = self.push(_Ecodes.EV_ABS, code, value)
        self._axis_values[code] = value
        # The kernel caches the current value of the axis and hands it out
        # with EVIOCGABS.
        info = self._abs_info.get(code)
        if info is not None:
            info.value = value
        return event

    def set_axis_value(self, code: int, value: int) -> None:
        """Set the cached value of an axis without queueing an event."""
        self._axis_values[code] = value

    def push_syn(self, code: int = _Ecodes.SYN_REPORT) -> FakeEvent:
        return self.push(_Ecodes.EV_SYN, code, 0)

    def sync(self) -> FakeEvent:
        """Queue an ``EV_SYN / SYN_REPORT`` (end of an event batch)."""
        return self.push_syn(_Ecodes.SYN_REPORT)

    def press(self, code: int) -> FakeEvent:
        """Physically press a key: the device now holds it and reports it."""
        self._held.add(code)
        return self.push_key(code, 1)

    def release(self, code: int) -> FakeEvent:
        """Physically release a key: the device no longer holds it."""
        self._held.discard(code)
        return self.push_key(code, 0)

    def drop_pending(self) -> int:
        """Simulate lost events: forget everything that was queued.

        The keys that are currently held stay correct — that is exactly the
        situation the kernel creates when it reports ``SYN_DROPPED``.
        """
        dropped = len(self._queue)
        self._queue.clear()
        self._drain_pipe(4096)
        return dropped

    def held_keys(self) -> set[int]:
        """The set of key codes that are currently held down."""
        return set(self._held)

    # -- internals ---------------------------------------------------------

    def _drain_pipe(self, size: int) -> None:
        try:
            os.read(self._rfd, size)
        except OSError:
            pass


def make_fake_evdev() -> types.ModuleType:
    """Build a fake ``evdev`` module."""
    module = types.ModuleType("evdev")
    module.ecodes = _Ecodes
    module.InputDevice = FakeInputDevice
    module.list_devices = lambda: []

    ff = types.ModuleType("evdev.ff")
    ff.Effect = type("Effect", (), {})
    ff.EffectType = type("EffectType", (), {})
    ff.Trigger = type("Trigger", (), {})
    ff.Replay = type("Replay", (), {})
    ff.Rumble = type("Rumble", (), {})
    module.ff = ff

    def new_device(**kwargs) -> FakeInputDevice:
        device = FakeInputDevice(**kwargs)
        module.devices.append(device)
        return device

    module.devices = []
    module.new_device = new_device
    return module


# ---------------------------------------------------------------------------
# vgamepad fake
# ---------------------------------------------------------------------------

class _XUSB_BUTTON:
    XUSB_GAMEPAD_A = 0x1000
    XUSB_GAMEPAD_B = 0x1001
    XUSB_GAMEPAD_X = 0x1002
    XUSB_GAMEPAD_Y = 0x1003
    XUSB_GAMEPAD_LEFT_SHOULDER = 0x1004
    XUSB_GAMEPAD_RIGHT_SHOULDER = 0x1005
    XUSB_GAMEPAD_BACK = 0x1006
    XUSB_GAMEPAD_START = 0x1007
    XUSB_GAMEPAD_LEFT_THUMB = 0x1008
    XUSB_GAMEPAD_RIGHT_THUMB = 0x1009
    XUSB_GAMEPAD_GUIDE = 0x100A
    XUSB_GAMEPAD_DPAD_UP = 0x1010
    XUSB_GAMEPAD_DPAD_RIGHT = 0x1011
    XUSB_GAMEPAD_DPAD_DOWN = 0x1012
    XUSB_GAMEPAD_DPAD_LEFT = 0x1013


#: Reverse lookup used by the end-to-end tests ("A", "LEFT_SHOULDER", …).
XUSB_BUTTON_NAMES = {
    value: name[len("XUSB_GAMEPAD_"):]
    for name, value in vars(_XUSB_BUTTON).items()
    if name.startswith("XUSB_")
}


class FakePad:
    """Records every vgamepad call made by the emitter.

    ``held`` is the authoritative "what does the game see right now" set: a
    button is in it after ``press_button()`` and gone after
    ``release_button()``.

    ``reset_clears_buttons`` models the (unsafe) assumption that
    ``pad.reset()`` implicitly releases everything.  It defaults to
    ``False`` so that a test fails when the emitter relies on ``reset()``
    instead of issuing explicit ``release_button()`` calls.
    """

    def __init__(self, reset_clears_buttons: bool = False) -> None:
        self.reset_clears_buttons = reset_clears_buttons
        self.presses: list[int] = []
        self.releases: list[int] = []
        self.trigger_calls: list[tuple[str, float]] = []
        self.joystick_calls: list[tuple[str, float, float]] = []
        self.updates = 0
        self.resets = 0
        self.held: set[int] = set()
        self.trigger_values = {"lt": 0.0, "rt": 0.0}
        self.joystick_values = {"l": (0.0, 0.0), "r": (0.0, 0.0)}
        self.notification = None
        self._lock = threading.Lock()

    # -- vgamepad API ------------------------------------------------------

    def press_button(self, button: int) -> None:
        with self._lock:
            self.presses.append(button)
            self.held.add(button)

    def release_button(self, button: int) -> None:
        with self._lock:
            self.releases.append(button)
            self.held.discard(button)

    def left_trigger_float(self, value_float: float) -> None:
        with self._lock:
            self.trigger_calls.append(("lt", value_float))
            self.trigger_values["lt"] = value_float

    def right_trigger_float(self, value_float: float) -> None:
        with self._lock:
            self.trigger_calls.append(("rt", value_float))
            self.trigger_values["rt"] = value_float

    def left_joystick_float(self, x_value_float: float, y_value_float: float) -> None:
        with self._lock:
            self.joystick_calls.append(("l", x_value_float, y_value_float))
            self.joystick_values["l"] = (x_value_float, y_value_float)

    def right_joystick_float(self, x_value_float: float, y_value_float: float) -> None:
        with self._lock:
            self.joystick_calls.append(("r", x_value_float, y_value_float))
            self.joystick_values["r"] = (x_value_float, y_value_float)

    def reset(self) -> None:
        with self._lock:
            self.resets += 1
            if self.reset_clears_buttons:
                self.held.clear()

    def update(self) -> None:
        with self._lock:
            self.updates += 1

    def register_notification(self, callback) -> None:
        self.notification = callback

    # -- helpers -----------------------------------------------------------

    def held_names(self) -> set[str]:
        """The held buttons, by name (``"A"``, ``"DPAD_UP"``, …)."""
        with self._lock:
            return {XUSB_BUTTON_NAMES.get(b, hex(b)) for b in self.held}

    def clear_calls(self) -> None:
        with self._lock:
            self.presses.clear()
            self.releases.clear()


def make_fake_vgamepad(reset_clears_buttons: bool = False) -> types.ModuleType:
    """Build a fake ``vgamepad`` module."""
    module = types.ModuleType("vgamepad")
    module.XUSB_BUTTON = _XUSB_BUTTON
    module.pads: list[FakePad] = []

    def make_pad() -> FakePad:
        pad = FakePad(reset_clears_buttons=reset_clears_buttons)
        module.pads.append(pad)
        return pad

    module.VX360Gamepad = make_pad
    return module


# ---------------------------------------------------------------------------
# Socket doubles
# ---------------------------------------------------------------------------

class ScriptedSocket:
    """A socket whose ``recv()`` hands out a pre-baked byte script."""

    def __init__(self, script: bytes = b"", chunk_size: int = 16) -> None:
        self.script = script
        self.chunk_size = chunk_size
        self._pos = 0
        self.sent = bytearray()
        self.closed = False
        self.shutdown_called = False
        self.timeouts: list = []

    # -- socket API --------------------------------------------------------

    def setsockopt(self, *_args) -> None:
        pass

    def settimeout(self, timeout) -> None:
        self.timeouts.append(timeout)

    def recv(self, _size: int) -> bytes:
        if self._pos >= len(self.script):
            return b""
        chunk = self.script[self._pos:self._pos + self.chunk_size]
        self._pos += len(chunk)
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def send(self, data: bytes) -> int:
        self.sent += data
        return len(data)

    def shutdown(self, _how) -> None:
        self.shutdown_called = True

    def close(self) -> None:
        self.closed = True


class PartialWriteSocket:
    """A socket whose *first* write is cut short after *accept_bytes*.

    This models a congested TCP connection: the kernel accepted part of the
    frame, then the send timed out.  Everything that reached the peer is in
    ``written``.

    Writes that happen *after* the failure succeed, because the connection
    is still open — that is exactly the situation that corrupts the stream
    when the sender does not tear the connection down.
    """

    def __init__(self, accept_bytes: int = 30,
                 exc: BaseException | None = None) -> None:
        self.accept_bytes = accept_bytes
        self.exc = exc if exc is not None else socket.timeout("timed out")
        self.written = bytearray()
        self.closed = False
        self.shutdown_called = False
        self.timeouts: list = []
        self.calls = 0
        self.failed = False
        self._lock = threading.Lock()

    # -- socket API --------------------------------------------------------

    def setsockopt(self, *_args) -> None:
        pass

    def settimeout(self, timeout) -> None:
        self.timeouts.append(timeout)

    def sendall(self, data: bytes) -> None:
        with self._lock:
            self.calls += 1
            if not self.failed:
                self.failed = True
                room = max(0, self.accept_bytes - len(self.written))
                self.written += data[:room]
                raise self.exc
            self.written += data

    def send(self, data: bytes) -> int:
        with self._lock:
            self.calls += 1
            if not self.failed:
                self.failed = True
                room = max(0, self.accept_bytes - len(self.written))
                self.written += data[:room]
                raise self.exc
            self.written += data
            return len(data)

    def recv(self, _size: int) -> bytes:
        return b""

    def shutdown(self, _how) -> None:
        self.shutdown_called = True

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def make_state(**overrides) -> dict:
    """A neutral state dict, shaped exactly like ``StateParser.parse()``."""
    state = {
        "lthumb_x": 32768,
        "lthumb_y": 32768,
        "rthumb_x": 32768,
        "rthumb_y": 32768,
        "lt": 0,
        "rt": 0,
        "buttons_low": 0,
        "buttons_high": 0,
        "dpad": 0,
        "battery": -1,
        "charging": False,
        "rumble_left": 0,
        "rumble_right": 0,
        "controller_mac": "",
        "controller_name": "",
    }
    state.update(overrides)
    return state


def button_names_from_state(state: dict) -> set[str]:
    """Human readable names of the buttons held in a parsed state dict."""
    names: set[str] = set()
    for name, bit in BTN_LOW_BITS.items():
        if state.get("buttons_low", 0) & (1 << bit):
            names.add(name)
    for name, bit in BTN_HIGH_BITS.items():
        if state.get("buttons_high", 0) & (1 << bit):
            names.add(name)
    dpad = state.get("dpad", 0)
    if dpad & DPAD_UP:
        names.add("DPAD_UP")
    if dpad & DPAD_RIGHT:
        names.add("DPAD_RIGHT")
    if dpad & DPAD_DOWN:
        names.add("DPAD_DOWN")
    if dpad & DPAD_LEFT:
        names.add("DPAD_LEFT")
    return names
