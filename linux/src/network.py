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

from . import trace

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

    def __init__(self, host: str, port: int, on_disconnect=None, on_recv_packet=None,
                 send_timeout_s: float = 0.25):
        self.host = host
        self.port = port
        self.on_disconnect = on_disconnect
        # Called with each 54-byte v2 packet received from the PC.
        # Implementations should be fast and non-blocking.
        self.on_recv_packet = on_recv_packet
        # Bound the time we block on sendall() so a stalled peer cannot
        # queue up evdev events on the producer side.
        self._send_timeout_s = send_timeout_s
        self._sock: socket.socket | None = None
        self._sock_generation = 0
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._recv_thread: threading.Thread | None = None
        self._latency_warning_issued = False
        # Trace state: the last button/D-pad signature that was sent and
        # the time of the last trace line (for the periodic heartbeat).
        self._trace_sig: tuple | None = None
        self._trace_at = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, name="TCPStreamer", daemon=True)
        self._thread.start()

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._sock is not None

    def stop(self) -> None:
        self._running = False
        with self._lock:
            sock = self._sock
            self._sock = None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
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

        # The lock must be held across the ENTIRE sendall(), not just while
        # reading self._sock.  The keepalive thread also writes to this
        # socket; if a partial sendall() under congestion interleaves with
        # a PING packet, the byte stream is permanently misaligned and the
        # Windows receiver can false-re-sync onto garbage (ghost inputs).
        #
        # The lock is acquired with a timeout: the keepalive may be stuck
        # in a write while the peer stopped reading, and the input loop
        # must not be blocked by it.  A frame that could not be sent is not
        # lost — every frame is an authoritative snapshot, so the next
        # heartbeat carries the same state.
        if not self._lock.acquire(timeout=self._send_timeout_s):
            logger.warning("Send skipped — the connection is busy")
            return False
        try:
            sock = self._sock
            if sock is None:
                return False
            # Apply a short per-send timeout so a stalled peer (slow
            # WiFi, blocked Windows-side loop, etc.) cannot queue up
            # evdev events on the producer side.  The socket is restored
            # to blocking mode in the `finally` to keep the keepalive
            # thread unaffected.
            sock.settimeout(self._send_timeout_s)
            try:
                sock.sendall(data)
                if data[10] or data[11] or data[12]:
                    logger.debug("TX button frame: buttons=%s", data[10:13].hex())
                self._trace_frame(data)
                return True
            except socket.timeout:
                # The peer stopped reading and only *part* of the frame
                # reached the wire.  The stream is now permanently
                # misaligned: every following frame would be decoded by
                # the receiver at the wrong offset and turned into ghost
                # button presses.  The only safe recovery is to drop the
                # connection and start a fresh, aligned stream.
                logger.warning("Send timed out after %.0f ms — dropping the "
                               "connection to avoid a corrupted frame stream",
                               self._send_timeout_s * 1000)
                trace.event("SEND TIMEOUT after %.0f ms — dropping the connection",
                            self._send_timeout_s * 1000)
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                logger.warning("Send failed: %s", exc)
                trace.event("SEND FAILED: %s", exc)
            finally:
                try:
                    sock.settimeout(None)
                except OSError:
                    pass
        finally:
            self._lock.release()
        # NOTE: _mark_disconnected() must not be called while the lock is
        # held — it takes the same (non-reentrant) lock.
        self._mark_disconnected(sock)
        return False

    def _trace_frame(self, data: bytes) -> None:
        """Write a trace line for a frame that was sent.

        A line is written whenever the button / D-pad state changes and
        once per second otherwise, so the trace shows every transition
        without flooding the file.
        """
        sig = (data[10], data[11], data[12])
        now = time.monotonic()
        if sig == self._trace_sig and now - self._trace_at < 1.0:
            return
        self._trace_sig = sig
        self._trace_at = now
        trace.state("TX", data[10], data[11], data[12], data[8], data[9])

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
            self._sock_generation += 1
            generation = self._sock_generation
        logger.info("Connected to %s:%s", self.host, self.port)
        trace.event("CONNECT to %s:%s", self.host, self.port)

        # Start the receive thread for this connection
        self._recv_thread = threading.Thread(
            target=self._recv_loop, args=(sock,), name="TCPRecv", daemon=True
        )
        self._recv_thread.start()

        # Send keepalive frames every second.  The lock is held across
        # sendall() for the same reason as in send(): PING and state
        # packets must never interleave on the wire.
        last_ping = time.monotonic()
        while self._running:
            try:
                now = time.monotonic()
                if now - last_ping >= 1.0:
                    with self._lock:
                        if self._sock is not sock or not self._running:
                            break
                        try:
                            # The same bound as for the state frames: a
                            # peer that stopped reading must not block the
                            # writer, and a half-written PING would
                            # misalign the stream.
                            sock.settimeout(self._send_timeout_s)
                            sock.sendall(_PING_PAYLOAD)
                        except socket.timeout:
                            logger.warning("Keepalive PING timed out — "
                                           "dropping the connection")
                            break
                        except OSError:
                            break
                        finally:
                            try:
                                sock.settimeout(None)
                            except OSError:
                                pass
                    last_ping = now
                time.sleep(0.1)
            except Exception:
                break

        # Stop the recv thread and tear down
        with self._lock:
            active = self._sock is sock and self._sock_generation == generation
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
        if active:
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
                try:
                    chunk = sock.recv(_PAYLOAD_SIZE * 4)
                except socket.timeout:
                    # The socket timeout is shared with the writer; a
                    # timeout here only means "no data yet".
                    continue
                if not chunk:
                    break  # peer closed
                buf.extend(chunk)
                # Slice off complete packets, keep any partial tail
                while len(buf) >= _PAYLOAD_SIZE:
                    pkt = bytes(buf[:_PAYLOAD_SIZE])
                    del buf[:_PAYLOAD_SIZE]
                    if pkt == _PING_PAYLOAD:
                        logger.debug("RX ping from Windows")
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

    def _mark_disconnected(self, sock: socket.socket) -> None:
        active = False
        with self._lock:
            if self._sock is sock:
                self._sock = None
                active = True
        if not active:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
        self._notify_disconnect()

    def _notify_disconnect(self) -> None:
        trace.event("DISCONNECT from %s:%s", self.host, self.port)
        cb = self.on_disconnect
        if cb:
            cb()
