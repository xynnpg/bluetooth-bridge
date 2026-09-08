"""TCP server that receives 54-byte state packets from the Linux bridge."""

from __future__ import annotations

import logging
import socket
import struct
import threading

logger = logging.getLogger("receiver")

_PAYLOAD_SIZE    = 54
_PAYLOAD_V1_SIZE = 24
_PING_PAYLOAD    = b"\xff" * _PAYLOAD_SIZE
_PROTO_V2        = 0x02

# XInput dead-zone constants
DEADZONE_STICK = int(0.15 * 32767)  # ~15 % dead-zone, matches Xbox spec
DEADZONE_TRIGGER = int(0.01 * 255)  # near-zero trigger threshold


class StateParser:
    """Parse a packet into a dict matching ControllerState layout.

    Supports both v1 (24-byte, no proto byte) and v2 (54-byte, proto=0x02).
    V1 packets will arrive with `battery=-1`, `charging=False`,
    `rumble_left=0`, `rumble_right=0`, `controller_mac=""`, `controller_name=""`.
    """

    @staticmethod
    def parse(data: bytes) -> dict | None:
        if not data:
            return None
        # Keepalive — first byte 0xFF or all-0xFF means PING
        if data[:1] == b"\xff":
            return None

        if len(data) >= _PAYLOAD_SIZE and data[13] == _PROTO_V2:
            return StateParser._parse_v2(data)
        if len(data) >= _PAYLOAD_V1_SIZE:
            return StateParser._parse_v1(data[:_PAYLOAD_V1_SIZE])
        return None

    # ------------------------------------------------------------------
    # v2 — battery, rumble, MAC, name
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_v2(packet: bytes) -> dict:
        (lthumb_x, lthumb_y, rthumb_x, rthumb_y,
         lt, rt, buttons_low, buttons_high,
         dpad, _proto, battery, flags,
         rumble_left, rumble_right) = struct.unpack(
            "<HHHHBBBBBBBBBB", packet[:18]
        )
        charging = bool(flags & 0x01)
        if not (flags & 0x02):
            battery = -1  # unknown
        # Identity fields (6 + 16 bytes) — only present in full 54-byte packets
        mac = ""
        name = ""
        if len(packet) >= 18 + 6 + 16:
            mac_bytes  = packet[18:24]
            if any(mac_bytes):
                mac = ":".join(f"{b:02x}" for b in mac_bytes)
            name_bytes = packet[24:40]
            name = name_bytes.rstrip(b"\x00").decode("ascii", errors="replace")
        return {
            "lthumb_x":      lthumb_x,
            "lthumb_y":      lthumb_y,
            "rthumb_x":      rthumb_x,
            "rthumb_y":      rthumb_y,
            "lt":            lt,
            "rt":            rt,
            "buttons_low":   buttons_low,
            "buttons_high":  buttons_high,
            "dpad":          dpad,
            "battery":       battery,
            "charging":      charging,
            "rumble_left":   rumble_left,
            "rumble_right":  rumble_right,
            "controller_mac":  mac,
            "controller_name": name,
        }

    # ------------------------------------------------------------------
    # v1 — legacy 14-byte payload (caller already sliced)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_v1(packet: bytes) -> dict:
        (lthumb_x, lthumb_y, rthumb_x, rthumb_y,
         lt, rt, buttons_low, buttons_high,
         dpad, _reserved) = struct.unpack("<HHHHBBBBBB", packet[:14])
        return {
            "lthumb_x":      lthumb_x,
            "lthumb_y":      lthumb_y,
            "rthumb_x":      rthumb_x,
            "rthumb_y":      rthumb_y,
            "lt":            lt,
            "rt":            rt,
            "buttons_low":   buttons_low,
            "buttons_high":  buttons_high,
            "dpad":          dpad,
            "battery":       -1,         # unknown on v1 senders
            "charging":      False,
            "rumble_left":   0,
            "rumble_right":  0,
            "controller_mac":  "",
            "controller_name": "",
        }


