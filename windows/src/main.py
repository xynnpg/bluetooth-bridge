"""Xbox Controller Bluetooth Bridge — Windows side entry point.

Receives state packets from the Linux bridge over TCP and emits a virtual
Xbox controller via ViGEmBus (vgamepad).  Forwards XInput rumble from games
back to the physical controller over the same TCP socket (v2 protocol).

All configuration and control now live in a local Flask dashboard (see
``webui.py``); the tray only exposes Open Web UI / Reset / Reconnect /
Open App Folder / Exit.
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

from . import trace
from .receiver     import TCPReceiver
from .emitter      import XInputEmitter
from .tray         import TrayManager
from .discovery    import DiscoveryBroadcaster
from .state        import BridgeState, _EventLogHandler
from .config       import Config
from .webui        import WebUI, VERSION

logger = logging.getLogger("main")

LOG_DIR  = os.path.join(
    os.environ.get("LOCALAPPDATA", os.environ.get("USERPROFILE", ".")),
    "bluetooth_bridge"
)
LOG_FILE    = os.path.join(LOG_DIR, "bluetooth-bridge.log")
INSTALL_DIR = os.path.abspath(os.getcwd())
CONFIG_FILE = os.path.join(INSTALL_DIR, "config.ini")

_RUMBLE_PACK_FMT = "<HHHHBBBBBBBBBB"
_RUMBLE_PACK_SIZE = struct.calcsize(_RUMBLE_PACK_FMT)

PROTO_VERSION = 0x02
PAYLOAD_SIZE  = 54

_config: Config | None = None


def _configure_logging(state: BridgeState, level_name: str) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level_name, logging.INFO))

    for h in root.handlers[:]:
        root.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                            datefmt="%H:%M:%S")

    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    if sys.stdout is not None:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        root.addHandler(ch)

    eb_fmt = logging.Formatter("%(message)s")
    # The input trace: a separate, millisecond-precise log of the whole
    # input path (see the "Diagnostics" section of the README).
    trace_path = trace.setup()
    if trace_path:
        root.info("Input trace log: %s", trace_path)

    eb = _EventLogHandler(state, level=logging.INFO)
    eb.setFormatter(eb_fmt)
    root.addHandler(eb)


def _build_rumble_packet(left: int, right: int) -> bytes:
    flags = 0
    pkt = struct.pack(
        _RUMBLE_PACK_FMT,
        32768, 32768, 32768, 32768,
        0, 0,
        0, 0,
        0,
        PROTO_VERSION,
        0xFF,
        flags,
        max(0, min(255, int(left))),
        max(0, min(255, int(right))),
    )
    return pkt + b"\x00" * (PAYLOAD_SIZE - _RUMBLE_PACK_SIZE)


class BridgeApp:
    def __init__(self, config: Config) -> None:
        self._config  = config
        self._running = False
        self._state   = BridgeState()

        self._receiver: TCPReceiver | None = None
        self._emitter = XInputEmitter(slot=0, on_rumble=self._on_rumble,
                                      config=config)
        self._tray = TrayManager(
            on_exit=self.stop,
            on_restart_controller=self.reset_controller,
            on_reconnect=self.reconnect,
            on_open_app=self.open_web_ui,
        )
        self._broadcaster = DiscoveryBroadcaster(
            listen_port=config.get_int("app.listen_port"),
            discovery_port=config.get_int("network.discovery_port"),
        )
        self._webui: WebUI | None = None

        self._controller_ok = False
        self._pc_reachable  = False
        self._peer_ip       = ""
        self._last_recv     = time.monotonic()
        # Timestamp of the last *state* frame.  The keepalive PINGs are
        # tracked separately: a peer that only sends PINGs is alive but its
        # input pipeline is dead, and the pad must not keep the buttons it
        # received before.
        self._last_state_at = time.monotonic()
        # The rumble that was last sent to the controller.
        self._last_rumble = (0, 0)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._running = True

        listener_host = self._config.get_str("app.listen_host") or "0.0.0.0"
        listener_port = self._config.get_int("app.listen_port")

        logger.info("Starting Xbox Bridge (Windows) v%s — listening on %s:%s",
                    VERSION, listener_host, listener_port)

        if not self._emitter.attach():
            logger.error("Could not attach ViGEmBus controller — is ViGEmBus installed?")
            sys.exit(1)

        if self._config.get_bool("app.auto_discover"):
            self._broadcaster.start()
        else:
            logger.info("Auto-discovery disabled in config")

        atexit.register(self._cleanup)

        self._tray.set_install_dir(INSTALL_DIR)
        self._tray.set_listen_addr(f"{listener_host}:{listener_port}")
        self._tray.set_state(self._state)
        self._tray.set_show_battery(
            self._config.get_bool("tray.show_battery") and
            self._config.get_bool("controller.battery_display_enabled"))

        self._webui = WebUI(
            state=self._state,
            config=self._config,
            log_path=LOG_FILE,
            install_dir=INSTALL_DIR,
            config_path=CONFIG_FILE,
            listen_addr=f"{listener_host}:{listener_port}",
            callbacks={
                "reconnect":        self.reconnect,
                "reset_controller": self.reset_controller,
                "open_folder":      self._open_folder,
                "open_logs":        self._open_folder,
                "quit":             self._request_quit,
                "settings_applied": self._on_settings_applied,
            },
        )
        self._webui.start()

        self._receiver = self._make_receiver(listener_host, listener_port)
        self._receiver.start()

        threading.Thread(target=self._monitor_loop, name="Monitor",
                         daemon=True).start()

        logger.info("Bridge running — tray icon in the notification area")

        self._tray.run_blocking()
        logger.info("Tray exited — shutting down")

    def stop(self) -> None:
        logger.info("Stop signal received")
        self._running = False

    def _request_quit(self):
        """Quit requested from the web UI (runs on the Flask thread)."""
        logger.info("Quit requested from web UI")
        self._running = False
        threading.Timer(0.3, self._tray.stop).start()
        return True

    def _make_receiver(self, host: str, port: int) -> TCPReceiver:
        return TCPReceiver(
            host, port,
            on_state=self._on_state,
            on_connect=self._on_connect,
            on_ping=self._on_ping,
            on_disconnect=self._on_disconnect,
        )

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, peer_ip: str) -> None:
        logger.info("Linux bridge connected from %s", peer_ip)
        self._peer_ip = peer_ip
        self._pc_reachable = True
        self._last_recv = time.monotonic()
        self._state.on_connect(peer_ip)
        self._tray.update(connected=True, pc_reachable=True, peer_ip=peer_ip)

    def _on_ping(self) -> None:
        if not self._pc_reachable:
            self._pc_reachable = True
            logger.info("Linux bridge keepalive received")
            self._state.on_connect(self._peer_ip)
            self._tray.update(connected=True, pc_reachable=True, peer_ip=self._peer_ip)
        self._last_recv = time.monotonic()

    def _on_disconnect(self, peer_ip: str) -> None:
        logger.info("Linux bridge disconnected from %s", peer_ip)
        trace.event("DISCONNECT from %s", peer_ip)
        self._pc_reachable = False
        self._emitter.clear()
        self._state.on_disconnect()
        self._tray.update(connected=False, pc_reachable=False, peer_ip=self._peer_ip)

    def _on_state(self, state: dict) -> None:
        if not self._pc_reachable:
            self._pc_reachable = True
            logger.info("Linux bridge connected")
            self._state.on_connect(self._peer_ip)
            self._tray.update(connected=True, pc_reachable=True,
                              peer_ip=self._peer_ip)

        self._controller_ok = True
        self._last_recv     = time.monotonic()
        self._last_state_at = self._last_recv
        self._state.on_state(state)

        try:
            self._emitter.apply(state)
        except Exception as exc:
            logger.error("Emitter error: %s", exc)

    def _on_rumble(self, left: int, right: int) -> None:
        self._state.on_rumble(left, right)
        if self._receiver is None:
            return
        if not self._pc_reachable:
            return
        self._last_rumble = (left, right)
        pkt = _build_rumble_packet(left, right)
        if not self._receiver.send_state(pkt):
            logger.debug("Rumble packet dropped (no active connection)")

    def _resend_rumble(self) -> None:
        """Send the current rumble state again.

        The rumble is only sent when the game changes it, so a lost packet
        would leave the motor running until the next change.  Unlike the
        buttons — where every frame is a complete snapshot — the rumble
        needs to be repeated to be self-healing.
        """
        if self._receiver is None or not self._pc_reachable:
            return
        left, right = self._last_rumble
        self._receiver.send_state(_build_rumble_packet(left, right))

    def _on_settings_applied(self, changed: dict) -> None:
        """React to a save from the web dashboard without a restart."""
        if not changed:
            return
        if "app.log_level" in changed:
            level = self._config.get_str("app.log_level")
            logging.getLogger().setLevel(getattr(logging, level, logging.INFO))
            logger.info("Log level changed to %s", level)
        if "controller.rumble_enabled" in changed:
            self._emitter.set_rumble_enabled(
                self._config.get_bool("controller.rumble_enabled"))
        if "tray.show_battery" in changed or "controller.battery_display_enabled" in changed:
            self._tray.set_show_battery(
                self._config.get_bool("tray.show_battery") and
                self._config.get_bool("controller.battery_display_enabled"))
        for name, value in changed.items():
            if name in ("app.listen_port", "app.listen_host",
                        "webui.port", "webui.host", "network.discovery_port"):
                logger.info("Setting %s changed to %r — restart required",
                            name, value)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _peer_streams_heartbeats(self) -> bool:
        """True when the peer is known to keep streaming the state.

        A peer that only reports when the controller is used (an older
        version of the Linux side) must not be watched with the state
        watchdog: a button that is held for a long time would be released
        while it is still being held.  Newer peers announce that they
        stream the state continuously (flag bit 2 of every frame).
        """
        receiver = self._receiver
        if receiver is None:
            return True      # no receiver yet — nothing better to go on
        return bool(getattr(receiver, "_peer_streams", False))

    def _monitor_loop(self) -> None:
        while self._running:
            time.sleep(0.5)
            if not self._running:
                break

            if self._pc_reachable:
                timeout = self._config.get_float("network.keepalive_timeout_s")
                elapsed = time.monotonic() - self._last_recv
                if elapsed > timeout:
                    self._pc_reachable = False
                    self._emitter.clear()
                    self._state.on_disconnect()
                    logger.warning("Linux bridge gone (%.1f s silence)", elapsed)
                    trace.event("TIMEOUT Linux bridge gone (%.1f s silence) — "
                                "released all buttons", elapsed)
                else:
                    # The connection is alive but the *input* stopped: the
                    # peer only sends keepalives.  Let go of every button
                    # so a dead input pipeline cannot leave a button
                    # latched in the game.
                    stale = time.monotonic() - self._last_state_at
                    if stale > timeout and self._peer_streams_heartbeats():
                        self._emitter.clear()
                        logger.warning("No controller state for %.1f s — "
                                       "released all buttons", stale)
                        trace.event("WATCHDOG no controller state for %.1f s — "
                                    "released all buttons", stale)

            # Repeat the rumble so a lost packet cannot leave the motor
            # running forever.
            self._resend_rumble()

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
            self._tray.set_show_battery(
                self._config.get_bool("tray.show_battery") and
                self._config.get_bool("controller.battery_display_enabled"))

    def open_web_ui(self) -> None:
        if self._webui is None:
            logger.warning("Web UI not initialised")
            self._tray.notify("Web UI is not available.", title="Xbox Bridge")
            return
        logger.info("Opening web UI at %s", self._webui.url or "(pending)")
        self._webui.open_browser()
        url = self._webui.url
        self._tray.notify(
            f"Web UI: {url}" if url else "Starting Web UI…",
            title="Xbox Bridge",
        )

    def _open_folder(self):
        folder = INSTALL_DIR or "."
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error("Cannot open folder: %s", exc)
        return True

    def _cleanup(self) -> None:
        logger.info("Cleaning up …")
        if self._webui:
            self._webui.stop()
        self._broadcaster.stop()
        if self._receiver:
            self._receiver.stop()
        if self._tray:
            self._tray.stop()
        if self._emitter:
            self._emitter.detach()
        logger.info("Shutdown complete")

    # ------------------------------------------------------------------
    # Tray / web actions
    # ------------------------------------------------------------------

    def reset_controller(self):
        logger.info("Resetting virtual controller …")
        try:
            self._emitter.detach()
            time.sleep(0.2)
            self._emitter = XInputEmitter(slot=0, on_rumble=self._on_rumble,
                                          config=self._config)
            self._emitter.attach()
            logger.info("Virtual controller reset")
            return True
        except Exception as exc:
            logger.error("Reset controller error: %s", exc)
            return False

    def reconnect(self):
        logger.info("Reconnecting TCP receiver …")
        try:
            if self._receiver:
                self._receiver.stop()
            host = self._config.get_str("app.listen_host") or "0.0.0.0"
            port = self._config.get_int("app.listen_port")
            self._receiver = self._make_receiver(host, port)
            self._receiver.start()
            logger.info("TCP receiver restarted on %s:%s", host, port)
            return True
        except Exception as exc:
            logger.error("Reconnect error: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global _config
    state = BridgeState()
    _config = Config(CONFIG_FILE)

    _configure_logging(state, _config.get_str("app.log_level"))
    env_level = os.environ.get("LOG_LEVEL")
    if env_level:
        logging.getLogger().setLevel(getattr(logging, env_level.upper(),
                                             logging.INFO))
    try:
        app = BridgeApp(_config)
        app._state = state
        app.run()
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Fatal error")
        sys.exit(1)


if __name__ == "__main__":
    main()
