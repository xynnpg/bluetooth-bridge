"""Xbox Controller Bluetooth Bridge — Windows side entry point.

Receives state packets from the Linux bridge over TCP and emits a virtual
Xbox controller via ViGEmBus (vgamepad).

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

# ── Hide console window (works with both python.exe and pythonw.exe) ────────
# With pythonw.exe there is no console so GetConsoleWindow returns 0 (safe).
# With python.exe this removes the flash before the tray appears.
if sys.platform == "win32":
    try:
        import ctypes as _ctypes
        _hwnd = _ctypes.windll.kernel32.GetConsoleWindow()
        if _hwnd:
            _ctypes.windll.user32.ShowWindow(_hwnd, 0)  # SW_HIDE
    except Exception:
        pass

from .receiver  import TCPReceiver
from .emitter   import XInputEmitter
from .tray      import TrayManager
from .discovery import DiscoveryBroadcaster

logger = logging.getLogger("main")

# Rotating log — 5 × 5 MB = ~25 MB total
LOG_DIR  = os.path.join(
    os.environ.get("LOCALAPPDATA", os.environ.get("USERPROFILE", ".")),
    "bluetooth_bridge"
)
LOG_FILE = os.path.join(LOG_DIR, "bluetooth-bridge.log")

INSTALL_DIR = os.path.join(os.environ.get("USERPROFILE", "."), "bluetooth_bridge")


def _configure_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Remove any handlers added by earlier basicConfig calls
    for h in root.handlers[:]:
        root.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                            datefmt="%H:%M:%S")

    # Rotating file handler — always active
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console handler — only when stdout is available (python.exe, not pythonw.exe)
    if sys.stdout is not None:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        root.addHandler(ch)


# ---------------------------------------------------------------------------
# Bridge application
# ---------------------------------------------------------------------------

class BridgeApp:
    def __init__(self) -> None:
        self._running       = False
        self._receiver: TCPReceiver | None = None
        self._emitter       = XInputEmitter(slot=0)
        self._tray          = TrayManager(
            on_exit=self.stop,
            on_restart_controller=self._restart_controller,
            on_reconnect=self._reconnect,
        )
        self._broadcaster   = DiscoveryBroadcaster()

        self._controller_ok = False
        self._pc_reachable  = False
        self._peer_ip       = ""
        self._last_recv     = time.monotonic()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the bridge and block on the tray message loop (main thread)."""
        self._running = True

        listener_host = os.environ.get("LISTEN_HOST", "0.0.0.0")
        listener_port = int(os.environ.get("LISTEN_PORT", "9999"))

        logger.info("Starting Xbox Bridge (Windows) — listening on %s:%s",
                    listener_host, listener_port)

        # Attach ViGEmBus virtual controller
        if not self._emitter.attach():
            logger.error("Could not attach ViGEmBus controller — is ViGEmBus installed?")
            sys.exit(1)

        self._broadcaster.start()
        atexit.register(self._cleanup)

        # Pass context to tray
        self._tray.set_log_path(LOG_FILE)
        self._tray.set_install_dir(INSTALL_DIR)
        self._tray.set_listen_addr(f"{listener_host}:{listener_port}")

        # Start TCP receiver
        self._receiver = TCPReceiver(listener_host, listener_port,
                                     on_state=self._on_state)
        self._receiver.start()

        # Connection monitor runs in the background
        monitor = threading.Thread(
            target=self._monitor_loop, name="Monitor", daemon=True
        )
        monitor.start()

        logger.info("Bridge running — icon in the system notification area")

        # ── Tray icon runs on the main thread (required for Win32 msg loop) ──
        self._tray.run_blocking()

        # run_blocking() returned → user clicked Exit
        logger.info("Tray exited — shutting down")

    def stop(self) -> None:
        """Signal the app to shut down (thread-safe)."""
        logger.info("Stop signal received")
        self._running = False

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_state(self, state: dict) -> None:
        """Called for every received controller state packet."""
        if not self._pc_reachable:
            self._pc_reachable = True
            logger.info("Linux bridge connected")
            self._tray.update(connected=True, pc_reachable=True,
                              peer_ip=self._peer_ip)

        self._controller_ok = True
        self._last_recv     = time.monotonic()

        try:
            self._emitter.apply(state)
        except Exception as exc:
            logger.error("Emitter error: %s", exc)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        """Periodically check connection health and update the tray icon."""
        while self._running:
            time.sleep(0.5)

            if self._pc_reachable:
                elapsed = time.monotonic() - self._last_recv
                if elapsed > 3.0 and not self._controller_ok:
                    self._pc_reachable = False
                    logger.warning("Linux bridge gone (%.1f s silence)", elapsed)

            # Reset per-cycle flag
            self._controller_ok = False

            self._tray.update(
                connected=self._pc_reachable,
                pc_reachable=self._pc_reachable,
                peer_ip=self._peer_ip,
            )

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
        logger.info("Restarting virtual controller …")
        try:
            self._emitter.detach()
            time.sleep(0.2)
            self._emitter = XInputEmitter(slot=0)
            self._emitter.attach()
            logger.info("Virtual controller restarted")
        except Exception as exc:
            logger.error("Restart controller error: %s", exc)

    def _reconnect(self) -> None:
        logger.info("Reconnecting TCP receiver …")
        try:
            if self._receiver:
                self._receiver.stop()
            host = os.environ.get("LISTEN_HOST", "0.0.0.0")
            port = int(os.environ.get("LISTEN_PORT", "9999"))
            self._receiver = TCPReceiver(host, port, on_state=self._on_state)
            self._receiver.start()
            logger.info("TCP receiver restarted on %s:%s", host, port)
        except Exception as exc:
            logger.error("Reconnect error: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _configure_logging()
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