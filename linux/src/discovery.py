"""UDP discovery listener for zero-config networking.

Listens for BRIDGE_HELLO:<ip>:<port> broadcasts from the Windows side
on port 9876. Returns the discovered IP so the caller can use it for
the TCP connection.

Intended to run as a blocking listen loop that exits once a valid
broadcast is received, or after a timeout.
"""

from __future__ import annotations

import logging
import socket
import threading
import time

logger = logging.getLogger("discovery")

DISCOVERY_PORT = 9876
DISCOVERY_TIMEOUT = 30.0  # seconds to wait before giving up
BROADCAST_MAGIC = b"BRIDGE_HELLO:"


def _wait_for_broadcast(timeout: float = DISCOVERY_TIMEOUT) -> tuple[str, int] | None:
    """
    Block until a BRIDGE_HELLO broadcast is received, or timeout expires.

    Returns
    -------
    (ip: str, port: int)
        The Windows PC's IP address and TCP listen port.

    Returns
    -------
    None
        If no valid broadcast is received within *timeout*.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        sock.bind(("", DISCOVERY_PORT))
    except OSError as exc:
        logger.error("Failed to bind discovery socket on UDP %d: %s", DISCOVERY_PORT, exc)
        return None

    logger.info("Discovery listener active on UDP %d (timeout=%ds)", DISCOVERY_PORT, timeout)

    try:
        while True:
            try:
                data, addr = sock.recvfrom(256)
            except socket.timeout:
                logger.warning("Discovery timeout — no BRIDGE_HELLO received in %ds", timeout)
                return None

            if not data.startswith(BROADCAST_MAGIC):
                logger.debug("Ignored non-broadcast packet from %s: %r", addr[0], data[:32])
                continue

            # payload: BRIDGE_HELLO:<ip>:<port>
            try:
                parts = data.decode().strip().split(":")
                if len(parts) != 3 or parts[0] != "BRIDGE_HELLO":
                    raise ValueError("malformed")
                discovered_ip = parts[1]
                discovered_port = int(parts[2])
            except (UnicodeDecodeError, ValueError) as exc:
                logger.warning("Malformed discovery packet from %s: %r (%s)", addr[0], data, exc)
                continue

            # Basic sanity check: must look like an IPv4 address
            octets = discovered_ip.split(".")
            if len(octets) != 4:
                logger.warning("Discovery packet has invalid IP: %s", discovered_ip)
                continue
            try:
                [int(o) for o in octets]
            except ValueError:
                logger.warning("Discovery packet has invalid IP: %s", discovered_ip)
                continue

            logger.info("Discovered Windows bridge at %s:%d (received from %s:%d)",
                        discovered_ip, discovered_port, addr[0], addr[1])
            return (discovered_ip, discovered_port)

    finally:
        try:
            sock.close()
        except OSError:
            pass


class DiscoveryListener:
    """
    Runs the UDP discovery listener in a background thread so discovery
    can happen concurrently with the normal startup poll loop.

    After receiving a valid broadcast, the discovered host/port are
    stored and any caller of :meth:`get` will receive them.

    If no broadcast arrives within *timeout* seconds, the listener exits
    and the object reports discovery as failed.
    """

    def __init__(self, timeout: float = DISCOVERY_TIMEOUT):
        self.timeout = timeout
        self._running = False
        self._thread: threading.Thread | None = None
        self._result: tuple[str, int] | None = None
        self._error: str | None = None

    def start(self) -> None:
        """Start the listener thread. Idempotent."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="DiscoveryListener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the listener. Idempotent."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def get(self) -> tuple[str, int] | None:
        """
        Return the discovered (IP, port) once a broadcast has arrived,
        or ``None`` if discovery failed / hasn't completed yet.
        """
        return self._result

    def failed(self) -> bool:
        """Return ``True`` if discovery timed out without receiving a valid packet."""
        return self._result is None and not self._running

    def _run(self) -> None:
        result = _wait_for_broadcast(self.timeout)
        if result is not None:
            self._result = result
        else:
            self._error = "No BRIDGE_HELLO received within timeout"
            logger.error("Discovery failed: %s", self._error)
        self._running = False