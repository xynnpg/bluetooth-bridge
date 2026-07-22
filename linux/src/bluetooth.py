"""Bluetooth utilities — pairing, connection, and trust management via bluetoothctl."""

from __future__ import annotations

import logging
import os
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