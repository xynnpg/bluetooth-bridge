"""TCP server that receives 24-byte state packets from the Linux bridge."""

from __future__ import annotations

import logging
import socket
import struct
import threading

logger = logging.getLogger("receiver")

_PAYLOAD_SIZE = 24
_PING_PAYLOAD = b"\xff" * _PAYLOAD_SIZE

# XInput dead-zone constants
DEADZONE_STICK = int(0.15 * 32767)  # ~15 % dead-zone, matches Xbox spec
DEADZONE_TRIGGER = int(0.01 * 255)  # near-zero trigger threshold


class StateParser:
    """Parse a 24-byte packet into a dict matching ControllerState layout."""

    @staticmethod
    def parse(data: bytes) -> dict | None:
        if len(data) < _PAYLOAD_SIZE:
            return None
        packet = data[:_PAYLOAD_SIZE]
        if packet == _PING_PAYLOAD:
            return None  # keepalive — no state update

        (lthumb_x, lthumb_y, rthumb_x, rthumb_y,
         lt, rt, buttons_low, buttons_high,
         dpad, _reserved) = struct.unpack("<HHHHBBBBBB", packet[:14])

        return {
            "lthumb_x":    lthumb_x,
            "lthumb_y":    lthumb_y,
            "rthumb_x":    rthumb_x,
            "rthumb_y":    rthumb_y,
            "lt":          lt,
            "rt":          rt,
            "buttons_low":  buttons_low,
            "buttons_high": buttons_high,
            "dpad":         dpad,
        }


class TCPReceiver:
    """TCP server that dispatches parsed state to a callback."""

    def __init__(self, host: str, port: int, on_state):
        """
        Parameters
        ----------
        host : str
            Bind address. Use "" or "0.0.0.0" to accept from any interface.
        port : int
            TCP port (must match the Linux side PC_PORT).
        on_state : Callable[[dict], None]
            Called with the parsed state dict for each received packet.
        """
        self.bind_host = host
        self.port = port
        self.on_state = on_state
        self._running = False
        self._thread: threading.Thread | None = None
        self._listener: socket.socket | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, name="TCPReceiver", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._listener:
            try:
                self._listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._listener.close()
            self._listener = None
        if self._thread:
            self._thread.join(timeout=3)

    # ------------------------------------------------------------------
    # Server loop
    # ------------------------------------------------------------------

    def _accept_loop(self) -> None:
        try:
            self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._listener.bind((self.bind_host, self.port))
            self._listener.listen(1)
            logger.info("Listening on %s:%s", self.bind_host, self.port)
        except OSError as exc:
            logger.error("Failed to bind %s:%s — %s", self.bind_host, self.port, exc)
            return

        while self._running:
            try:
                self._listener.settimeout(2.0)
                conn, addr = self._listener.accept()
                logger.info("Connection from %s", addr[0])
                threading.Thread(
                    target=self._serve,
                    args=(conn, addr),
                    name=f"TCPClient-{addr[0]}",
                    daemon=True,
                ).start()
            except socket.timeout:
                continue
            except OSError as exc:
                if self._running:
                    logger.warning("Accept error: %s", exc)

    def _serve(self, conn: socket.socket, addr: tuple) -> None:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # SO_RCVBUF tuned to 32 KB — absorbs micro-bursts without dropping frames
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32768)
        buf = b""
        try:
            while self._running:
                chunk = conn.recv(4096)
                if not chunk:
                    logger.info("Client %s disconnected", addr[0])
                    break
                buf += chunk

                while len(buf) >= _PAYLOAD_SIZE:
                    packet = buf[:_PAYLOAD_SIZE]
                    buf = buf[_PAYLOAD_SIZE:]
                    state = StateParser.parse(packet)
                    if state is None:
                        continue  # PING
                    try:
                        self.on_state(state)
                    except Exception as exc:
                        logger.error("on_state callback error: %s", exc)
        except OSError as exc:
            logger.info("Connection error from %s: %s", addr[0], exc)
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
            logger.info("Connection closed: %s", addr[0])