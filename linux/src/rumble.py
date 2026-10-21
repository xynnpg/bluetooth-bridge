"""Writes Xbox One/Series rumble commands to the controller.

The Xbox Wireless controller over Bluetooth on Linux is exposed as a
``/dev/input/eventN`` node by the kernel's ``uhid`` driver.  Unlike USB
controllers, it does **not** create a sibling ``/dev/hidrawN`` device
that we can write raw output reports to.  For Bluetooth-attached
controllers, rumble therefore has to go through the kernel's
generic Force-Feedback interface (``EV_FF`` / ``FF_RUMBLE``), which the
input device exposes directly.

For USB-attached controllers (or any controller that *does* present a
``/dev/hidrawN`` node), the original raw-report writer is still used
because it gives finer-grained control over the motor format and
doesn't depend on a quirked-up FF driver.

Two backends share the same ``apply(left, right)`` / ``close()``
interface and are selected automatically based on the device path:

* ``HidrawRumbleWriter`` — writes the controller-specific 9-byte
  (model 1708+) or 13-byte (model 1697) output report.  The format is
  auto-detected from the device's PRODUCT id.
* ``ForceFeedbackRumbleWriter`` — uploads a single indefinite
  ``FF_RUMBLE`` effect and updates its ``strong_magnitude`` /
  ``weak_magnitude`` on every ``apply()`` call.

Public entry points:

* :func:`find_hidraw_for` / :func:`find_hidraw_by_mac` — locate a
  hidraw node for the controller (USB-style).  Returns ``None`` for
  pure-Bluetooth controllers that have no hidraw sibling.
* :func:`is_force_feedback_available` — check whether a given
  ``/dev/input/eventN`` device exposes ``FF_RUMBLE``.
* :class:`RumbleWriter` — facade.  ``set_device(path)`` dispatches to
  the appropriate backend based on whether ``path`` is a hidraw or
  input-event node.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import struct
from typing import Optional

import evdev

logger = logging.getLogger("rumble")

# --- Xbox Wireless (model 1708, 1797, 1914) — 9-byte output report ----------
# Layout from drivers/hid/hid-microsoft.c:
#   report_id=0x03, enable=0x03, strong, weak, duration=0xFF, delay=0, loop=0xFF
_WIRELESS_RUMBLE = struct.Struct("<BBBBBBBBB")
_WIRELESS_INIT   = bytes([0x03, 0x03, 0x00, 0x00, 0x00, 0x00, 0xFF, 0x00, 0xFF])

# --- Xbox One original (045E:02D1 / 02EA / 02FD) — 13-byte output report ----
_ONE_RUMBLE = struct.Struct("<BBBBBBBBBBBBB")
_ONE_INIT   = bytes([0x09, 0x00, 0x00, 0x09, 0x00, 0x0F,
                     0x00, 0x00, 0x00, 0x00, 0x80, 0x00, 0x00])

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


# ---------------------------------------------------------------------------
# Device discovery
# ---------------------------------------------------------------------------

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

    Note: for Bluetooth-only Xbox Wireless controllers the kernel's uhid
    driver does NOT create a hidraw sibling at all.  In that case this
    function returns ``None`` and the caller should fall back to
    :func:`is_force_feedback_available` + the input device.
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


def is_force_feedback_available(evdev_path: str) -> bool:
    """Return True if the input device at *evdev_path* exposes ``FF_RUMBLE``.

    Used as the fallback path for Bluetooth controllers whose kernel
    driver does not create a hidraw sibling.  ``EV_FF`` is exposed by
    the ``hid-microsoft`` driver for the Xbox Wireless controller and
    shows up in ``/sys/class/input/eventN/device/capabilities/ev``.
    """
    try:
        dev = evdev.InputDevice(evdev_path)
    except (FileNotFoundError, OSError) as exc:
        logger.debug("is_force_feedback_available: cannot open %s: %s",
                     evdev_path, exc)
        return False
    try:
        ff_types = dev.capabilities(absinfo=False).get(evdev.ecodes.EV_FF, [])
        # `ff_types` is a list of (code, AbsInfo) tuples or just codes
        # depending on evdev version.  Normalise to a set of ints.
        codes = {c[0] if isinstance(c, tuple) else c for c in ff_types}
        return evdev.ecodes.FF_RUMBLE in codes
    except OSError as exc:
        logger.debug("is_force_feedback_available: capabilities() failed: %s", exc)
        return False
    finally:
        try:
            dev.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Backend: raw hidraw output reports
# ---------------------------------------------------------------------------

class HidrawRumbleWriter:
    """Writes rumble output reports to a ``/dev/hidraw*`` node."""

    def __init__(self) -> None:
        self._path: str | None = None
        self._fd: int | None = None
        self._last = (-1, -1)
        # 'wireless' = 9-byte report (0x03, model 1708+); 'one' = 13-byte
        # legacy (0x09, model 1697 / original Xbox One).
        self._format: str = "wireless"
        self._wireless = _WIRELESS_RUMBLE
        self._one = _ONE_RUMBLE
        self._init_pkt: bytes = _WIRELESS_INIT

    def set_device(self, hidraw_path: str) -> bool:
        """Open the hidraw node. Returns True on success, False on failure."""
        if self._path == hidraw_path and self._fd is not None:
            return True
        self.close()
        self._path = hidraw_path
        if not self._detect_format():
            self._path = None
            return False
        return self._open()

    def _detect_format(self) -> bool:
        """Pick the rumble output-report format based on the controller's
        PRODUCT id (from the hidraw device's parent). For model 1708/1797/
        1914 (XBOX Wireless) we use the 9-byte report; for older Xbox One
        controllers (model 1697 / 1707 / PID 0x02D1 / 02EA / 02FD) we fall
        back to the legacy 13-byte report.

        We don't fail the bridge if we can't tell — the wireless format is
        the safe modern default.
        """
        try:
            base = os.path.realpath(os.path.join(
                "/sys/class/hidraw", os.path.basename(self._path or ""),
                "device"))
            for _ in range(6):
                modalias = os.path.join(base, "modalias")
                if os.path.exists(modalias):
                    with open(modalias, encoding="ascii") as f:
                        text = f.read()
                    # modalias format examples:
                    #   hid:b0005g0001v0000045Ep00000B20
                    #   bluetooth:0005v045Ep0B20e0521
                    # We just need to find a 4-hex-digit PID adjacent to p.
                    import re as _re
                    m = _re.search(r"v[\dA-Fa-f]{4}p([0-9A-Fa-f]{4})", text)
                    if m:
                        pid = int(m.group(1), 16)
                        logger.debug("rumble: detected PID 0x%04x from %s",
                                     pid, modalias)
                        if pid in (0x02D1, 0x02EA, 0x02FD):
                            self._format = "one"
                            self._init_pkt = _ONE_INIT
                            logger.info("Rumble format: legacy Xbox One (13-byte)")
                            return True
                    # Found a modalias but no match → modern
                    self._format = "wireless"
                    self._init_pkt = _WIRELESS_INIT
                    logger.info("Rumble format: Xbox Wireless (9-byte)")
                    return True
                base = os.path.dirname(base)
        except OSError as exc:
            logger.debug("_detect_format: %s", exc)
        # Couldn't determine — use modern default.
        self._format = "wireless"
        self._init_pkt = _WIRELESS_INIT
        return True

    def _open(self) -> bool:
        if not self._path:
            return False
        try:
            self._fd = os.open(self._path, os.O_WRONLY | os.O_NONBLOCK)
            # Initialise motor state to zero (some controllers won't rumble
            # until the first report is sent after reconnect)
            os.write(self._fd, self._init_pkt)
            self._last = (0, 0)
            logger.info("HidrawRumbleWriter attached to %s", self._path)
            return True
        except OSError as exc:
            logger.warning("Could not open %s for rumble: %s", self._path, exc)
            self._fd = None
            return False

    def apply(self, left: int, right: int) -> None:
        """Send a rumble report if the motor speeds changed."""
        left  = max(0, min(255, int(left)))
        right = max(0, min(255, int(right)))
        if (left, right) == self._last:
            return
        self._last = (left, right)

        if self._fd is None:
            return
        if self._format == "one":
            report = self._one.pack(
                0x09, 0x00, 0x00, 0x09, 0x00, 0x0F,
                left,  right,
                0x00, 0x00, 0x80, 0x00, 0x00,
            )
        else:
            strong = max(0, min(100, round(left  * 100 / 255)))
            weak   = max(0, min(100, round(right * 100 / 255)))
            report = self._wireless.pack(
                0x03, 0x03, 0x00, 0x00, strong, weak, 0xFF, 0x00, 0xFF,
            )
        try:
            os.write(self._fd, report)
        except OSError as exc:
            logger.debug("Rumble write failed: %s", exc)
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def close(self) -> None:
        """Stop rumble and release the file descriptor."""
        if self._fd is not None:
            try:
                os.write(self._fd, self._init_pkt)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            logger.info("HidrawRumbleWriter detached")


# ---------------------------------------------------------------------------
# Backend: Linux Force-Feedback interface (works for Bluetooth controllers)
# ---------------------------------------------------------------------------

class ForceFeedbackRumbleWriter:
    """Writes rumble via the kernel's ``EV_FF`` / ``FF_RUMBLE`` interface.

    On Bluetooth-only Xbox Wireless controllers the kernel's uhid driver
    exposes the device as ``/dev/input/eventN`` with an ``EV_FF`` capability
    of ``FF_RUMBLE`` but **no** sibling ``/dev/hidrawN`` node.  In that
    case rumble is sent by uploading a single indefinite
    ``FF_RUMBLE`` effect and updating its ``strong_magnitude`` /
    ``weak_magnitude`` on every change.

    The effect is created with ``replay.length = 0`` (= "play until
    stopped / re-uploaded") and ``replay.delay = 0``.  Re-uploading with
    the same effect id updates the magnitudes in-place without needing
    a separate play/stop sequence.
    """

    # Force-feedback magnitude range is 0..0xFFFF (16-bit).  We map our
    # 0..255 input range onto 0..0xFFFF, leaving the top quarter of
    # the range as headroom — most FF devices saturate before then.
    _MAX_FF = 0xFFFF

    def __init__(self) -> None:
        self._path: str | None = None
        self._dev: evdev.InputDevice | None = None
        self._last = (-1, -1)
        # Cache the effect struct so we can re-upload with the same id
        # (kernel interprets re-upload of an existing id as an update).
        self._effect: evdev.ff.Effect | None = None
        self._effect_id: int | None = None

    def set_device(self, evdev_path: str) -> bool:
        """Open the input device and prepare a permanent FF_RUMBLE effect.

        Returns True on success, False if the device has no FF_RUMBLE
        capability (caller should try a different backend or skip).
        """
        if self._path == evdev_path and self._dev is not None:
            return True
        self.close()
        self._path = evdev_path
        return self._open()

    def _open(self) -> bool:
        try:
            self._dev = evdev.InputDevice(self._path)
        except (FileNotFoundError, OSError) as exc:
            logger.warning("Could not open %s for FF rumble: %s",
                           self._path, exc)
            self._dev = None
            return False

        # Verify FF_RUMBLE is present.  We don't check upfront because
        # some kernels surface FF capabilities only after the device is
        # open.  If absent, fail cleanly.
        try:
            ff_types = self._dev.capabilities(absinfo=False).get(
                evdev.ecodes.EV_FF, []
            )
            codes = {c[0] if isinstance(c, tuple) else c for c in ff_types}
        except OSError as exc:
            logger.warning("FF capability query failed: %s", exc)
            self._dev.close()
            self._dev = None
            return False

        if evdev.ecodes.FF_RUMBLE not in codes:
            logger.info("Device %s has no FF_RUMBLE capability", self._path)
            self._dev.close()
            self._dev = None
            return False

        # Build the effect struct: indefinite replay, zero magnitudes
        # (initial state — apply() will set the real values).
        self._effect = evdev.ff.Effect(
            type=evdev.ecodes.FF_RUMBLE,
            id=-1,           # -1 → kernel assigns a new id
            direction=0,
            ff_trigger=evdev.ff.Trigger(button=0, interval=0),
            ff_replay=evdev.ff.Replay(length=0, delay=0),  # 0 = play forever
            u=evdev.ff.EffectType(
                ff_rumble_effect=evdev.ff.Rumble(
                    strong_magnitude=0,
                    weak_magnitude=0,
                ),
            ),
        )

        try:
            self._effect_id = self._dev.upload_effect(self._effect)
            self._last = (0, 0)
            logger.info(
                "ForceFeedbackRumbleWriter attached to %s (effect id %d)",
                self._path, self._effect_id,
            )
            return True
        except OSError as exc:
            logger.warning("Failed to upload FF_RUMBLE effect: %s", exc)
            self._effect = None
            self._effect_id = None
            self._dev.close()
            self._dev = None
            return False

    def apply(self, left: int, right: int) -> None:
        """Update the FF_RUMBLE effect's strong/weak magnitudes.

        Re-uploading the effect with the same id updates it in-place.
        Most kernels will keep the effect playing across updates, but a
        few require the effect to be erased+re-uploaded; in that case
        we fall back to a clean re-upload.
        """
        left  = max(0, min(255, int(left)))
        right = max(0, min(255, int(right)))
        if (left, right) == self._last:
            return
        self._last = (left, right)

        if self._dev is None or self._effect is None or self._effect_id is None:
            return

        strong = round(left  * self._MAX_FF / 255)
        weak   = round(right * self._MAX_FF / 255)
        self._effect.u.ff_rumble_effect.strong_magnitude = strong
        self._effect.u.ff_rumble_effect.weak_magnitude   = weak
        # Preserve the existing effect id so the kernel updates
        # in-place rather than allocating a new effect.
        self._effect.id = self._effect_id
        try:
            self._dev.upload_effect(self._effect)
        except OSError as exc:
            logger.debug("FF_RUMBLE update failed: %s", exc)
            # Effect may have been lost (controller reset etc.) —
            # try a clean re-upload once.
            try:
                self._effect.id = -1
                self._effect_id = self._dev.upload_effect(self._effect)
            except OSError as exc2:
                logger.debug("FF_RUMBLE re-upload failed: %s", exc2)
                # Give up; the next set_device() will try fresh.
                try:
                    self._dev.close()
                except Exception:
                    pass
                self._dev = None
                self._effect_id = None

    def close(self) -> None:
        """Stop the FF effect and release the input device."""
        if self._dev is not None and self._effect_id is not None:
            try:
                self._dev.erase_effect(self._effect_id)
            except OSError as exc:
                logger.debug("erase_effect failed: %s", exc)
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:
                pass
        if self._dev is not None or self._effect_id is not None:
            logger.info("ForceFeedbackRumbleWriter detached")
        self._dev = None
        self._effect = None
        self._effect_id = None
        self._last = (-1, -1)


# ---------------------------------------------------------------------------
# Facade: choose the right backend based on the path the caller gives us
# ---------------------------------------------------------------------------

class RumbleWriter:
    """High-level rumble writer.

    ``set_device(path)`` accepts either:

    * ``/dev/hidrawN``  → :class:`HidrawRumbleWriter` (raw output reports)
    * ``/dev/input/eventN`` → :class:`ForceFeedbackRumbleWriter` (FF_RUMBLE)

    The bridge first tries to locate a hidraw node for the controller;
    if none exists (typical for Bluetooth-only controllers) it falls
    back to the input device path which exposes ``FF_RUMBLE``.
    """

    def __init__(self) -> None:
        self._hidraw = HidrawRumbleWriter()
        self._ff = ForceFeedbackRumbleWriter()
        self._active: object | None = None
        self._last = (-1, -1)

    def set_device(self, path: str) -> bool:
        """Attach to the device at *path*.

        Returns True if a working rumble backend was attached, False
        otherwise.  The caller should treat False as "rumble disabled
        for this controller" and not retry on every frame.
        """
        if path.startswith("/dev/hidraw"):
            target = self._hidraw
        elif path.startswith("/dev/input/event"):
            target = self._ff
        else:
            logger.warning("RumbleWriter.set_device: unsupported path %s", path)
            return False

        ok = target.set_device(path)
        if ok:
            # Closing the other backend avoids holding two file
            # descriptors open for the same controller.
            other = self._ff if target is self._hidraw else self._hidraw
            other.close()
            self._active = target
            # Reset the dedupe so the new backend gets the first frame
            # even if it's the same (left, right) we last sent.
            self._last = (-1, -1)
            target._last = (-1, -1)  # type: ignore[attr-defined]
        else:
            # Leave whichever backend is currently active in place.
            logger.warning("RumbleWriter: could not attach to %s", path)
        return ok

    def apply(self, left: int, right: int) -> None:
        """Forward to the active backend."""
        if self._active is None:
            return
        left  = max(0, min(255, int(left)))
        right = max(0, min(255, int(right)))
        if (left, right) == self._last:
            return
        self._last = (left, right)
        self._active.apply(left, right)  # type: ignore[attr-defined]

    def close(self) -> None:
        """Stop rumble and release all backends."""
        self._hidraw.close()
        self._ff.close()
        self._active = None
        self._last = (-1, -1)
