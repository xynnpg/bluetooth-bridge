"""System tray manager for the Windows bridge.

The tray is intentionally minimal — every option lives in the local web
dashboard. The menu only exposes:

    Open Web UI · Reset · Reconnect · Open App Folder · Exit
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

try:
    import pystray
    from PIL import Image, ImageDraw
    _TRAY_AVAILABLE = True
except ImportError:
    _TRAY_AVAILABLE = False
    pystray = None  # type: ignore
    Image = None    # type: ignore

logger = logging.getLogger("tray")

VERSION = "1.1.0"

_COLOR_ONLINE  = (40, 210, 90)
_COLOR_WARN    = (230, 165, 0)
_COLOR_OFFLINE = (210, 55, 55)


def _make_icon(color: tuple[int, int, int], size: int = 64) -> "Image.Image":
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.ellipse([0, 0, size - 1, size - 1], fill=(28, 28, 28, 240))

    rw = max(3, size // 16)
    m  = rw + 1
    draw.ellipse([m, m, size - m - 1, size - m - 1],
                 fill=None, outline=color, width=rw)

    cx = cy = size // 2
    arm = size // 5
    lw  = max(2, size // 18)
    draw.line([cx - arm, cy - arm, cx + arm, cy + arm], fill=color, width=lw)
    draw.line([cx + arm, cy - arm, cx - arm, cy + arm], fill=color, width=lw)

    return img


class TrayManager:
    """Manages the pystray icon.

    Call ``run_blocking()`` from the **main thread** so pystray owns the
    Win32 message loop.
    """

    def __init__(
        self,
        on_exit:               Callable[[], None],
        on_restart_controller: Callable[[], None] | None = None,
        on_reconnect:          Callable[[], None] | None = None,
        on_open_app:           Callable[[], None] | None = None,
    ) -> None:
        if not _TRAY_AVAILABLE:
            logger.warning("pystray / Pillow not available — tray icon disabled")
            self._icon = None
            self._running = False
            return

        self._on_exit               = on_exit
        self._on_restart_controller = on_restart_controller
        self._on_reconnect          = on_reconnect
        self._on_open_app           = on_open_app

        self._icon: "pystray.Icon | None" = None
        self._running = False
        self._lock    = threading.Lock()

        self._listen_addr  = ""
        self._install_dir  = ""
        self._connected    = False
        self._peer_ip      = ""

        self._state = None
        self._battery         = -1
        self._charging        = False
        self._controller_name = ""
        self._controller_mac  = ""
        self._show_battery    = True

    # ------------------------------------------------------------------
    # Public setters (thread-safe)
    # ------------------------------------------------------------------

    def set_listen_addr(self, addr: str) -> None:
        self._listen_addr = addr

    def set_install_dir(self, path: str) -> None:
        self._install_dir = path

    def set_state(self, state) -> None:
        self._state = state

    def set_show_battery(self, enabled: bool) -> None:
        self._show_battery = bool(enabled)

    # ------------------------------------------------------------------
    # Main-thread entry point
    # ------------------------------------------------------------------

    def run_blocking(self) -> None:
        if not _TRAY_AVAILABLE:
            self._running = True
            while self._running:
                time.sleep(0.5)
            return

        self._running = True
        self._icon = pystray.Icon(
            "xbox_bridge",
            _make_icon(_COLOR_WARN),
            f"Xbox Bridge {VERSION}",
            self._build_menu(),
        )
        logger.info("Tray icon starting on main thread")
        self._icon.run()
        logger.info("Tray icon stopped")

    # ------------------------------------------------------------------
    # Menu builder
    # ------------------------------------------------------------------

    def _build_menu(self) -> "pystray.Menu":
        def _status_title(item):  # noqa: ARG001
            if self._connected and self._peer_ip:
                return f"● Connected — {self._peer_ip}"
            if self._connected:
                return "● Connected"
            return "○ Waiting for controller…"

        def _battery_title(item):  # noqa: ARG001
            if not self._show_battery:
                return "Battery display disabled"
            if self._battery is None or self._battery < 0:
                return "Battery:  --"
            glyph = "⚡" if self._charging else "  "
            return f"Battery:  {self._battery}%  {glyph}"

        return pystray.Menu(
            pystray.MenuItem(f"Xbox Bridge  {VERSION}", None, enabled=False),
            pystray.MenuItem(_status_title,  None, enabled=False),
            pystray.MenuItem(_battery_title, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Web UI",      self._handle_open_app, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Reset",            self._handle_restart_controller),
            pystray.MenuItem("Reconnect",        self._handle_reconnect),
            pystray.MenuItem("Open App Folder",  self._handle_open_folder),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit",             self._handle_exit),
        )

    # ------------------------------------------------------------------
    # Status update (thread-safe)
    # ------------------------------------------------------------------

    def update(self, *, connected: bool, pc_reachable: bool, peer_ip: str = "",
               battery: int | None = None, charging: bool | None = None,
               controller_name: str = "", controller_mac: str = "") -> None:
        if not _TRAY_AVAILABLE or not self._icon:
            return

        self._connected = connected and pc_reachable
        if peer_ip:
            self._peer_ip = peer_ip
        if battery is not None:
            self._battery = battery
        if charging is not None:
            self._charging = bool(charging)
        if controller_name:
            self._controller_name = controller_name
        if controller_mac:
            self._controller_mac = controller_mac

        if connected and pc_reachable:
            color   = _COLOR_ONLINE
            tooltip = self._build_tooltip(streaming=True)
        elif connected:
            color   = _COLOR_WARN
            tooltip = self._build_tooltip(streaming=False)
        else:
            color   = _COLOR_OFFLINE
            tooltip = "Xbox Bridge — waiting for controller"

        try:
            self._icon.icon  = _make_icon(color)
            self._icon.title = tooltip
            self._icon.menu  = self._build_menu()
        except Exception as exc:
            logger.debug("Tray update error: %s", exc)

    def _build_tooltip(self, *, streaming: bool) -> str:
        if self._show_battery and self._battery is not None and self._battery >= 0:
            bat = f" · {self._battery}%"
            if self._charging:
                bat += " ⚡"
        else:
            bat = ""
        if streaming:
            return f"Xbox Bridge{bat} — streaming to {self._peer_ip or 'PC'}"
        return f"Xbox Bridge{bat} — controller ready, PC reconnecting…"

    def stop(self) -> None:
        self._running = False
        if self._icon:
            try:
                self._icon.stop()
            except Exception as exc:
                logger.debug("Tray stop error: %s", exc)

    # ------------------------------------------------------------------
    # Menu handlers
    # ------------------------------------------------------------------

    def _handle_exit(self, _=None) -> None:
        logger.info("Exit requested from tray")
        self._running = False
        if self._icon:
            self._icon.stop()
        self._on_exit()

    def _handle_open_app(self, _=None) -> None:
        if self._on_open_app:
            try:
                self._on_open_app()
            except Exception as exc:
                logger.error("Open Web UI error: %s", exc)
        else:
            self._notify("Web UI not available.", title="Xbox Bridge")

    def _handle_open_folder(self, _=None) -> None:
        folder = self._install_dir or "."
        if not os.path.isdir(folder):
            folder = os.path.join(
                os.environ.get("LOCALAPPDATA", os.environ.get("USERPROFILE", ".")),
                "bluetooth_bridge",
            )
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error("Cannot open folder: %s", exc)

    def _handle_restart_controller(self, _=None) -> None:
        self._notify("Resetting virtual controller…", title="Xbox Bridge")
        if self._on_restart_controller:
            try:
                self._on_restart_controller()
            except Exception as exc:
                logger.error("Reset controller error: %s", exc)

    def _handle_reconnect(self, _=None) -> None:
        self._notify("Reconnecting…", title="Xbox Bridge")
        if self._on_reconnect:
            try:
                self._on_reconnect()
            except Exception as exc:
                logger.error("Reconnect error: %s", exc)

    # ------------------------------------------------------------------

    def notify(self, message: str, *, title: str = "Xbox Bridge") -> None:
        self._notify(message, title=title)

    def _notify(self, message: str, *, title: str = "Xbox Bridge") -> None:
        if self._icon:
            try:
                self._icon.notify(message, title)
            except Exception:
                pass
