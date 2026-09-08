"""Xbox Controller Bluetooth Bridge — Windows side entry point.

Receives state packets from the Linux bridge over TCP and emits a virtual
Xbox controller via ViGEmBus (vgamepad).  Forwards XInput rumble from games
back to the physical controller over the same TCP socket (v2 protocol).

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
import struct
import sys
import threading
import time

# ── Hide console window (works with both python.exe and pythonw.exe) ────────
if sys.platform == "win32":
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
            ctypes.windll.kernel32.FreeConsole()
    except Exception:
        pass

from .receiver     import TCPReceiver
from .emitter      import XInputEmitter
from .tray         import TrayManager
from .discovery    import DiscoveryBroadcaster
from .state        import BridgeState, _EventLogHandler

logger = logging.getLogger("main")

# Rotating log — 5 × 5 MB = ~25 MB total
LOG_DIR  = os.path.join(
    os.environ.get("LOCALAPPDATA", os.environ.get("USERPROFILE", ".")),
    "bluetooth_bridge"
)
LOG_FILE    = os.path.join(LOG_DIR, "bluetooth-bridge.log")
INSTALL_DIR = os.path.abspath(os.getcwd())   # wherever the app is installed
CONFIG_FILE = os.path.join(INSTALL_DIR, "config.ini")

# Rumble packet (v2): 14-byte state header + battery/flags/rumble bytes
_RUMBLE_PACK_FMT = "<HHHHBBBBBBBBBB"
_RUMBLE_PACK_SIZE = struct.calcsize(_RUMBLE_PACK_FMT)   # 14

# Wire-protocol version byte — MUST match linux/src/controller.py
PROTO_VERSION = 0x02
PAYLOAD_SIZE  = 54


def _configure_logging(state: BridgeState) -> None:
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

    # Forward INFO+ records into the live BridgeState for the dashboard.
    # Uses a simple "message only" formatter so the dashboard doesn't show
    # duplicate timestamps (the state already prepends its own ts).
    eb_fmt = logging.Formatter("%(message)s")
    eb = _EventLogHandler(state, level=logging.INFO)
    eb.setFormatter(eb_fmt)
    root.addHandler(eb)


# ---------------------------------------------------------------------------
# Bridge application
# ---------------------------------------------------------------------------

def _build_rumble_packet(left: int, right: int) -> bytes:
    """Build a 54-byte v2 packet carrying only rumble values.

    All other fields (sticks, triggers, buttons, identity) are zeroed.
    The Linux side reads only the rumble bytes.
    """
    flags = 0
    pkt = struct.pack(
        _RUMBLE_PACK_FMT,
        32768, 32768, 32768, 32768,  # sticks at centre
        0, 0,                         # triggers
        0, 0,                         # buttons
        0,                            # dpad
        PROTO_VERSION,
        0xFF,                         # battery unknown
        flags,
        max(0, min(255, int(left))),
        max(0, min(255, int(right))),
    )
    return pkt + b"\x00" * (PAYLOAD_SIZE - _RUMBLE_PACK_SIZE)


class BridgeApp:
    def __init__(self) -> None:
        self._running       = False
        self._state         = BridgeState()
        self._receiver: TCPReceiver | None = None
        self._emitter       = XInputEmitter(slot=0, on_rumble=self._on_rumble)
        self._tray          = TrayManager(
            on_exit=self.stop,
            on_restart_controller=self._restart_controller,
            on_reconnect=self._reconnect,
            on_open_app=self._open_dashboard,
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
        self._tray.set_config_path(CONFIG_FILE)
        self._tray.set_listen_addr(f"{listener_host}:{listener_port}")
        self._tray.set_state(self._state)

        # Start TCP receiver
        self._receiver = TCPReceiver(
            listener_host, listener_port,
            on_state=self._on_state,
            on_connect=self._on_connect,
            on_ping=self._on_ping,
            on_disconnect=self._on_disconnect,
        )
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

    def _on_connect(self, peer_ip: str) -> None:
        """Called when the Linux bridge opens a TCP connection."""
        logger.info("Linux bridge connected from %s", peer_ip)
        self._peer_ip = peer_ip
        self._pc_reachable = True
        self._last_recv = time.monotonic()
        self._state.on_connect(peer_ip)
        self._tray.update(connected=True, pc_reachable=True, peer_ip=peer_ip)

    def _on_ping(self) -> None:
        """Called when a keepalive PING frame is received from Linux."""
        if not self._pc_reachable:
            self._pc_reachable = True
            logger.info("Linux bridge keepalive received")
            self._state.on_connect(self._peer_ip)
            self._tray.update(connected=True, pc_reachable=True, peer_ip=self._peer_ip)
        self._last_recv = time.monotonic()

    def _on_disconnect(self, peer_ip: str) -> None:
        """Called when Linux disconnects the TCP connection."""
        logger.info("Linux bridge disconnected from %s", peer_ip)
        self._pc_reachable = False
        self._state.on_disconnect()
        self._tray.update(connected=False, pc_reachable=False, peer_ip=self._peer_ip)

    def _on_state(self, state: dict) -> None:
        """Called for every received controller state packet."""
        if not self._pc_reachable:
            self._pc_reachable = True
            logger.info("Linux bridge connected")
            self._state.on_connect(self._peer_ip)
            self._tray.update(connected=True, pc_reachable=True,
                              peer_ip=self._peer_ip)

        self._controller_ok = True
        self._last_recv     = time.monotonic()
        self._state.on_state(state)

        try:
            self._emitter.apply(state)
        except Exception as exc:
            logger.error("Emitter error: %s", exc)

    def _on_rumble(self, left: int, right: int) -> None:
        """Called by the emitter when XInput rumble arrives from the game.

        Forwards the new motor speeds to the Linux side so the physical
        controller vibrates.  No-op when no Linux connection is up.
        """
        self._state.on_rumble(left, right)
        if self._receiver is None:
            return
        if not self._pc_reachable:
            return
        pkt = _build_rumble_packet(left, right)
        if not self._receiver.send_state(pkt):
            logger.debug("Rumble packet dropped (no active connection)")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        """Periodically check connection health and update the tray icon."""
        while self._running:
            time.sleep(0.5)

            if self._pc_reachable:
                elapsed = time.monotonic() - self._last_recv
                if elapsed > 6.0:
                    self._pc_reachable = False
                    logger.warning("Linux bridge gone (%.1f s silence)", elapsed)

            snap = self._state.snapshot()
            self._tray.update(
                connected=self._pc_reachable,
                pc_reachable=self._pc_reachable,
                peer_ip=self._peer_ip,
                battery=snap["battery"],
                charging=snap["charging"],
                controller_name=snap["controller_name"],
                controller_mac=snap["controller_mac"],
            )

    def _open_dashboard(self) -> None:
        """Open the 'Open App' dashboard in a background thread."""
        from . import ui as _ui
        _ui.open_dashboard(
            self._state,
            log_path=LOG_FILE,
            install_dir=INSTALL_DIR,
            config_path=CONFIG_FILE,
            listen_addr=self._tray._listen_addr,
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
            self._emitter = XInputEmitter(slot=0, on_rumble=self._on_rumble)
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
            self._receiver = TCPReceiver(
                host, port,
                on_state=self._on_state,
                on_connect=self._on_connect,
                on_ping=self._on_ping,
                on_disconnect=self._on_disconnect,
            )
            self._receiver.start()
            logger.info("TCP receiver restarted on %s:%s", host, port)
        except Exception as exc:
            logger.error("Reconnect error: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    state = BridgeState()
    _configure_logging(state)
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.getLogger().setLevel(getattr(logging, log_level, logging.INFO))
    try:
        app = BridgeApp()
        # Replace the placeholder state with the one logging is already using,
        # so the dashboard's "Recent Events" panel matches the log file.
        app._state = state
        app.run()
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Fatal error")
        sys.exit(1)


if __name__ == "__main__":
    main()