class TCPReceiver:
    """TCP server that dispatches parsed state to a callback.

    The active connection is also used to send rumble back to the Linux side
    via ``send_state()`` — a full-duplex single-socket design.
    """

    def __init__(self, host: str, port: int, on_state, on_connect=None,
                 on_ping=None, on_disconnect=None):
        self.bind_host     = host
        self.port          = port
        self.on_state      = on_state
        self.on_connect    = on_connect
        self.on_ping       = on_ping
        self.on_disconnect = on_disconnect
        self._running      = False
        self._thread: threading.Thread | None = None
        self._listener: socket.socket | None = None
        # Active client connection (single-client design)
        self._conn: socket.socket | None = None
        self._conn_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Outgoing — used to forward rumble back to the controller
    # ------------------------------------------------------------------

    def send_state(self, packet: bytes) -> bool:
        """Send a 54-byte v2 packet to the Linux side (e.g. rumble)."""
        if len(packet) != _PAYLOAD_SIZE:
            logger.warning("send_state: packet is %d bytes, expected %d",
                           len(packet), _PAYLOAD_SIZE)
            return False
        with self._conn_lock:
            conn = self._conn
        if conn is None:
            return False
        try:
            conn.sendall(packet)
            return True
        except OSError as exc:
            logger.debug("send_state failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, name="TCPReceiver", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        with self._conn_lock:
            if self._conn:
                try:
                    self._conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self._conn.close()
                self._conn = None
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
                with self._conn_lock:
                    # Replace any stale connection
                    if self._conn is not None:
                        try: self._conn.close()
                        except OSError: pass
                    self._conn = conn
                if self.on_connect:
                    try:
                        self.on_connect(addr[0])
                    except Exception:
                        pass
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

                # Process v2 packets (54 B) first, then v1 (24 B)
                # v2 packets start with proto byte at offset 13 == 0x02
                processed = 0
                while len(buf) >= _PAYLOAD_SIZE and processed < 16:
                    packet = buf[:_PAYLOAD_SIZE]
                    if packet == _PING_PAYLOAD or packet[0:1] == b"\xff":
                        buf = buf[_PAYLOAD_SIZE:]
                        if self.on_ping:
                            try: self.on_ping()
                            except Exception: pass
                        processed += 1
                        continue
                    if packet[13] != _PROTO_V2:
                        # Probably a v1 packet from a legacy sender — try parsing
                        v1 = StateParser.parse(packet[:_PAYLOAD_V1_SIZE])
                        if v1 is not None:
                            try: self.on_state(v1)
                            except Exception as exc:
                                logger.error("on_state callback error: %s", exc)
                        buf = buf[_PAYLOAD_V1_SIZE:]
                        processed += 1
                        continue
                    state = StateParser.parse(packet)
                    buf = buf[_PAYLOAD_SIZE:]
                    if state is not None:
                        try: self.on_state(state)
                        except Exception as exc:
                            logger.error("on_state callback error: %s", exc)
                    processed += 1
                # Fallback: legacy v1 sender (only 24-byte frames)
                while len(buf) >= _PAYLOAD_V1_SIZE:
                    packet = buf[:_PAYLOAD_V1_SIZE]
                    if packet == b"\xff" * _PAYLOAD_V1_SIZE:
                        if self.on_ping:
                            try: self.on_ping()
                            except Exception: pass
                        buf = buf[_PAYLOAD_V1_SIZE:]
                        continue
                    state = StateParser.parse(packet)
                    buf = buf[_PAYLOAD_V1_SIZE:]
                    if state is not None:
                        try: self.on_state(state)
                        except Exception as exc:
                            logger.error("on_state callback error: %s", exc)
        except OSError as exc:
            logger.info("Connection error from %s: %s", addr[0], exc)
        finally:
            with self._conn_lock:
                if self._conn is conn:
                    self._conn = None
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
            logger.info("Connection closed: %s", addr[0])
            if self.on_disconnect:
                try:
                    self.on_disconnect(addr[0])
                except Exception:
                    pass