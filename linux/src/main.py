"""Xbox Controller Bluetooth Bridge — Linux side entry point.

Reads Bluetooth Xbox controller events via evdev and streams normalised
state packets to the Windows PC over TCP.

Environment variables:
  PC_HOST         IP address of the Windows PC  (required)
  PC_PORT         TCP port on the PC            (default: 9999)
  CONTROLLER_MAC  Pre-known controller MAC     (optional, auto-discovers if unset)
  LOG_LEVEL       Python log level              (default: INFO)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

import evdev

from .bluetooth import ensure_paired, get_battery_pct
from .controller import (
    PAYLOAD_SIZE, BATTERY_UNKNOWN, build_abs_info, find_controller,
    make_state, read_next_state,
)
from .discovery import DiscoveryListener
from .network import TCPStreamer
from .rumble import RumbleWriter, find_hidraw_by_mac, find_hidraw_for

logger = logging.getLogger("main")


class BridgeApp:
    def __init__(self):
        self._running = False
        self._tcp: TCPStreamer | None = None
        self._device = None
        self._abs_info: dict = {}
        self._state = None          # persistent ControllerState
        self._controller_mac = os.getenv("CONTROLLER_MAC", "").strip() or None
        self._rumble = RumbleWriter()
        self._rumble_lock = threading.Lock()
        self._last_rumble = (0, 0)
        self._start_time = time.monotonic()
        self._bt_thread: threading.Thread | None = None
        self._battery_thread: threading.Thread | None = None
        self._battery_state_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._running = True
        self._register_signals()
        self._start_time = time.monotonic()

        pc_host, pc_port = self._resolve_host_port()

        logger.info("Starting Xbox Bridge — target %s:%s", pc_host, pc_port)

        # 1 — Bluetooth setup (pair / connect)
        self._setup_bluetooth()

        # 2 — Find the evdev device
        self._open_device()

        # 3 — Start TCP streamer
        self._tcp = TCPStreamer(pc_host, pc_port, on_disconnect=self._on_disconnect)
        self._tcp.start()

        # 3b — Battery poller (background thread, 1 Hz).
        # Many kernels don't surface battery state via EV_MSC, so we also
        # poll `bluetoothctl info` as a reliable fallback.
        self._battery_thread = threading.Thread(
            target=self._battery_poll_loop,
            name="BatteryPoller",
            daemon=True,
        )
        self._battery_thread.start()

        # 4 — Main poll loop
        logger.info("Bridge running — press Ctrl+C to stop")
        while self._running:
            self._poll()

        logger.info("Shutting down …")
        self._tcp.stop()
        self._rumble.close()

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def _setup_bluetooth(self) -> None:
        logger.info("Setting up Bluetooth …")
        # Run in a thread — the kernel driver handles the actual BT connection.
        # We just need the device node to exist.
        self._bt_thread = threading.Thread(
            target=ensure_paired,
            args=(self._controller_mac,),
            name="BluetoothSetup",
            daemon=True,
        )
        self._bt_thread.start()
        # Give it a few seconds before we try to open /dev/input
        time.sleep(5)

    def _open_device(self) -> None:
        logger.info("Waiting for /dev/input device …")
        for attempt in range(1, 61):  # up to 60 s
            try:
                self._device = find_controller()
                self._abs_info = build_abs_info(self._device)
                self._state = make_state(self._device, self._abs_info)
                if self._state is not None:
                    self._state.controller_mac = self._controller_mac or ""
                    self._state.controller_name = self._device.name or ""
                logger.info("Controller device: %s (%s)",
                            self._device.path, self._device.name)
                # Log whether the device exposes EV_MSC so users can see why
                # battery is or isn't coming through that path.
                try:
                    caps = self._device.capabilities()
                    has_msc = evdev.ecodes.EV_MSC in caps
                    logger.info("EV_MSC available: %s (battery will %s)",
                                has_msc,
                                "use MSC events" if has_msc
                                else "fall back to bluetoothctl polling")
                except Exception:
                    pass
                self._open_rumble()
                return
            except RuntimeError:
                pass
            time.sleep(1)

        raise RuntimeError(
            "Controller device not found after 60 s. "
            "Ensure the controller is paired and powered on."
        )

    def _open_rumble(self) -> None:
        """Locate and open the hidraw node that backs the current evdev device."""
        hidraw = find_hidraw_for(self._device.path)
        if not hidraw and self._controller_mac:
            hidraw = find_hidraw_by_mac(self._controller_mac)
        if hidraw:
            with self._rumble_lock:
                self._rumble.set_device(hidraw)
        else:
            logger.warning(
                "No hidraw node found for %s — vibration disabled. "
                "Mount /dev/hidraw into the container to enable it.",
                self._device.path,
            )


    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        # Skip until the device is open (during reconnect retries)
        if self._device is None or self._state is None:
            time.sleep(0.5)
            return
        try:
            state = read_next_state(self._device, self._abs_info, self._state)
            # Apply host-driven rumble to the controller
            self._maybe_rumble(state)
            self._debug_log_state(state)
            if self._tcp.send(state.to_bytes()):
                return  # sent ok
            # If send failed, sleep and retry until reconnected
            time.sleep(0.5)
        except OSError as exc:
            logger.error("Device read error: %s", exc)
            self._reconnect_device()
        except AttributeError as exc:
            # Device handle vanished (e.g. controller was unplugged) — treat
            # as a transient reconnect situation rather than a fatal crash.
            logger.error("Device handle lost: %s — will reconnect", exc)
            self._reconnect_device()

    def _maybe_rumble(self, state) -> None:
        rl, rr = state.rumble_left, state.rumble_right
        if (rl, rr) == self._last_rumble:
            return
        self._last_rumble = (rl, rr)
        with self._rumble_lock:
            self._rumble.apply(rl, rr)

    _debug_last_log = 0.0
    _debug_last_buttons = (0, 0, 0)

    def _debug_log_state(self, state) -> None:
        """Log buttons_low/high/dpad whenever they change. Helps diagnose
        "buttons don't work" — if the bits ARE being captured here, the
        problem is on the Windows side; if they aren't, it's the kernel.

        Throttled to 1 Hz when steady, but a transition log fires immediately."""
        now = time.monotonic()
        cur = (state.buttons_low & 0xFF, state.buttons_high & 0xFF, state.dpad & 0xFF)
        if cur != self._debug_last_buttons:
            logger.info("button change: bl=0x%02x bh=0x%02x dpad=0x%02x",
                        cur[0], cur[1], cur[2])
            self._debug_last_buttons = cur
            self._debug_last_log = now
            return
        if now - self._debug_last_log < 1.0:
            return
        self._debug_last_log = now
        if any(cur):
            logger.info("state: bl=0x%02x bh=0x%02x dpad=0x%02x rumble=(%d,%d) batt=%d%%",
                        cur[0], cur[1], cur[2],
                        state.rumble_left, state.rumble_right,
                        state.battery)

    def _battery_poll_loop(self) -> None:
        """Background poller for `bluetoothctl info` battery readings.

        Runs at 1 Hz. Only updates state.battery when the kernel MSC path
        hasn't already provided a value (it has higher priority because
        it's event-driven, lower latency, and reports charging state).

        If no MAC is configured, we auto-discover one by reading
        `bluetoothctl devices` and matching on "xbox" / "controller".
        """
        # Give the BT subsystem a moment to settle after connect
        time.sleep(2.0)
        cached_mac = (self._controller_mac or "").lower()
        while self._running:
            try:
                mac = cached_mac or self._discover_xbox_mac()
                if not mac:
                    time.sleep(2.0)
                    continue
                result = get_battery_pct(mac)
                if result is not None:
                    pct, charging = result
                    state = self._state
                    if state is None:
                        time.sleep(1.0)
                        continue
                    # Only apply if MSC hasn't already given us a value
                    with self._battery_state_lock:
                        if state.battery == BATTERY_UNKNOWN:
                            logger.info("Battery %d%% (bluetoothctl fallback)",
                                        pct)
                            state.battery = pct
                        state.charging = state.charging or charging
                        if mac and not state.controller_mac:
                            state.controller_mac = mac
            except Exception as exc:
                logger.debug("battery poll error: %s", exc)
            time.sleep(1.0)

    def _discover_xbox_mac(self) -> str:
        """Find a paired Xbox controller MAC via `bluetoothctl devices`.

        Returns "" if no Xbox device is found. Result is cached.
        """
        try:
            from .bluetooth import _runctl
            r = _runctl(["devices"], timeout=3.0)
            if r.returncode != 0:
                return ""
            for line in (r.stdout or "").splitlines():
                parts = line.split(maxsplit=2)
                if len(parts) >= 3 and len(parts[1]) == 17:
                    name = parts[2].lower()
                    if any(k in name for k in ("xbox", "controller", "microsoft")):
                        return parts[1].lower()
        except Exception as exc:
            logger.debug("discover_xbox_mac: %s", exc)
        return ""

    def _reconnect_device(self) -> None:
        """Re-open the controller device, retrying until it reappears.

        This is invoked from the main poll loop and intentionally blocks —
        the main loop's `_poll` will simply skip iterations while
        ``self._device`` is None, so we keep trying in the background
        instead of crashing the whole bridge.
        """
        logger.info("Attempting to re-open controller device …")
        with self._rumble_lock:
            self._rumble.close()
        self._device = None
        self._state  = None
        attempt = 0
        while self._running:
            attempt += 1
            try:
                dev = find_controller()
                self._abs_info = build_abs_info(dev)
                new_state = make_state(dev, self._abs_info)
                if new_state is not None:
                    new_state.controller_mac  = self._controller_mac or ""
                    new_state.controller_name = dev.name or ""
                self._device = dev
                self._state  = new_state
                logger.info("Controller reconnected at %s (attempt %d)",
                            dev.path, attempt)
                self._open_rumble()
                return
            except RuntimeError:
                pass
            except Exception as exc:
                logger.debug("reconnect attempt %d failed: %s", attempt, exc)
            time.sleep(2)
            if attempt % 15 == 0:
                logger.warning(
                    "Still waiting for controller device (%d s elapsed)…",
                    attempt * 2,
                )

    def _on_disconnect(self) -> None:
        logger.warning("Connection to PC lost — will auto-reconnect")

    # ------------------------------------------------------------------
    # Host / port resolution
    # ------------------------------------------------------------------

    def _resolve_host_port(self) -> tuple[str, int]:
        """Resolve PC_HOST and PC_PORT, using auto-discovery if PC_HOST=auto."""
        raw_host = os.environ.get("PC_HOST", "").strip()
        raw_port = int(os.environ.get("PC_PORT", "9999"))

        if raw_host.lower() == "auto":
            logger.info("PC_HOST=auto — listening for Windows discovery broadcast …")
            listener = DiscoveryListener(timeout=30.0)
            listener.start()

            # Poll until we have a result or listener exits
            while listener._running:
                time.sleep(0.5)
                result = listener.get()
                if result is not None:
                    discovered_ip, discovered_port = result
                    logger.info("Auto-discovered Windows at %s:%d", discovered_ip, discovered_port)
                    return discovered_ip, discovered_port

            logger.error(
                "Auto-discovery timed out. Set PC_HOST manually in ~/.bluetooth-bridge/.env "
                "or run install.sh again."
            )
            sys.exit(1)

        if not raw_host:
            logger.error(
                "PC_HOST environment variable is not set. "
                "Set it in ~/.bluetooth-bridge/.env or use PC_HOST=auto for auto-discovery."
            )
            sys.exit(1)

        return raw_host, raw_port

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def _register_signals(self) -> None:
        def handler(sig, _):
            logger.info("Caught signal %d — stopping", sig)
            self._running = False

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)


def main() -> None:
    """Module entry point (python -m src.main)."""
    try:
        BridgeApp().run()
    except Exception:
        logger.exception("Fatal error")
        sys.exit(1)


if __name__ == "__main__":
    main()