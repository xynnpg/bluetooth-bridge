"""TCP server that receives 54-byte state packets from the Linux bridge."""

from __future__ import annotations

import logging
import select
import socket
import struct
import threading
import time

from . import trace

logger = logging.getLogger("receiver")

_PAYLOAD_SIZE    = 54
_PAYLOAD_V1_SIZE = 24
_PING_PAYLOAD    = b"\xff" * _PAYLOAD_SIZE
_PROTO_V2        = 0x02

# Byte offset of the frame checksum and the number of bytes it covers.
_CHECKSUM_OFFSET = 52
_CHECKSUM_SPAN   = 52

# How long the rumble path waits for the socket to become writable.  A
# rumble packet is tiny — if the socket is not writable right now it is
# dropped instead of stalling the ViGEm notification thread.
_SEND_TIMEOUT_S = 0.0

# How many bytes may be discarded while re-syncing a misaligned stream
# before the connection is dropped.  One frame is 54 bytes, so a shift of
# up to a full frame is recoverable; a stream that stays broken is dropped
# so the peer can reconnect with a fresh, aligned one.
_MAX_RESYNC = 54

# flags bit 2: the sender streams idle heartbeats.
_FLAG_STREAMING = 0x04
# flags bit 3: byte 52 holds a valid checksum.
_FLAG_CHECKSUM = 0x08

# XInput dead-zone constants
DEADZONE_STICK = int(0.15 * 32767)  # ~15 % dead-zone, matches Xbox spec
DEADZONE_TRIGGER = int(0.01 * 255)  # near-zero trigger threshold


