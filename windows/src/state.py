"""Shared live state for the Windows bridge — read by tray, dashboard, etc.

This is a deliberately simple, thread-safe holder for things the UI needs
to display (battery, peer IP, packet count, …) and a small ring buffer of
recent log lines. The hot path (receiver, emitter) writes to it; the UI
reads from it.
"""

from __future__ import annotations

import collections
import logging
import threading
import time

logger = logging.getLogger("state")

_RECENT_EVENTS_MAX = 100
_BUTTON_EVENTS_MAX = 200
_BUTTON_NAMES = {0: "A", 1: "B", 2: "X", 3: "Y", 4: "LB", 5: "RB", 6: "Back", 7: "Start"}
_HIGH_BUTTON_NAMES = {0: "L3", 1: "R3", 2: "Guide"}
_DPAD_NAMES = {0x01: "D-pad Up", 0x02: "D-pad Right", 0x04: "D-pad Down", 0x08: "D-pad Left"}


class BridgeState:
    """Thread-safe live state, read by the tray menu / dashboard."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._start_time = time.monotonic()
        self._reset()

    def _reset(self) -> None:
        self.connected       = False
        self.pc_reachable    = False
        self.peer_ip         = ""
        self.battery         = -1   # -1 = unknown
        self.charging        = False
        self.controller_mac  = ""
        self.controller_name = ""
        self.rumble_left     = 0
        self.rumble_right    = 0
        self.packets_sent    = 0
        self.last_latency_ms = -1
        self.last_packet_ms  = -1
        self.packet_rate     = 0.0
        self.buttons_low     = 0
        self.buttons_high    = 0
        self.dpad            = 0
        self.last_event_ts   = ""
        self.recent_events: collections.deque = collections.deque(maxlen=_RECENT_EVENTS_MAX)
        self.button_events: collections.deque = collections.deque(maxlen=_BUTTON_EVENTS_MAX)
        self._last_packet_at = None

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def on_state(self, state: dict) -> None:
        """Called by BridgeApp._on_state with the parsed v2 dict."""
        with self._lock:
            now = time.monotonic()
            if self._last_packet_at is not None:
                interval = (now - self._last_packet_at) * 1000
                self.last_packet_ms = round(interval, 1)
                self.packet_rate = 1000.0 / interval if interval > 0 else 0.0
            self._last_packet_at = now
            self.packets_sent += 1
            self.battery         = state.get("battery", self.battery)
            self.charging        = bool(state.get("charging", self.charging))
            mac  = state.get("controller_mac", "")
            name = state.get("controller_name", "")
            if mac:  self.controller_mac  = mac
            if name: self.controller_name = name
            rl = state.get("rumble_left", 0)
            rr = state.get("rumble_right", 0)
            if rl is not None: self.rumble_left  = rl
            if rr is not None: self.rumble_right = rr
            old_low, old_high, old_dpad = self.buttons_low, self.buttons_high, self.dpad
            self.buttons_low = int(state.get("buttons_low", 0))
            self.buttons_high = int(state.get("buttons_high", 0))
            self.dpad = int(state.get("dpad", 0))
            self._record_button_changes(old_low, self.buttons_low, old_high,
                                        self.buttons_high, old_dpad, self.dpad)

    def _record_button_changes(self, old_low: int, new_low: int,
                               old_high: int, new_high: int,
                               old_dpad: int, new_dpad: int) -> None:
        changes = []
        for bit, name in _BUTTON_NAMES.items():
            if bool(old_low & (1 << bit)) != bool(new_low & (1 << bit)):
                changes.append((name, bool(new_low & (1 << bit))))
        for bit, name in _HIGH_BUTTON_NAMES.items():
            if bool(old_high & (1 << bit)) != bool(new_high & (1 << bit)):
                changes.append((name, bool(new_high & (1 << bit))))
        for mask, name in _DPAD_NAMES.items():
            if bool(old_dpad & mask) != bool(new_dpad & mask):
                changes.append((name, bool(new_dpad & mask)))
        timestamp = time.strftime("%H:%M:%S")
        for name, pressed in changes:
            self.button_events.append({
                "ts": timestamp, "button": name,
                "action": "pressed" if pressed else "released",
            })

    def on_rumble(self, left: int, right: int) -> None:
        """Called by the emitter when XInput rumble arrives from the game."""
        with self._lock:
            self.rumble_left  = left
            self.rumble_right = right

    def on_connect(self, peer_ip: str) -> None:
        with self._lock:
            self.connected    = True
            self.pc_reachable = True
            self.peer_ip      = peer_ip

    def on_disconnect(self) -> None:
        with self._lock:
            old_low, old_high, old_dpad = self.buttons_low, self.buttons_high, self.dpad
            self.connected    = False
            self.pc_reachable = False
            self.buttons_low  = 0
            self.buttons_high = 0
            self.dpad         = 0
            self._record_button_changes(old_low, 0, old_high, 0, old_dpad, 0)
            # Keep peer_ip so the user can see "was connected to X" in the UI

    def add_event(self, level: str, name: str, message: str) -> None:
        """Append a short line to the dashboard's "Recent Events" list."""
        ts = time.strftime("%H:%M:%S")
        with self._lock:
            self.last_event_ts = ts
            self.recent_events.append(f"{ts}  {level:<7}  {name}: {message}")

    # ------------------------------------------------------------------
    # Snapshot — returns a plain dict the UI can read without locks
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "connected":        self.connected,
                "pc_reachable":     self.pc_reachable,
                "peer_ip":          self.peer_ip,
                "battery":          self.battery,
                "charging":         self.charging,
                "controller_mac":   self.controller_mac,
                "controller_name":  self.controller_name,
                "rumble_left":      self.rumble_left,
                "rumble_right":     self.rumble_right,
                "packets_sent":     self.packets_sent,
                "last_latency_ms":  self.last_latency_ms,
                "last_packet_ms":   self.last_packet_ms,
                "packet_rate":      round(self.packet_rate, 1),
                "buttons_low":      self.buttons_low,
                "buttons_high":     self.buttons_high,
                "dpad":             self.dpad,
                "button_events":    list(self.button_events),
                "uptime_s":         int(time.monotonic() - self._start_time),
                "last_event_ts":    self.last_event_ts,
                "recent_events":    list(self.recent_events),
            }


class _EventLogHandler(logging.Handler):
    """logging.Handler that forwards records into a BridgeState."""

    def __init__(self, state: BridgeState, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self._state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._state.add_event(
                record.levelname,
                record.name,
                self.format(record),
            )
        except Exception:
            pass
