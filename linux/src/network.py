"""TCP client that streams controller state packets to the Windows PC.

The connection is full-duplex: the bridge *sends* state packets upstream
and also *receives* 54-byte v2 packets that carry host-side feedback such
as rumble commands.  Keeping the protocol symmetric means neither side
needs a second socket.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time

logger = logging.getLogger("network")

_PAYLOAD_SIZE = 54
# All-0xFF = PING packet (connection keepalive, sent every 1s when idle)
_PING_PAYLOAD = b"\xff" * _PAYLOAD_SIZE

# Offsets inside a v2 packet — must match windows/src/receiver.py:50-55
_RUMBLE_LEFT_OFFSET  = 16
_RUMBLE_RIGHT_OFFSET = 17


class TCPStreamer:
    """Connection to the Windows PC, with auto-reconnect and keepalive.

    Threading model
    ---------------
    * _run_thread:     connect / reconnect loop + keepalive PING
    * _recv_thread:    drains 54-byte v2 packets, fires on_recv_packet(pkt)
    * main thread:     calls .send(data) to push state upstream

    send() and the recv thread share the socket via _lock.  A short
    packet (< 54 bytes) is dropped, a PING is dropped silently.
    """

    def __init__(self, host: str, port: int, on_disconnect=None, on_recv_packet=None):
        self.host = host
        self.port = port
        self.on_disconnect = on_disconnect
        # Called with each 54-byte v2 packet received from the PC.
        # Implementations should be fast and non-blocking.
        self.on_recv_packet = on_recv_packet
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._recv_thread: threading.Thread | None = None
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
        if self._recv_thread:
            self._recv_thread.join(timeout=1)

    # ------------------------------------------------------------------
    # Public send
    # ------------------------------------------------------------------

    def send(self, data: bytes) -> bool:
        """Send a 54-byte controller state packet. Thread-safe."""
        if len(data) != _PAYLOAD_SIZE:
            raise ValueError(f"Payload must be {_PAYLOAD_SIZE} bytes, got {len(data)}")

        with self._lock:
            sock = self._sock
        if sock is None:
            return False

        try:
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
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, 1)
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

        # Start the receive thread for this connection
        self._recv_thread = threading.Thread(
            target=self._recv_loop, args=(sock,), name="TCPRecv", daemon=True
        )
        self._recv_thread.start()

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

        # Stop the recv thread and tear down
        with self._lock:
            if self._sock is sock:
                self._sock = None
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        if self._recv_thread:
            self._recv_thread.join(timeout=1)
            self._recv_thread = None
        self._notify_disconnect()

    def _recv_loop(self, sock: socket.socket) -> None:
        """Drain 54-byte v2 packets until the socket closes.

        The protocol is full-duplex on a single socket, so the same
        socket that the Windows receiver is reading from will be
        written to with rumble packets.  We extract the rumble bytes
        and forward them to the on_recv_packet callback.
        """
        buf = bytearray()
        try:
            while self._running:
                chunk = sock.recv(_PAYLOAD_SIZE * 4)
                if not chunk:
                    break  # peer closed
                buf.extend(chunk)
                # Slice off complete packets, keep any partial tail
                while len(buf) >= _PAYLOAD_SIZE:
                    pkt = bytes(buf[:_PAYLOAD_SIZE])
                    del buf[:_PAYLOAD_SIZE]
                    if pkt == _PING_PAYLOAD:
                        continue  # keepalive — ignore
                    cb = self.on_recv_packet
                    if cb is not None:
                        try:
                            cb(pkt)
                        except Exception as exc:
                            logger.debug("on_recv_packet callback error: %s", exc)
        except OSError:
            pass  # normal shutdown
        except Exception as exc:
            logger.debug("recv_loop: %s", exc)

    def _mark_disconnected(self) -> None:
        with self._lock:
            self._sock = None
        self._notify_disconnect()

    def _notify_disconnect(self) -> None:
        cb = self.on_disconnect
        if cb:
            cb()
