"""System tray manager for the Windows bridge app."""

from __future__ import annotations

import logging
import os
import subprocess
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

VERSION = "1.0.0"

# Status colours
_COLOR_ONLINE  = (40, 210, 90)   # Green  — connected & streaming
_COLOR_WARN    = (230, 165, 0)   # Amber  — waiting / reconnecting
_COLOR_OFFLINE = (210, 55, 55)   # Red    — disconnected


# ---------------------------------------------------------------------------
# Icon generator
# ---------------------------------------------------------------------------

def _make_icon(color: tuple[int, int, int], size: int = 64) -> "Image.Image":
    """Draw a coloured Xbox-style circle icon for the notification area."""
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Dark background disc
    draw.ellipse([0, 0, size - 1, size - 1], fill=(28, 28, 28, 240))

    # Coloured status ring
    rw = max(3, size // 16)
    m  = rw + 1
    draw.ellipse([m, m, size - m - 1, size - m - 1],
                 fill=None, outline=color, width=rw)

    # "X" in the centre
    cx = cy = size // 2
    arm = size // 5
    lw  = max(2, size // 18)
    draw.line([cx - arm, cy - arm, cx + arm, cy + arm], fill=color, width=lw)
    draw.line([cx + arm, cy - arm, cx - arm, cy + arm], fill=color, width=lw)

    return img


# ---------------------------------------------------------------------------
# TrayManager
# ---------------------------------------------------------------------------

class TrayManager:
    """Manages the pystray icon.

    Call ``run_blocking()`` from the **main thread** so that pystray owns
    the Win32 message loop.  All other bridge logic should run on background
    threads before calling ``run_blocking()``.
    """

    def __init__(
        self,
        on_exit:               Callable[[], None],
        on_restart_controller: Callable[[], None] | None = None,
        on_reconnect:          Callable[[], None] | None = None,
    ) -> None:
        if not _TRAY_AVAILABLE:
            logger.warning("pystray / Pillow not available — tray icon disabled")
            self._icon = None
            return

        self._on_exit               = on_exit
        self._on_restart_controller = on_restart_controller
        self._on_reconnect          = on_reconnect

        self._icon: "pystray.Icon | None" = None
        self._running   = False
        self._lock      = threading.Lock()

        self._status_text  = "Starting…"
        self._listen_addr  = ""
        self._install_dir  = ""
        self._log_path: str | None = None
        self._connected    = False
        self._peer_ip      = ""

    # ------------------------------------------------------------------
    # Public setters (thread-safe)
    # ------------------------------------------------------------------

    def set_log_path(self, path: str) -> None:
        self._log_path = path

    def set_listen_addr(self, addr: str) -> None:
        self._listen_addr = addr

    def set_install_dir(self, path: str) -> None:
        self._install_dir = path

    # ------------------------------------------------------------------
    # Main-thread entry point (replaces start() + thread)
    # ------------------------------------------------------------------

    def run_blocking(self) -> None:
        """Create the icon and run its message loop on the calling thread.

        Blocks until ``_handle_exit`` is called (user clicks Exit).
        Must be called from the **main thread** on Windows.
        """
        if not _TRAY_AVAILABLE:
            # No tray available — just spin until stop() is called externally
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
        self._icon.run()           # ← blocks here; released by icon.stop()
        logger.info("Tray icon stopped")

    def start(self) -> None:
        """Compatibility shim — calls run_blocking() in a daemon thread.

        Prefer calling run_blocking() from the main thread instead.
        """
        if not _TRAY_AVAILABLE or self._icon is not None:
            return
        self._running = True
        t = threading.Thread(target=self.run_blocking, name="TrayIcon", daemon=True)
        t.start()
        logger.info("Tray icon started (background thread)")

    # ------------------------------------------------------------------
    # Menu builder
    # ------------------------------------------------------------------

    def _build_menu(self) -> "pystray.Menu":
        # Dynamic status line
        def _status_title(item):  # noqa: ARG001
            if self._connected and self._peer_ip:
                return f"● Connected — {self._peer_ip}"
            elif self._connected:
                return "● Connected"
            else:
                return "○ Waiting for controller…"

        return pystray.Menu(
            # ── Header ──────────────────────────────────────────────
            pystray.MenuItem(f"Xbox Bridge  {VERSION}", None, enabled=False),
            pystray.MenuItem(_status_title,             None, enabled=False),
            pystray.Menu.SEPARATOR,

            # ── Logs & files ─────────────────────────────────────────
            pystray.MenuItem("View Logs",            self._handle_view_logs),
            pystray.MenuItem("Copy Log to Clipboard",self._handle_copy_logs),
            pystray.MenuItem("Open App Folder",      self._handle_open_folder),
            pystray.Menu.SEPARATOR,

            # ── Controller / connection ───────────────────────────────
            pystray.MenuItem("Restart Virtual Controller", self._handle_restart_controller),
            pystray.MenuItem("Reconnect",                  self._handle_reconnect),
            pystray.Menu.SEPARATOR,

            # ── Settings / info ──────────────────────────────────────
            pystray.MenuItem("Settings",             self._handle_settings),
            pystray.MenuItem("About",                self._handle_about),
            pystray.Menu.SEPARATOR,

            # ── Danger zone ──────────────────────────────────────────
            pystray.MenuItem("Uninstall…",           self._handle_uninstall),
            pystray.MenuItem("Exit",                 self._handle_exit),
        )

    # ------------------------------------------------------------------
    # Status update (thread-safe)
    # ------------------------------------------------------------------

    def update(self, *, connected: bool, pc_reachable: bool, peer_ip: str = "") -> None:
        if not _TRAY_AVAILABLE or not self._icon:
            return

        self._connected = connected and pc_reachable
        if peer_ip:
            self._peer_ip = peer_ip

        if connected and pc_reachable:
            color   = _COLOR_ONLINE
            tooltip = f"Xbox Bridge — streaming to {self._peer_ip or 'PC'}"
        elif connected:
            color   = _COLOR_WARN
            tooltip = "Xbox Bridge — controller ready, PC reconnecting…"
        else:
            color   = _COLOR_OFFLINE
            tooltip = "Xbox Bridge — waiting for controller"

        try:
            self._icon.icon  = _make_icon(color)
            self._icon.title = tooltip
            self._icon.menu  = self._build_menu()
        except Exception as exc:
            logger.debug("Tray update error: %s", exc)

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

    def _handle_view_logs(self, _=None) -> None:
        path = self._log_path
        if not path or not os.path.exists(path):
            self._notify("Log file not found.", title="Xbox Bridge")
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except AttributeError:
            subprocess.Popen(["notepad", path])
        except Exception as exc:
            logger.error("Cannot open log file: %s", exc)

    def _handle_copy_logs(self, _=None) -> None:
        path = self._log_path
        if not path or not os.path.exists(path):
            self._notify("Log file not found.", title="Xbox Bridge")
            return
        try:
            import pyperclip  # type: ignore[import]
            with open(path, encoding="utf-8", errors="replace") as f:
                content = f.read()
            pyperclip.copy(content[-65536:])   # last 64 KB
            self._notify("Log copied to clipboard.", title="Xbox Bridge")
        except ImportError:
            # Fallback: use Windows clip command
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    content = f.read()
                proc = subprocess.Popen(
                    ["clip"], stdin=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                proc.communicate(input=content[-65536:].encode("utf-16-le"))
                self._notify("Log copied to clipboard.", title="Xbox Bridge")
            except Exception as exc:
                logger.error("Cannot copy logs: %s", exc)
        except Exception as exc:
            logger.error("Cannot copy logs: %s", exc)

    def _handle_open_folder(self, _=None) -> None:
        folder = self._install_dir or os.path.dirname(self._log_path or "")
        if not folder or not os.path.isdir(folder):
            # Fallback: LOCALAPPDATA\bluetooth_bridge
            folder = os.path.join(
                os.environ.get("LOCALAPPDATA", os.environ.get("USERPROFILE", ".")),
                "bluetooth_bridge"
            )
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error("Cannot open folder: %s", exc)

    def _handle_restart_controller(self, _=None) -> None:
        self._notify("Restarting virtual controller…", title="Xbox Bridge")
        if self._on_restart_controller:
            try:
                self._on_restart_controller()
            except Exception as exc:
                logger.error("Restart controller error: %s", exc)

    def _handle_reconnect(self, _=None) -> None:
        self._notify("Reconnecting…", title="Xbox Bridge")
        if self._on_reconnect:
            try:
                self._on_reconnect()
            except Exception as exc:
                logger.error("Reconnect error: %s", exc)

    def _handle_settings(self, _=None) -> None:
        config_candidates = [
            os.path.join(self._install_dir, "config.ini"),
            os.path.join(
                os.environ.get("USERPROFILE", "."),
                "bluetooth_bridge", "config.ini"
            ),
        ]
        config = next((p for p in config_candidates if os.path.exists(p)), None)
        if not config:
            self._notify("config.ini not found.", title="Xbox Bridge")
            return
        try:
            os.startfile(config)  # type: ignore[attr-defined]
        except Exception as exc:
            subprocess.Popen(["notepad", config])

    def _handle_about(self, _=None) -> None:
        lines = [
            f"Xbox Bridge  v{VERSION}",
            f"Listening on  {self._listen_addr or '0.0.0.0:9999'}",
        ]
        if self._peer_ip:
            lines.append(f"Linux bridge:  {self._peer_ip}")
        if self._log_path:
            lines.append(f"Log:  {self._log_path}")
        self._notify("\n".join(lines), title="About Xbox Bridge")

    def _handle_uninstall(self, _=None) -> None:
        candidates = [
            os.path.join(self._install_dir, "uninstall.ps1"),
            os.path.join(
                os.environ.get("USERPROFILE", "."),
                "bluetooth_bridge", "uninstall.ps1"
            ),
        ]
        script = next((p for p in candidates if os.path.exists(p)), None)
        if not script:
            self._notify("uninstall.ps1 not found.", title="Xbox Bridge")
            return
        try:
            subprocess.Popen(
                ["powershell", "-ExecutionPolicy", "Bypass", "-File", script],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as exc:
            logger.error("Cannot launch uninstaller: %s", exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _notify(self, message: str, *, title: str = "Xbox Bridge") -> None:
        if self._icon:
            try:
                self._icon.notify(message, title)
            except Exception:
                pass