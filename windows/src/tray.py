"""System tray manager for the Windows bridge app."""

from __future__ import annotations

import logging
import threading
from typing import Callable

try:
    import pystray
    from PIL import Image, ImageDraw
    _TRAY_AVAILABLE = True
except ImportError:
    _TRAY_AVAILABLE = False
    pystray = None
    Image = None

logger = logging.getLogger("tray")

# Brand colours
_COLOR_ONLINE  = (0, 180, 80)   # Green  — connected
_COLOR_WARN    = (220, 160, 0)   # Amber  — reconnecting
_COLOR_OFFLINE = (200, 50, 50)   # Red    — disconnected


def _make_icon(color_rgb: tuple[int, int, int], size: int = 64) -> Image.Image:
    """Generate a coloured circle icon for the system tray."""
    img = Image.new("RGB", (size, size), (30, 30, 30))
    draw = ImageDraw.Draw(img)
    margin = 6
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=color_rgb,
        outline=None,
    )
    # Small "X" letter in white
    draw.line([size//4, size//4, 3*size//4, 3*size//4], fill=(255,255,255), width=3)
    draw.line([3*size//4, size//4, size//4, 3*size//4], fill=(255,255,255), width=3)
    return img


class TrayManager:
    """Manages the pystray icon and updates the status text on changes."""

    def __init__(
        self,
        on_exit: Callable[[], None],
        on_restart_controller: Callable[[], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
    ):
        if not _TRAY_AVAILABLE:
            logger.warning("pystray / Pillow not available — tray disabled")
            self._icon = None
            return

        self._on_exit = on_exit
        self._on_restart_controller = on_restart_controller
        self._on_reconnect = on_reconnect
        self._icon: pystray.Icon | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()
        self._status_label = "Initialising …"
        self._log_path: str | None = None

    def set_log_path(self, path: str) -> None:
        """Path to the on-disk log file, for the View Logs menu item."""
        self._log_path = path

    def start(self) -> None:
        if not _TRAY_AVAILABLE or self._icon is not None:
            return
        self._running = True
        self._icon = pystray.Icon(
            "xbox_bridge",
            _make_icon(_COLOR_WARN),
            "Xbox Bridge",
            self._build_menu(),
        )
        self._thread = threading.Thread(target=self._run, name="TrayIcon", daemon=True)
        self._thread.start()
        logger.info("Tray icon started")

    def _run(self) -> None:
        self._icon.run()

    def _build_menu(self):
        # Lazy imports so tray still works without tkinter
        return pystray.Menu(
            pystray.MenuItem("Xbox Bridge", None, enabled=False),
            pystray.MenuItem(lambda _, __: self._status_label, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("View Logs…", self._handle_view_logs),
            pystray.MenuItem("Copy Logs", self._handle_copy_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart Controller", self._handle_restart_controller),
            pystray.MenuItem("Reconnect", self._handle_reconnect),
            pystray.MenuItem("Settings…", self._handle_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Uninstall…", self._handle_uninstall),
            pystray.MenuItem("Exit", self._handle_exit),
        )

    def _handle_exit(self, _=None) -> None:
        logger.info("Tray exit requested")
        self._running = False
        if self._icon:
            self._icon.stop()
        self._on_exit()

    # ------------------------------------------------------------------
    # Extended menu handlers
    # ------------------------------------------------------------------

    def _handle_view_logs(self, _=None) -> None:
        """Open the log file in the default text editor."""
        if not self._log_path:
            logger.warning("Log path not set — cannot open logs")
            return
        import subprocess, os as _os
        try:
            _os.startfile(self._log_path)
        except AttributeError:
            # Non-Windows fallback
            subprocess.Popen(["notepad", self._log_path])
        except Exception as exc:
            logger.error("Failed to open log file: %s", exc)

    def _handle_copy_logs(self, _=None) -> None:
        """Copy the current log file contents to the clipboard."""
        if not self._log_path:
            logger.warning("Log path not set — cannot copy logs")
            return
        import pyperclip
        try:
            with open(self._log_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            pyperclip.copy(content[-65536:])  # last 64 KB to stay under clipboard limits
            logger.info("Log contents copied to clipboard")
        except Exception as exc:
            logger.error("Failed to copy log file: %s", exc)

    def _handle_restart_controller(self, _=None) -> None:
        """Request a vgamepad reset via registered callback."""
        self._icon.notify("Restarting virtual controller…", "Xbox Bridge")
        if self._on_restart_controller:
            try:
                self._on_restart_controller()
            except Exception as exc:
                logger.error("Restart controller callback error: %s", exc)
        else:
            logger.info("Restart controller requested — no callback registered")

    def _handle_reconnect(self, _=None) -> None:
        """Request a TCP reconnect via registered callback."""
        self._icon.notify("Reconnecting…", "Xbox Bridge")
        if self._on_reconnect:
            try:
                self._on_reconnect()
            except Exception as exc:
                logger.error("Reconnect callback error: %s", exc)
        else:
            logger.info("Reconnect requested — no callback registered")

    def _handle_settings(self, _=None) -> None:
        """Open the config file in Notepad."""
        import subprocess, os as _os, logging as _logging
        config = _os.path.join(
            _os.environ.get("LOCALAPPDATA",
                           _os.environ.get("USERPROFILE", ".")),
            "bluetooth_bridge", "config.ini"
        )
        if not _os.path.exists(config):
            config = _os.path.join(
                _os.environ.get("USERPROFILE", "."),
                "bluetooth_bridge", "config.ini"
            )
        try:
            _os.startfile(config)
        except AttributeError:
            subprocess.Popen(["notepad", config])
        except Exception as exc:
            _logging.error("Failed to open settings: %s", exc)

    def _handle_uninstall(self, _=None) -> None:
        """Launch the uninstaller."""
        import subprocess, os as _os, logging as _logging
        uninstaller = _os.path.join(
            _os.environ.get("USERPROFILE", "."),
            "bluetooth_bridge", "uninstall.ps1"
        )
        try:
            subprocess.Popen(
                ["powershell", "-ExecutionPolicy", "Bypass", "-File", uninstaller],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as exc:
            _logging.error("Failed to launch uninstaller: %s", exc)

    def update(self, *, connected: bool, pc_reachable: bool) -> None:
        """Call this whenever connection state changes."""
        if not _TRAY_AVAILABLE or not self._icon:
            return

        if connected and pc_reachable:
            color = _COLOR_ONLINE
            status = "Connected — streaming"
            tooltip = "Xbox Bridge: Active"
        elif connected:
            color = _COLOR_WARN
            status = "Controller OK — reconnecting to PC"
            tooltip = "Xbox Bridge: Reconnecting to PC"
        else:
            color = _COLOR_OFFLINE
            status = "No controller"
            tooltip = "Xbox Bridge: No controller"
            # If no controller, start re-check
            status = "Disconnected — retrying"

        try:
            self._icon.icon       = _make_icon(color)
            self._icon.title     = tooltip
            self._icon.menu      = self._build_menu()
        except Exception as exc:
            logger.debug("Tray update error (icon may be stopped): %s", exc)

    def stop(self) -> None:
        self._running = False
        if self._icon:
            try:
                self._icon.stop()
            except Exception as exc:
                logger.debug("Tray stop error: %s", exc)
        if self._thread:
            self._thread.join(timeout=3)