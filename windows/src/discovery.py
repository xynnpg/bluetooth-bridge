"""UDP discovery broadcaster for zero-config networking.

On startup, broadcasts BRIDGE_HELLO:<local-ip>:<listen_port> to
255.255.255.255:9876 so the Linux side can auto-detect our IP without
manual configuration.

Meant to run as a background thread for the lifetime of the app.
"""

from __future__ import annotations

import logging
import socket
import threading
import time

logger = logging.getLogger("discovery")

DISCOVERY_PORT = 9876
MESSAGE_TEMPLATE = "BRIDGE_HELLO:{ip}:{port}\n"


def _get_local_ip() -> str:
    """Return the non-loopback IPv4 address of this machine.

    Uses a dummy outbound connection as a trick to determine the
    routing interface — works on most NAT configurations.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        # 8.8.8.8:53 is arbitrary — no packet is actually sent
        s.connect(("8.8.8.8", 53))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        # Fallback: parse the first non-loopback address from hostname
        hostname = socket.gethostname()
        addrs = socket.getaddrinfo(hostname, None, socket.AF_INET)
        for family, _, _, _, sockaddr in addrs:
            if sockaddr[0] not in ("127.0.0.1", "::1"):
                return sockaddr[0]
        return "127.0.0.1"


class DiscoveryBroadcaster:
    """Sends BRIDGE_HELLO UDP packets every 5 seconds while running."""

    def __init__(self, listen_port: int = 9999, broadcast_interval: float = 5.0):
        self.listen_port = listen_port
        self.interval = broadcast_interval
        self._running = False
        self._thread: threading.Thread | None = None
        self._local_ip: str | None = None

    def start(self) -> None:
        """Start broadcasting in a background thread. Idempotent."""
        if self._running:
            return
        self._running = True
        self._local_ip = _get_local_ip()
        logger.info("Discovery broadcaster starting — will announce %s on UDP %d",
                    self._local_ip, DISCOVERY_PORT)
        self._thread = threading.Thread(target=self._run, name="DiscoveryBroadcast", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop broadcasting. Idempotent."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            # Allow rebinding of the address quickly after restart
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError as exc:
            logger.error("Failed to create discovery socket: %s", exc)
            return

        payload = f"BRIDGE_HELLO:{self._local_ip}:{self.listen_port}".encode()

        try:
            sock.sendto(payload, ("<broadcast>", DISCOVERY_PORT))
            logger.info("Discovery broadcast sent: %s:%d", self._local_ip, self.listen_port)
        except OSError as exc:
            logger.warning("Initial discovery broadcast failed: %s", exc)

        while self._running:
            time.sleep(self.interval)
            if not self._running:
                break
            try:
                sock.sendto(payload, ("<broadcast>", DISCOVERY_PORT))
            except OSError as exc:
                logger.debug("Discovery broadcast error: %s", exc)
                break

        try:
            sock.close()
        except OSError:
            pass
        logger.info("Discovery broadcaster stopped")