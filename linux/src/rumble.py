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

    Tries three strategies, in order of specificity:

    1. **Sibling** — ``/sys/class/input/eventN/device/hidraw/`` (works for
       most USB controllers).
    2. **Ancestor walk** — for BT controllers, the input event is created
       by ``uhid`` and the hidraw node lives at a higher level
       (``/sys/class/input/eventN/device/.../hidraw/hidrawN``). We walk the
       device tree upward until we find a sibling ``hidraw/`` directory,
       then descend into it.
    3. **MAC fallback** — if a ``CONTROLLER_MAC`` was passed, search
       ``/sys/class/hidraw/*/uevent`` for the MAC and match.

    Returns the first ``/dev/hidrawN`` that exists, or None.
    """
    try:
        name = os.path.basename(evdev_path)             # eventN
        base = os.path.realpath(os.path.join("/sys/class/input", name))
        if not os.path.isdir(base):
            return None

        # Strategy 1: direct sibling hidraw/ directory
        node = _scan_for_hidraw(base + "/device")
        if node:
            return node

        # Strategy 2: walk upward through the device tree
        cur = os.path.realpath(base + "/device")
        for _ in range(6):  # limit depth — BT chains are usually ≤ 4
            parent = os.path.dirname(cur)
            if parent == cur or not parent:
                break
            node = _scan_for_hidraw(parent)
            if node:
                return node
            cur = parent
    except OSError as exc:
        logger.debug("find_hidraw_for: %s", exc)
    return None


def _scan_for_hidraw(dev_dir: str) -> str | None:
    """Look in dev_dir for either a hidrawN entry, or a hidraw/ subdir
    containing hidrawN entries.  Returns the resolved /dev/hidrawN path."""
    try:
        entries = os.listdir(dev_dir)
    except OSError:
        return None

    # Direct child: hidrawN
    for e in entries:
        if e.startswith("hidraw") and e[len("hidraw"):].isdigit():
            return _resolve_hidraw(e)

    # Child directory named "hidraw" (e.g. .../0005:045E:0B20.0024/hidraw/hidraw1)
    if "hidraw" in entries:
        for e in _list_dir(dev_dir + "/hidraw"):
            if e.startswith("hidraw") and e[len("hidraw"):].isdigit():
                return _resolve_hidraw(e)
    return None


def _list_dir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def _resolve_hidraw(name: str) -> str | None:
    devnode = f"/dev/{name}"
    if os.path.exists(devnode):
        logger.info("Found hidraw node: %s", devnode)
        return devnode
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
