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
import time

from .bluetooth import ensure_paired
from .controller import build_abs_info, find_controller, make_state, read_next_state
from .discovery import DiscoveryListener
from .network import TCPStreamer

logger = logging.getLogger("main")


class BridgeApp:
    def __init__(self):
        self._running = False
        self._tcp: TCPStreamer | None = None
        self._device = None
        self._abs_info: dict = {}
        self._state = None          # persistent ControllerState
        self._controller_mac = os.getenv("CONTROLLER_MAC", "").strip() or None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._running = True
        self._register_signals()

        pc_host, pc_port = self._resolve_host_port()

        logger.info("Starting Xbox Bridge — target %s:%s", pc_host, pc_port)

        # 1 — Bluetooth setup (pair / connect)
        self._setup_bluetooth()

        # 2 — Find the evdev device
        self._open_device()

        # 3 — Start TCP streamer
        self._tcp = TCPStreamer(pc_host, pc_port, on_disconnect=self._on_disconnect)
        self._tcp.start()

        # 4 — Main poll loop
        logger.info("Bridge running — press Ctrl+C to stop")
        while self._running:
            self._poll()

        logger.info("Shutting down …")
        self._tcp.stop()

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def _setup_bluetooth(self) -> None:
        logger.info("Setting up Bluetooth …")
        # Run in a thread — the kernel driver handles the actual BT connection.
        # We just need the device node to exist.
        import threading
        t = threading.Thread(
            target=ensure_paired,
            args=(self._controller_mac,),
            name="BluetoothSetup",
            daemon=True,
        )
        t.start()
        # Give it a few seconds before we try to open /dev/input
        time.sleep(5)

    def _open_device(self) -> None:
        logger.info("Waiting for /dev/input device …")
        for attempt in range(1, 61):  # up to 60 s
            try:
                self._device = find_controller()
                self._abs_info = build_abs_info(self._device)
                self._state = make_state(self._device, self._abs_info)
                logger.info("Controller device: %s", self._device.path)
                return
            except RuntimeError:
                pass
            time.sleep(1)

        raise RuntimeError(
            "Controller device not found after 60 s. "
            "Ensure the controller is paired and powered on."
        )


    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        try:
            state = read_next_state(self._device, self._abs_info, self._state)
            if self._tcp.send(state.to_bytes()):
                return  # sent ok
            # If send failed, sleep and retry until reconnected
            time.sleep(0.5)
        except OSError as exc:
            logger.error("Device read error: %s", exc)
            self._reconnect_device()

    def _reconnect_device(self) -> None:
        logger.info("Attempting to re-open controller device …")
        self._device = None
        for _ in range(30):
            try:
                self._device = find_controller()
                self._abs_info = build_abs_info(self._device)
                self._state = make_state(self._device, self._abs_info)
                logger.info("Controller reconnected at %s", self._device.path)
                return
            except RuntimeError:
                pass
            time.sleep(1)
        logger.error("Could not re-find controller device")

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