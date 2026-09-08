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
        self.last_event_ts   = ""
        self.recent_events: collections.deque = collections.deque(maxlen=_RECENT_EVENTS_MAX)

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def on_state(self, state: dict) -> None:
        """Called by BridgeApp._on_state with the parsed v2 dict."""
        with self._lock:
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
            self.connected    = False
            self.pc_reachable = False
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