def is_valid_frame(packet: bytes, require_checksum: bool = False) -> bool:
    """True when *packet* is a plausible v2 state frame.

    The wire format is a sequence of fixed-size frames, so a frame that was
    cut short on the wire shifts every frame that follows it.  The proto
    byte alone cannot detect that: a shifted window can carry a perfectly
    plausible proto byte, and decoding it would hand the game button
    presses that nobody made (the "it crashes when I play" symptom).

    Every field with a fixed meaning is therefore validated:

    * the proto byte,
    * the D-pad (a 4 bit mask),
    * the battery level (0-100 or "unknown"),
    * the flags byte (only charging / battery-valid / streaming /
      checksum-present are defined),
    * the reserved bytes (they are always zero),
    * the checksum — always, when the sender announces it with flag bit 3.

    *require_checksum* makes the announcement mandatory.  It is used for a
    peer that is known to send checksums: then every window is verified
    and a shifted window can never pass, which is what makes the byte-by-
    byte re-sync safe.
    """
    if len(packet) < _PAYLOAD_SIZE:
        return False
    if packet[13] != _PROTO_V2:
        return False
    if packet[12] & 0xF0:                 # D-pad is a 4 bit mask
        return False
    battery = packet[14]
    if battery != 0xFF and battery > 100:
        return False
    if packet[15] & 0xF0:                 # only bits 0..3 are defined
        return False
    if any(packet[40:_CHECKSUM_OFFSET]):  # reserved
        return False
    if packet[53]:                        # reserved
        return False
    has_checksum = bool(packet[15] & _FLAG_CHECKSUM)
    if require_checksum and not has_checksum:
        return False
    if has_checksum:
        if packet[_CHECKSUM_OFFSET] != (sum(packet[:_CHECKSUM_SPAN]) & 0xFF):
            return False
    return True


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
        # Keepalive is the reserved all-0xFF frame.  A controller axis can
        # legitimately have 0xFF as its first byte, so checking only byte 0
        # silently drops real input frames (including button releases).
        if len(data) >= _PAYLOAD_SIZE and data[:_PAYLOAD_SIZE] == _PING_PAYLOAD:
            return None

        if len(data) >= _PAYLOAD_SIZE and data[13] == _PROTO_V2:
            # A v2 frame that does not validate is a misaligned window, not
            # controller input — decoding it would invent button presses.
            if not is_valid_frame(data[:_PAYLOAD_SIZE]):
                return None
            return StateParser._parse_v2(data)
        if _PAYLOAD_V1_SIZE <= len(data) < _PAYLOAD_SIZE:
            # Only a genuinely short frame may be a legacy v1 frame.  A
            # full-size frame with an unexpected proto byte is a misaligned
            # window — decoding its first 14 bytes would invent button
            # presses.
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
        self._conn_generation = 0
        self._conn_lock = threading.Lock()
        self._packet_count = 0
        # Counts the frames that had to be discarded because the stream was
        # misaligned — used to rate-limit the warning.
        self._misaligned = 0
        # True once the peer showed that it keeps streaming the state even
        # when the controller does nothing (flag bit 2).  Only such a peer
        # may be watched with the state watchdog.
        self._peer_streams = False
        # None = unknown, True = the peer sends checksummed frames,
        # False = a legacy peer without checksums.  The first frame of a
        # connection decides which it is.
        self._peer_checksums: bool | None = None
        # Trace state: the last button/D-pad signature that was received
        # and the time of the last trace line.
        self._trace_sig: tuple | None = None
        self._trace_at = 0.0

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
                # The caller is ViGEm's notification thread, which runs
                # while the emitter holds its pad lock.  Blocking here
                # would freeze the whole input pipeline, so only send when
                # the socket can take the data right away.
                _, writable, _ = select.select([], [conn], [], _SEND_TIMEOUT_S)
                if not writable:
                    logger.debug("send_state: peer is not reading — dropping packet")
                    return False
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
                    self._conn_generation += 1
                    generation = self._conn_generation
                if self.on_connect:
                    try:
                        trace.event("CONNECT from %s", addr[0])
                        self.on_connect(addr[0])
                    except Exception:
                        pass
                threading.Thread(
                    target=self._serve,
                    args=(conn, addr, generation),
                    name=f"TCPClient-{addr[0]}",
                    daemon=True,
                ).start()
            except socket.timeout:
                continue
            except OSError as exc:
                if self._running:
                    logger.warning("Accept error: %s", exc)

    def _trace_frame(self, packet: bytes) -> None:
        """Write a trace line for a frame that was received.

        A line is written whenever the button / D-pad state changes and
        once per second otherwise, so the trace shows every transition
        without flooding the file.
        """
        sig = (packet[10], packet[11], packet[12])
        now = time.monotonic()
        if sig == self._trace_sig and now - self._trace_at < 1.0:
            return
        self._trace_sig = sig
        self._trace_at = now
        trace.state("RX", packet[10], packet[11], packet[12], packet[8], packet[9])

    def _resync_step(self, packet: bytes) -> bool:
        """Count one discarded byte of a misaligned stream.

        Returns False when the re-sync has given up — the caller then drops
        the connection so the peer can start a fresh, aligned stream.
        """
        self._misaligned += 1
        trace.event("INVALID FRAME (proto 0x%02x) — re-syncing, %d bytes dropped",
                    packet[13], self._misaligned)
        if self._misaligned > _MAX_RESYNC:
            logger.warning(
                "Could not re-sync the frame stream after %d bytes — "
                "dropping the connection", self._misaligned,
            )
            return False
        if self._misaligned <= 3 or self._misaligned % 64 == 0:
            logger.warning(
                "Invalid frame (proto 0x%02x) — stream misaligned, "
                "discarding 1 byte to re-sync (%d frames dropped)",
                packet[13], self._misaligned,
            )
        else:
            logger.debug(
                "Invalid frame — discarding 1 byte to re-sync "
                "(%d frames dropped)", self._misaligned,
            )
        return True

    def _serve(self, conn: socket.socket, addr: tuple, generation: int) -> None:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # SO_RCVBUF tuned to 32 KB — absorbs micro-bursts without dropping frames
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32768)
        buf = b""
        # Every connection starts with a clean slate: the flags of the
        # previous peer must not be inherited.
        self._peer_streams = False
        self._peer_checksums = None
        # True once a byte of the stream was discarded — the frame boundary
        # is no longer known for sure.
        self._resynced = False
        # True while the next window starts at a known frame boundary (the
        # start of the stream or right after a frame was accepted).  A
        # window that violates the announced mode *at a boundary* is a
        # frame the peer really sent — the connection is dropped then.
        self._at_boundary = True
        misaligned = False
        try:
            while self._running:
                chunk = conn.recv(4096)
                if not chunk:
                    logger.info("Client %s disconnected", addr[0])
                    break
                buf += chunk

                # Process complete v2 packets (54 B).
                # v2 packets are identified by proto byte at offset 13 == 0x02.
                # We intentionally do NOT fall back to v1 (24-byte) parsing here:
                # any sub-54-byte tail left in the buffer is either a partial v2
                # packet still in-flight (kept for the next recv() call) or
                # genuine garbage — neither should be parsed as a v1 frame,
                # because doing so causes ghost button presses and extreme stick
                # values when v2 packet tails are mis-interpreted as v1 headers.
                processed = 0
                # 128 frames per round is more than one recv() can deliver
                # (4096 B = 75 frames), so the buffer always drains.
                while len(buf) >= _PAYLOAD_SIZE and processed < 128:
                    packet = buf[:_PAYLOAD_SIZE]
                    if packet == _PING_PAYLOAD:
                        buf = buf[_PAYLOAD_SIZE:]
                        if self.on_ping:
                            try: self.on_ping()
                            except Exception: pass
                        processed += 1
                        continue
                    has_checksum = bool(packet[15] & _FLAG_CHECKSUM)
                    if self._peer_checksums is None:
                        # The mode is not known yet: the first frame that
                        # *validates* decides it.
                        #
                        # As long as nothing was discarded the stream is
                        # still aligned, so a legacy frame (no checksum)
                        # may be accepted.  After a re-sync only a fully
                        # verified frame may be: a legacy frame cannot be
                        # told apart from a shifted window, and a shifted
                        # window must never reach the game.
                        if not is_valid_frame(packet,
                                              require_checksum=self._resynced):
                            if not self._resync_step(packet):
                                misaligned = True
                                break
                            buf = buf[1:]
                            self._resynced = True
                            self._at_boundary = False
                            processed += 1
                            continue
                        self._peer_checksums = has_checksum
                        trace.event("PEER MODE %s",
                                    "checksummed" if has_checksum else "legacy")
                    elif not is_valid_frame(
                            packet, require_checksum=bool(self._peer_checksums)):
                        if not self._peer_checksums:
                            # A peer without frame checksums: nothing tells a
                            # shifted window from a real frame, so the
                            # connection is dropped instead of decoding
                            # garbage as button input.  The Linux side
                            # reconnects and starts a fresh, aligned stream.
                            logger.warning(
                                "Misaligned stream from a peer without frame "
                                "checksums — dropping the connection"
                            )
                            trace.event("MISALIGNED legacy stream — dropping "
                                        "the connection")
                            misaligned = True
                            break
                        if self._at_boundary:
                            # The peer announced checksums and now sends a
                            # frame without one: it violates the mode it
                            # announced.  The frame is not shifted, so a
                            # re-sync cannot fix it — drop the connection
                            # instead of silently discarding the stream
                            # forever.
                            logger.warning(
                                "Peer stopped announcing the frame checksum "
                                "— dropping the connection"
                            )
                            trace.event("PEER stopped announcing the checksum "
                                        "— dropping the connection")
                            misaligned = True
                            break
                        # The peer sends checksums: every window is verified,
                        # so re-syncing is safe — a window that is shifted
                        # can never pass the validation.
                        if not self._resync_step(packet):
                            misaligned = True
                            break
                        buf = buf[1:]
                        processed += 1
                        continue
                    self._misaligned = 0
                    self._at_boundary = True
                    state = StateParser.parse(packet)
                    buf = buf[_PAYLOAD_SIZE:]
                    if packet[15] & _FLAG_STREAMING:
                        # Only the *current* connection may set the flag —
                        # a previous one may still be winding down.
                        if self._conn_generation == generation:
                            self._peer_streams = True
                    if state is not None:
                        self._packet_count += 1
                        if packet[10] or packet[11] or packet[12]:
                            logger.debug("RX button frame #%d: buttons=%s",
                                         self._packet_count, packet[10:13].hex())
                        self._trace_frame(packet)
                        try: self.on_state(state)
                        except Exception as exc:
                            logger.error("on_state callback error: %s", exc)
                    processed += 1
                if misaligned:
                    break
                # Any remaining bytes are a partial v2 packet — leave them in
                # buf so the next recv() call can complete the frame.
        except OSError as exc:
            logger.info("Connection error from %s: %s", addr[0], exc)
        finally:
            active = False
            with self._conn_lock:
                if self._conn is conn and self._conn_generation == generation:
                    self._conn = None
                    active = True
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
            logger.info("Connection closed: %s", addr[0])
            if active and self.on_disconnect:
                try:
                    self.on_disconnect(addr[0])
                except Exception:
                    pass