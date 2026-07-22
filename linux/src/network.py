"""TCP client that streams controller state packets to the Windows PC."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time

logger = logging.getLogger("network")

_PAYLOAD_SIZE = 24
# All-0xFF = PING packet (connection keepalive, sent every 1s when idle)
_PING_PAYLOAD = b"\xff" * _PAYLOAD_SIZE


class TCPStreamer:
    """Connection to the Windows PC, with auto-reconnect and keepalive."""

    def __init__(self, host: str, port: int, on_disconnect=None):
        self.host = host
        self.port = port
        self.on_disconnect = on_disconnect
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._latency_warning_issued = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, name="TCPStreamer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()
            self._sock = None
        if self._thread:
            self._thread.join(timeout=3)

    # ------------------------------------------------------------------
    # Public send
    # ------------------------------------------------------------------

    def send(self, data: bytes) -> bool:
        """Send a 24-byte controller state packet. Thread-safe."""
        if len(data) != _PAYLOAD_SIZE:
            raise ValueError(f"Payload must be {_PAYLOAD_SIZE} bytes, got {len(data)}")

        with self._lock:
            sock = self._sock
        if sock is None:
            return False

        try:
            # SO_SNDBUF tuned to 32 KB — reduces OS-level blocking at high rates
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32768)
            sock.sendall(data)
            return True
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            logger.warning("Send failed: %s", exc)
            self._mark_disconnected()
            return False

    # ------------------------------------------------------------------
    # Internal reconnect loop + keepalive
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while self._running:
            self._connect_blocking()
            if not self._running:
                break
            logger.info("Reconnecting in 1 s …")
            time.sleep(1)

    def _connect_blocking(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(10)

        try:
            logger.info("Connecting to %s:%s …", self.host, self.port)
            sock.connect((self.host, self.port))
            sock.settimeout(None)
        except OSError as exc:
            logger.warning("Connect failed: %s", exc)
            sock.close()
            self._notify_disconnect()
            return

        with self._lock:
            self._sock = sock
        logger.info("Connected to %s:%s", self.host, self.port)

        # Send keepalive frames every second
        last_ping = time.monotonic()
        while self._running:
            try:
                now = time.monotonic()
                if now - last_ping >= 1.0:
                    try:
                        sock.sendall(_PING_PAYLOAD)
                    except OSError:
                        break
                    last_ping = now
                time.sleep(0.1)
            except Exception:
                break

        with self._lock:
            if self._sock is sock:
                self._sock = None
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        self._notify_disconnect()

    def _mark_disconnected(self) -> None:
        with self._lock:
            self._sock = None
        self._notify_disconnect()

    def _notify_disconnect(self) -> None:
        cb = self.on_disconnect
        if cb:
            cb()