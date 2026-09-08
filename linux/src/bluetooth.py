"""Bluetooth utilities — pairing, connection, and trust management via bluetoothctl."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time

logger = logging.getLogger("bluetooth")

_BLUEZCTL = "/usr/bin/bluetoothctl"
_DBUS_LAUNCH = "/usr/bin/dbus-launch"


def _runctl(args: list[str], check: bool = False) -> subprocess.CompletedProcess:
    """Run bluetoothctl with optional dbus session."""
    full = [_BLUEZCTL] + args
    try:
        # Try dbus-run-session if available
        result = subprocess.run(
            ["/usr/bin/dbus-run-session", "--"] + full,
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        result = subprocess.run(
            full,
            capture_output=True, text=True, timeout=15,
        )
    if check and result.returncode != 0:
        logger.error("bluetoothctl %s failed: %s", args, result.stderr)
        raise subprocess.CalledProcessError(result.returncode, full)
    return result


def start_bluetooth_service() -> bool:
    """Ensure Bluetooth daemon is powered on using bluetoothctl."""
    # Ensure dbus-daemon is running (should be already in container)
    # Try to power on the adapter via bluetoothctl
    result = _runctl(["power", "on"])
    if result.returncode == 0:
        logger.info("Bluetooth powered on")
        return True
    # If that fails, try to start bluetoothd explicitly
    logger.info("Attempting to start bluetoothd daemon...")
    try:
        # Start dbus-daemon if not running
        subprocess.Popen(["dbus-daemon", "--system", "--nofork"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        # Start bluetoothd
        subprocess.Popen(["/usr/lib/bluetooth/bluetoothd", "--noplugin=sap", "--nointeractive"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
        # Retry power on
        result = _runctl(["power", "on"])
        if result.returncode == 0:
            logger.info("Bluetooth powered on after starting daemon")
            return True
    except Exception as e:
        logger.warning("Failed to start bluetoothd: %s", e)
    logger.warning("bluetoothctl power on failed: %s", result.stderr)
    return False


def scan_for_devices(timeout: float = 10.0) -> dict[str, str]:
    """Scan for nearby Bluetooth devices. Returns {mac: name}."""
    _runctl(["scan", "off"])     # clear any previous scan
    _runctl(["scan", "on"])
    logger.info("Scanning for devices (%ss) …", timeout)
    time.sleep(timeout)
    _runctl(["scan", "off"])

    # List devices already known to the adapter
    out = _runctl(["devices"]).stdout
    devices: dict[str, str] = {}
    for line in out.strip().splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) >= 2 and len(parts[1]) == 17:  # MAC address
            mac, name = parts[1], parts[2] if len(parts) > 2 else "(unknown)"
            devices[mac.lower()] = name
    return devices


def find_xbox_device(devices: dict[str, str]) -> str | None:
    """Return MAC of the first Xbox controller in the device list."""
    for mac, name in devices.items():
        nl = name.lower()
        if any(k in nl for k in ("xbox", "microsoft", "controller")):
            logger.info("Found Xbox device: %s (%s)", name, mac)
            return mac
    return None


def pair(mac: str) -> bool:
    """Pair with a device by MAC."""
    logger.info("Pairing with %s …", mac)
    result = _runctl(["pair", mac])
    if result.returncode != 0:
        logger.error("Pairing failed: %s", result.stderr)
        return False
    logger.info("Paired with %s", mac)
    return True


def trust(mac: str) -> bool:
    """Trust the device so it auto-connects."""
    result = _runctl(["trust", mac])
    return result.returncode == 0


def connect(mac: str) -> bool:
    """Establish a GATT/ HID connection to the controller."""
    logger.info("Connecting to %s …", mac)
    result = _runctl(["connect", mac])
    if result.returncode != 0:
        logger.warning("Connect command result: %s", result.stderr)
        # bluetoothctl connect can return non-zero even on success
    time.sleep(2)
    return True


def disconnect(mac: str) -> bool:
    """Disconnect a device."""
    result = _runctl(["disconnect", mac])
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Battery polling (fallback for kernels that don't emit MSC_INPUT events)
# ---------------------------------------------------------------------------

_BATTERY_RE = re.compile(
    r"Battery\s*Percentage\s*[:=]?\s*\(?0x[\da-f]+\)?\s*\(?\s*(\d+)\s*%\s*\)?",
    re.IGNORECASE,
)
# More permissive pattern — matches lines like:
#   Battery Percentage: 0x05 (5%)
#   Battery Level: 87
_BATTERY_RE_LOOSE = re.compile(
    r"[Bb]attery[^:\n]*[:=]\s*(?:0x[\da-f]+\s*)?\(?(\d{1,3})\s*%?",
)


def get_battery_pct(mac: str) -> tuple[int, bool] | None:
    """Return (percentage, charging) by parsing `bluetoothctl info <MAC>`.

    Returns None if the info is not available. This is a slow poll
    (one shell-out per call, ~200 ms) — only call it at 1 Hz from a
    background thread, not on the hot evdev path.
    """
    try:
        result = _runctl(["info", mac])
    except Exception as exc:
        logger.debug("bluetoothctl info failed: %s", exc)
        return None
    if result.returncode != 0:
        return None
    text = result.stdout or ""

    pct = None
    m = _BATTERY_RE.search(text) or _BATTERY_RE_LOOSE.search(text)
    if m:
        try:
            pct = max(0, min(100, int(m.group(1))))
        except ValueError:
            pct = None
    if pct is None:
        return None

    # "Charging" detection — bluetoothctl doesn't expose this on most stacks,
    # so we fall back to "charging if a power supply is mentioned"
    charging = bool(re.search(r"charging|power\s*supply", text, re.IGNORECASE))
    return pct, charging


def ensure_paired(mac: str | None = None) -> str:
    """Fully manage pairing: start BT, scan, findXbox, pair+trust, connect.

    If mac is provided, skip discovery and use that address directly.
    Returns the controller MAC on success.
    """
    if mac and len(mac) == 17:
        # Still need to start the BT service
        start_bluetooth_service()
        _runctl(["connect", mac])
        return mac.lower()

    start_bluetooth_service()
    devices = scan_for_devices(timeout=12)
    found = find_xbox_device(devices)
    if not found:
        raise RuntimeError(
            "No Xbox controller found during scan. "
            "Make sure the controller is in pairing mode (hold the sync button)."
        )

    pair(found)
    trust(found)
    connect(found)
    return found