"""Writes Xbox One/Series rumble output reports to the controller's hidraw node.

The Linux kernel's hid-microsoft driver exposes the controller's raw GATT
characteristic through a `/dev/hidraw*` device. We open it in non-blocking
mode and send the canonical 9-byte "Rumble" output report whenever the host
(Windo we side) sets a new motor speed.

Report layout (Xbox One S / Series X|S over Bluetooth):
    0x09 0x00 0x00 0x09 0x00 0x0F <left> <right> 0x00 0x00 0x80 0x00 0x00
"""

from __future__ import annotations

import glob
import logging
import os
import re
import struct

logger = logging.getLogger("rumble")

# Canonical Xbox One BT rumble output report (13 bytes).
# Source: linux/drivers/hid/hid-microsoft.c + community reverse-engineering.
_RUMBLE_REPORT = struct.Struct("<BBBBBBBBBBBBB")
_RUMBLE_INIT   = bytes([0x09, 0x00, 0x00, 0x09, 0x00, 0x0F,
                        0x00, 0x00, 0x00, 0x00, 0x80, 0x00, 0x00])

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def find_hidraw_for(evdev_path: str) -> str | None:
    """Return the /dev/hidraw* node that backs the given /dev/input/event* path.

    Walks /sys/class/input/eventN/device/hidraw/ to find the sibling hidraw
    node.  Returns None if no match is found (e.g. driver doesn't expose
    hidraw, or the device is in a permission-denied mount).
    """
    try:
        base = "/sys/class/input"
        name = os.path.basename(evdev_path)             # eventN
        hid_dir = os.path.realpath(os.path.join(base, name, "device", "hidraw"))
        if not os.path.isdir(hid_dir):
            return None
        for entry in sorted(os.listdir(hid_dir)):
            if entry.startswith("hidraw"):
                devnode = f"/dev/{entry}"
                if os.path.exists(devnode):
                    logger.info("Found hidraw node: %s", devnode)
                    return devnode
    except OSError as exc:
        logger.debug("find_hidraw_for: %s", exc)
    return None


def find_hidraw_by_mac(mac: str) -> str | None:
    """Fallback: match a hidraw node to a MAC address via /sys.

    Some kernels don't expose the input→hidraw link directly, so we walk
    every hidraw node and read its uevent for HID_NAME matching.
    """
    if not _MAC_RE.match(mac):
        return None
    mac_norm = mac.lower()
    for path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(path, "uevent"), encoding="utf-8") as f:
                text = f.read().lower()
            if mac_norm in text:
                node = f"/dev/{os.path.basename(path)}"
                if os.path.exists(node):
                    return node
        except OSError:
            continue
    return None


class RumbleWriter:
    """Writes rumble output reports to a /dev/hidraw* node."""

    def __init__(self, hidraw_path: str | None = None) -> None:
        self._path = hidraw_path
        self._fd: int | None = None
        self._last = (-1, -1)

    def set_device(self, hidraw_path: str) -> None:
        """(Re-)open the hidraw node. Safe to call after a controller reconnect."""
        if self._path == hidraw_path and self._fd is not None:
            return
        self.close()
        self._path = hidraw_path
        self._open()

    def _open(self) -> None:
        if not self._path:
            return
        try:
            self._fd = os.open(self._path, os.O_WRONLY | os.O_NONBLOCK)
            # Initialise motor state to zero (some controllers won't rumble
            # until the first report is sent after reconnect)
            os.write(self._fd, _RUMBLE_INIT)
            self._last = (0, 0)
            logger.info("Rumble writer attached to %s", self._path)
        except OSError as exc:
            logger.warning("Could not open %s for rumble: %s", self._path, exc)
            self._fd = None

    def apply(self, left: int, right: int) -> None:
        """Send a rumble report if the motor speeds changed.

        `left`/`right` are 0-255.  We throttle to "only when changed" so
        we don't hammer the hidraw node at 60 Hz for nothing.
        """
        left  = max(0, min(255, int(left)))
        right = max(0, min(255, int(right)))
        if (left, right) == self._last:
            return
        self._last = (left, right)

        if self._fd is None:
            return
        report = _RUMBLE_REPORT.pack(
            0x09, 0x00, 0x00, 0x09, 0x00, 0x0F,
            left,  right,
            0x00, 0x00, 0x80, 0x00, 0x00,
        )
        try:
            os.write(self._fd, report)
        except OSError as exc:
            logger.debug("Rumble write failed: %s", exc)
            # The controller may have disconnected — drop the fd; the next
            # apply() will reopen via set_device().
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def close(self) -> None:
        """Stop rumble and release the file descriptor."""
        if self._fd is not None:
            try:
                os.write(self._fd, _RUMBLE_INIT)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            logger.info("Rumble writer detached")
