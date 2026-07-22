"""Xbox Controller Bluetooth Bridge — Windows side entry point.

Receives 24-byte state packets from the Linux bridge over TCP and emits a
virtual Xbox controller via ViGEmBus (vgamepad).

Install ViGEmBus first: https://github.com/ViGEm/ViGEmBus/releases

Environment variables:
  LISTEN_HOST   Bind address for TCP server  (default: 0.0.0.0)
  LISTEN_PORT   TCP port to listen on         (default: 9999)
  LOG_LEVEL     Python log level             (default: INFO)
"""

from __future__ import annotations

import atexit
import logging
import logging.handlers
import os
import sys
import threading
import time

# Hide the console window as soon as possible so the app runs silently in the
# system tray.  This works even when launched with python.exe (not pythonw.exe).
if sys.platform == "win32":
    try:
        import ctypes
        _hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if _hwnd:
            ctypes.windll.user32.ShowWindow(_hwnd, 0)  # SW_HIDE
    except Exception:
        pass

from .receiver import TCPReceiver
from .emitter  import XInputEmitter
from .tray     import TrayManager
from .discovery import DiscoveryBroadcaster

logger = logging.getLogger("main")

# Rotating log file — keeps 5 × 5 MB files = ~25 MB total
LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.environ.get("USERPROFILE", ".")), "bluetooth_bridge")
LOG_FILE = os.path.join(LOG_DIR, "bluetooth-bridge.log")

def _configure_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Clean handlers from basicConfig if any
    for h in root.handlers[:]:
        root.removeHandler(h)

    # File handler — rotate every 5 MB
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    root.addHandler(fh)
    root.addHandler(ch)


class BridgeApp:
    def __init__(self):
        self._running       = False
        self._receiver      = None
        self._emitter       = XInputEmitter(slot=0)
        self._tray          = TrayManager(
            on_exit=self.stop,
            on_restart_controller=self._restart_controller,
            on_reconnect=self._reconnect,
        )
        self._broadcaster  = DiscoveryBroadcaster()
        # Track connection status for tray
        self._controller_ok    = False
        self._pc_reachable     = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._running = True

        listener_host = os.environ.get("LISTEN_HOST", "0.0.0.0")
        listener_port = int(os.environ.get("LISTEN_PORT", "9999"))

        logger.info("Starting Xbox Bridge (Windows) — listening on %s:%s", listener_host, listener_port)

        # Attach ViGEmBus controller
        if not self._emitter.attach():
            logger.error("Could not attach ViGEmBus controller. Is ViGEmBus installed?")
            sys.exit(1)

        self._broadcaster.start()
        atexit.register(self._cleanup)

        # Wire log path into tray
        self._tray.set_log_path(LOG_FILE)

        # Start TCP receiver + tray
        self._receiver = TCPReceiver(listener_host, listener_port, on_state=self._on_state)
        self._receiver.start()
        self._tray.start()

        logger.info("Bridge running. Press the Exit tray item to stop.")
        self._wait_until_stopped()

    def stop(self) -> None:
        """Signal the app to shut down. Thread-safe."""
        logger.info("Received stop signal")
        self._running = False

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_state(self, state: dict) -> None:
        """Called on each received controller state packet."""
        # First packet means the PC connection is healthy
        if not self._pc_reachable:
            self._pc_reachable = True
            logger.info("Linux bridge connected")
            self._tray.update(connected=True, pc_reachable=True)

        self._controller_ok = True
        try:
            self._emitter.apply(state)
        except Exception as exc:
            logger.error("Emitter error: %s", exc)

        # Monitor: if we stop receiving for > 3 s, mark PC down
        self._last_recv = time.monotonic()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _wait_until_stopped(self) -> None:
        last_recv = time.monotonic()

        while self._running:
            time.sleep(0.5)

            # Check if PC connection is stale
            if self._pc_reachable:
                elapsed = time.monotonic() - last_recv
                if elapsed > 3.0 and not self._controller_ok:
                    # timeout — no new state for several seconds
                    self._pc_reachable = False
                    logger.warning("Connection to Linux bridge lost (no data for %.1f s)", elapsed)

            self._controller_ok = False

            # Refresh tray status
            self._tray.update(
                connected=self._controller_ok or self._pc_reachable,
                pc_reachable=self._pc_reachable,
            )
            last_recv = time.monotonic()

    def _cleanup(self) -> None:
        logger.info("Cleaning up …")
        self._broadcaster.stop()
        if self._receiver:
            self._receiver.stop()
        if self._tray:
            self._tray.stop()
        if self._emitter:
            self._emitter.detach()
        logger.info("Shutdown complete")

    # ------------------------------------------------------------------
    # Tray callbacks
    # ------------------------------------------------------------------

    def _restart_controller(self) -> None:
        """Detach and re-attach the virtual controller (called from tray)."""
        logger.info("Restarting virtual controller …")
        try:
            self._emitter.detach()
            time.sleep(0.2)
            self._emitter = XInputEmitter(slot=0)
            self._emitter.attach()
            logger.info("Virtual controller restarted")
        except Exception as exc:
            logger.error("Failed to restart controller: %s", exc)

    def _reconnect(self) -> None:
        """Restart the TCP receiver (called from tray)."""
        logger.info("Reconnecting TCP receiver …")
        try:
            if self._receiver:
                self._receiver.stop()
            listener_host = os.environ.get("LISTEN_HOST", "0.0.0.0")
            listener_port = int(os.environ.get("LISTEN_PORT", "9999"))
            self._receiver = TCPReceiver(listener_host, listener_port, on_state=self._on_state)
            self._receiver.start()
            logger.info("TCP receiver restarted")
        except Exception as exc:
            logger.error("Failed to reconnect: %s", exc)


def main() -> None:
    """Module entry point."""
    _configure_logging()
    # Override log level from env
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.getLogger().setLevel(getattr(logging, log_level, logging.INFO))
    try:
        BridgeApp().run()
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Fatal error")
        sys.exit(1)


if __name__ == "__main__":
    main()