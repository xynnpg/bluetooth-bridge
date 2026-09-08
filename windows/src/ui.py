"""Custom UI windows — dark-themed log viewer, settings editor, and dashboard.

Each window runs its own Tkinter mainloop in a daemon thread so it
never blocks the tray or the bridge logic.

NOTE: deliberately avoids ttk widgets — ttk uses native Windows themes
that conflict with custom dark-mode backgrounds and cause TclErrors.
"""

from __future__ import annotations

import configparser
import logging
import os
import re
import threading
import time
import traceback
import tkinter as tk
from tkinter import messagebox

logger = logging.getLogger("ui")

# ── Palette ──────────────────────────────────────────────────────────────────
BG       = "#0e0e0e"
SURFACE  = "#1a1a1a"
SURFACE2 = "#232323"
BORDER   = "#2e2e2e"
TEXT     = "#e4e4e4"
MUTED    = "#777777"
ACCENT   = "#00b450"    # Xbox green
ACCENT_H = "#00d45e"
BTN_BG   = "#252525"
BTN_H    = "#303030"

# Log-level colours
_LEVEL_COLOR: dict[str, str] = {
    "DEBUG":    "#5a5a5a",
    "INFO":     "#64b5f6",
    "WARNING":  "#ffb74d",
    "ERROR":    "#ef5350",
    "CRITICAL": "#ff1744",
}
_TIME_COLOR = "#4e9a06"
_NAME_COLOR = "#8b9dc3"
_LEVEL_RE   = re.compile(
    r"^(\d{2}:\d{2}:\d{2})"
    r" \[(\w+)\]"
    r" ([\w.]+):"
    r"(.*)$"
)


# ── Widget helpers ────────────────────────────────────────────────────────────

def _centre(win: tk.Tk, w: int, h: int) -> None:
    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")


def _btn(parent, text: str, cmd, accent: bool = False) -> tk.Button:
    bg = ACCENT if accent else BTN_BG
    fg = "#000000" if accent else TEXT
    return tk.Button(
        parent, text=text, command=cmd,
        bg=bg, fg=fg,
        activebackground=ACCENT_H if accent else BTN_H,
        activeforeground="#000000" if accent else TEXT,
        relief="flat", cursor="hand2",
        padx=14, pady=6, font=("Segoe UI", 9), bd=0,
    )


def _lbl(parent, text: str, size: int = 9, bold: bool = False,
         color: str = TEXT, bg: str = BG) -> tk.Label:
    return tk.Label(parent, text=text, bg=bg, fg=color,
                    font=("Segoe UI", size, "bold" if bold else "normal"))


def _section_bar(parent, title: str) -> None:
    """Render a coloured section header directly into parent."""
    f = tk.Frame(parent, bg=BG)
    f.pack(fill="x", pady=(12, 4))
    tk.Frame(f, bg=ACCENT, width=3, height=16).pack(side="left")
    tk.Label(f, text=f"  {title}", bg=BG, fg=ACCENT,
             font=("Segoe UI", 9, "bold")).pack(side="left")


def _separator(parent) -> None:
    tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", pady=6)


def _option_menu(parent, variable: tk.StringVar,
                 choices: list[str]) -> tk.OptionMenu:
    """Pure-tk dropdown — avoids ttk theming issues on dark backgrounds."""
    m = tk.OptionMenu(parent, variable, *choices)
    m.config(bg=SURFACE, fg=TEXT, relief="flat", bd=0,
             activebackground=BTN_H, activeforeground=TEXT,
             highlightthickness=0, cursor="hand2",
             font=("Segoe UI", 9))
    m["menu"].config(bg=SURFACE2, fg=TEXT, activebackground=ACCENT,
                     activeforeground="#000000", relief="flat", bd=0)
    return m


def _check_row(parent, label: str, var: tk.BooleanVar) -> None:
    """Label + checkbutton on a single row."""
    f = tk.Frame(parent, bg=BG)
    f.pack(fill="x", pady=4)
    tk.Checkbutton(
        f, text=label, variable=var,
        bg=BG, fg=TEXT, selectcolor=SURFACE,
        activebackground=BG, activeforeground=ACCENT,
        font=("Segoe UI", 9), cursor="hand2", anchor="w",
    ).pack(side="left")


def _bar(parent, height: int = 8) -> tuple["tk.Canvas", int]:
    """Return a (canvas, line_y) where you can draw a horizontal progress bar.

    Use ``canvas.coords(line, x0, y, x1, y)`` to update the bar.
    """
    c = tk.Canvas(parent, height=height, bg=SURFACE,
                  highlightthickness=0, bd=0)
    c.pack(fill="x", pady=2)
    y = height // 2
    line = c.create_line(0, y, 0, y, fill=ACCENT, width=height - 2)
    return c, line


def _kv_row(parent, key: str, value: tk.StringVar) -> tk.Label:
    """Label/key + dynamic value (right-aligned), for dashboard cards."""
    f = tk.Frame(parent, bg=SURFACE)
    f.pack(fill="x", pady=3)
    tk.Label(f, text=key, bg=SURFACE, fg=MUTED,
             font=("Segoe UI", 9), anchor="w").pack(side="left")
    val = tk.Label(f, textvariable=value, bg=SURFACE, fg=TEXT,
                   font=("Consolas", 9), anchor="e")
    val.pack(side="right")
    return val


def _format_uptime(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _format_battery(b: int, charging: bool) -> str:
    if b is None or b < 0:
        return "Unknown"
    glyph = " ⚡" if charging else ""
    return f"{b}%{glyph}"


# ── Log Viewer ────────────────────────────────────────────────────────────────

_OPEN_LOGVIEWER: list[tk.Tk] = []


def open_log_viewer(log_path: str) -> None:
    """Open (or raise) the log viewer in a daemon thread."""
    for w in list(_OPEN_LOGVIEWER):
        try:
            w.lift(); w.focus_force(); return
        except (tk.TclError, RuntimeError):
            _OPEN_LOGVIEWER.remove(w)
    threading.Thread(target=_run_log_viewer, args=(log_path,), daemon=True).start()


def _run_log_viewer(log_path: str) -> None:
    try:
        _LogViewerWindow(log_path).run()
    except Exception:
        logger.error("Log viewer crashed:\n%s", traceback.format_exc())


class _LogViewerWindow:
    def __init__(self, path: str) -> None:
        self._path     = path
        self._root     = tk.Tk()
        self._auto_var = tk.BooleanVar(value=True)
        self._filter   = tk.StringVar(value="ALL")
        self._search   = tk.StringVar()
        self._after_id = None

    def run(self) -> None:
        r = self._root
        _OPEN_LOGVIEWER.append(r)
        r.title("Xbox Bridge — Logs")
        r.configure(bg=BG)
        _centre(r, 980, 660)
        r.protocol("WM_DELETE_WINDOW", self._close)
        self._build()
        self._load()
        self._schedule()
        r.mainloop()

    def _build(self) -> None:
        r = self._root

        # ── header ────────────────────────────────────────────────────
        hdr = tk.Frame(r, bg=SURFACE, height=44)
        hdr.pack(fill="x"); hdr.pack_propagate(False)
        _lbl(hdr, "  📋  Log Viewer", 11, True, bg=SURFACE).pack(side="left", pady=8)
        _lbl(hdr, self._path, 8, color=MUTED, bg=SURFACE).pack(side="left", padx=8)

        # ── toolbar ───────────────────────────────────────────────────
        bar = tk.Frame(r, bg=SURFACE2, height=40)
        bar.pack(fill="x"); bar.pack_propagate(False)

        tk.Checkbutton(
            bar, text="Auto-scroll", variable=self._auto_var,
            bg=SURFACE2, fg=TEXT, selectcolor=SURFACE,
            activebackground=SURFACE2, activeforeground=ACCENT,
            font=("Segoe UI", 9), cursor="hand2",
        ).pack(side="left", padx=10, pady=8)

        _lbl(bar, "Level:", bg=SURFACE2, color=MUTED).pack(side="left")
        m = _option_menu(bar, self._filter,
                         ["ALL","DEBUG","INFO","WARNING","ERROR","CRITICAL"])
        m.pack(side="left", padx=(2, 8), pady=6)
        self._filter.trace_add("write", lambda *_: self._load())

        _lbl(bar, "Module:", bg=SURFACE2, color=MUTED).pack(side="left")
        self._module_var = tk.StringVar(value="ALL")
        mm = _option_menu(bar, self._module_var,
                          ["ALL", "main", "receiver", "emitter", "controller",
                           "rumble", "tray", "ui", "state", "network",
                           "discovery", "bluetooth"])
        mm.pack(side="left", padx=(2, 8), pady=6)
        self._module_var.trace_add("write", lambda *_: self._load())

        # Highlight toggles for new v2 events
        self._highlight_battery_var = tk.BooleanVar(value=False)
        self._highlight_rumble_var  = tk.BooleanVar(value=False)
        tk.Checkbutton(
            bar, text="🔋 Battery", variable=self._highlight_battery_var,
            bg=SURFACE2, fg=TEXT, selectcolor=SURFACE,
            activebackground=SURFACE2, activeforeground=ACCENT,
            font=("Segoe UI", 9), cursor="hand2",
        ).pack(side="left", padx=4, pady=8)
        tk.Checkbutton(
            bar, text="📳 Rumble", variable=self._highlight_rumble_var,
            bg=SURFACE2, fg=TEXT, selectcolor=SURFACE,
            activebackground=SURFACE2, activeforeground=ACCENT,
            font=("Segoe UI", 9), cursor="hand2",
        ).pack(side="left", padx=4, pady=8)
        self._highlight_battery_var.trace_add("write", lambda *_: self._load())
        self._highlight_rumble_var.trace_add("write",  lambda *_: self._load())

        _lbl(bar, "Search:", bg=SURFACE2, color=MUTED).pack(side="left")
        se = tk.Entry(bar, textvariable=self._search,
                      bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                      relief="flat", font=("Segoe UI", 9), width=18,
                      highlightthickness=1, highlightcolor=ACCENT,
                      highlightbackground=BORDER)
        se.pack(side="left", padx=(2, 8), ipady=4)
        se.bind("<KeyRelease>", lambda _: self._load())

        _btn(bar, "⟳  Refresh",      self._load).pack(side="left", pady=6, padx=2)
        _btn(bar, "Copy Filtered",   self._copy).pack(side="left", pady=6, padx=2)

        # ── text area ─────────────────────────────────────────────────
        tf = tk.Frame(r, bg=BG)
        tf.pack(fill="both", expand=True)

        self._txt = tk.Text(
            tf, wrap="none",
            bg="#090909", fg=TEXT,
            font=("Consolas", 9), relief="flat", bd=0,
            selectbackground=ACCENT, selectforeground="#000",
            insertbackground=TEXT, state="disabled",
        )
        sy = tk.Scrollbar(tf, orient="vertical",   command=self._txt.yview,
                          bg=SURFACE, troughcolor=BG, width=12)
        sx = tk.Scrollbar(tf, orient="horizontal",  command=self._txt.xview,
                          bg=SURFACE, troughcolor=BG, width=12)
        self._txt.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side="right",  fill="y")
        sx.pack(side="bottom", fill="x")
        self._txt.pack(fill="both", expand=True)

        # tags
        self._txt.tag_configure("time", foreground=_TIME_COLOR)
        self._txt.tag_configure("name", foreground=_NAME_COLOR)
        self._txt.tag_configure("msg",  foreground=TEXT)
        self._txt.tag_configure("hl",   background="#4a3600", foreground="#ffd740")
        self._txt.tag_configure("battery", background="#1b3a1b", foreground="#a5d6a7")
        self._txt.tag_configure("rumble",  background="#1a2a4d", foreground="#90caf9")
        for lvl, clr in _LEVEL_COLOR.items():
            self._txt.tag_configure(lvl, foreground=clr,
                                    font=("Consolas", 9, "bold"))

        # ── footer ────────────────────────────────────────────────────
        ft = tk.Frame(r, bg=SURFACE, height=34)
        ft.pack(fill="x", side="bottom"); ft.pack_propagate(False)
        self._status = _lbl(ft, "", color=MUTED, bg=SURFACE)
        self._status.pack(side="left", padx=10, pady=6)
        _btn(ft, "Close",     self._close).pack(side="right", padx=8,  pady=5)
        _btn(ft, "Clear Log", self._clear).pack(side="right", padx=4,  pady=5)

    def _load(self) -> None:
        if not os.path.exists(self._path):
            self._write("[Log file not found]\n"); return
        with open(self._path, encoding="utf-8", errors="replace") as f:
            raw = f.readlines()

        level_f  = self._filter.get()
        module_f = getattr(self, "_module_var", tk.StringVar(value="ALL")).get()
        search   = self._search.get().lower()
        want_bat = self._highlight_battery_var.get()
        want_rum = self._highlight_rumble_var.get()
        lines    = []
        for ln in raw:
            s = ln.rstrip("\n")
            if level_f != "ALL" and f"[{level_f}]" not in s:
                continue
            if module_f != "ALL" and f" {module_f}:" not in s and not s.endswith(f" {module_f}:"):
                continue
            if search and search not in s.lower():
                continue
            if want_bat and "battery" not in s.lower() and "Battery" not in s:
                continue
            if want_rum and "rumble" not in s.lower() and "Rumble" not in s:
                continue
            lines.append(s)

        self._txt.configure(state="normal")
        self._txt.delete("1.0", "end")
        for ln in lines:
            m = _LEVEL_RE.match(ln)
            if m:
                ts, lvl, nm, msg = m.groups()
                # Battery / rumble highlight — yellow background
                tags = []
                if "battery" in ln.lower():
                    tags.append("battery")
                if "rumble" in ln.lower():
                    tags.append("rumble")
                tag_ts = ("battery" if "battery" in ln.lower() else
                          "rumble"  if "rumble"  in ln.lower() else "time")
                self._txt.insert("end", ts,           tag_ts)
                self._txt.insert("end", f" [{lvl}]",  lvl if lvl in _LEVEL_COLOR else "msg")
                self._txt.insert("end", f" {nm}:",    "name")
                self._txt.insert("end", msg + "\n",   "msg")
                for t in tags:
                    # Apply background to the whole line range we just inserted
                    self._txt.tag_add(t, f"end-1l linestart", f"end-1l lineend")
            else:
                self._txt.insert("end", ln + "\n", "msg")
        if search:
            idx = "1.0"
            while True:
                pos = self._txt.search(search, idx, stopindex="end", nocase=True)
                if not pos: break
                end = f"{pos}+{len(search)}c"
                self._txt.tag_add("hl", pos, end)
                idx = end
        self._txt.configure(state="disabled")
        if self._auto_var.get():
            self._txt.see("end")
        try:
            kb = os.path.getsize(self._path) / 1024
        except OSError:
            kb = 0
        self._status.configure(text=f"{len(lines)} lines  ·  {kb:.1f} KB")

    def _write(self, text: str) -> None:
        self._txt.configure(state="normal")
        self._txt.delete("1.0", "end")
        self._txt.insert("end", text)
        self._txt.configure(state="disabled")

    def _copy(self) -> None:
        self._txt.configure(state="normal")
        content = self._txt.get("1.0", "end")
        self._txt.configure(state="disabled")
        self._root.clipboard_clear()
        self._root.clipboard_append(content)

    def _clear(self) -> None:
        if not messagebox.askyesno("Clear Log",
                                   "Delete the log file contents?",
                                   parent=self._root):
            return
        try:
            open(self._path, "w").close()
            self._load()
        except Exception as exc:
            messagebox.showerror("Error", str(exc), parent=self._root)

    def _schedule(self) -> None:
        if self._auto_var.get():
            self._load()
        try:
            self._after_id = self._root.after(2000, self._schedule)
        except tk.TclError:
            pass

    def _close(self) -> None:
        try:
            if self._after_id:
                self._root.after_cancel(self._after_id)
        except Exception:
            pass
        try:
            _OPEN_LOGVIEWER.remove(self._root)
        except ValueError:
            pass
        try:
            self._root.destroy()
        except Exception:
            pass


# ── Settings Window ───────────────────────────────────────────────────────────

_OPEN_SETTINGS: list[tk.Tk] = []


def open_settings(config_path: str, on_log_level_change=None,
                  on_settings_change=None) -> None:
    """Open (or raise) the settings window.

    `on_settings_change(dict)` is called after a successful save with the
    raw values (port, host, log_level, rumble_enabled, low_battery_warn_pct,
    show_battery_in_tray, open_app_on_launch, auto_discover) so the
    running app can pick up changes without a restart.
    """
    for w in list(_OPEN_SETTINGS):
        try:
            w.lift(); w.focus_force(); return
        except (tk.TclError, RuntimeError):
            _OPEN_SETTINGS.remove(w)
    threading.Thread(
        target=_run_settings,
        args=(config_path, on_log_level_change, on_settings_change),
        daemon=True,
    ).start()


def _run_settings(config_path: str, on_log_level_change, on_settings_change) -> None:
    try:
        _SettingsWindow(config_path, on_log_level_change, on_settings_change).run()
    except Exception:
        logger.error("Settings window crashed:\n%s", traceback.format_exc())


class _SettingsWindow:
    def __init__(self, config_path: str, on_log_level_change, on_settings_change) -> None:
        self._path   = config_path
        self._on_lvl = on_log_level_change
        self._on_chg = on_settings_change
        self._root   = tk.Tk()
        self._cfg    = configparser.ConfigParser()

    def run(self) -> None:
        r = self._root
        _OPEN_SETTINGS.append(r)
        r.title("Xbox Bridge — Settings")
        r.configure(bg=BG)
        r.resizable(False, False)
        _centre(r, 480, 640)
        r.protocol("WM_DELETE_WINDOW", self._close)
        if os.path.exists(self._path):
            # utf-8-sig strips the BOM (\ufeff) that Windows Notepad/PowerShell
            # adds when saving UTF-8 files
            try:
                self._cfg.read(self._path, encoding="utf-8-sig")
            except (OSError, configparser.Error) as exc:
                logger.warning("Could not read %s — using defaults: %s",
                               self._path, exc)
        self._build()
        r.mainloop()

    def _build(self) -> None:
        r = self._root

        # ── header ────────────────────────────────────────────────────
        hdr = tk.Frame(r, bg=SURFACE, height=48)
        hdr.pack(fill="x"); hdr.pack_propagate(False)
        _lbl(hdr, "  ⚙   Settings", 12, True, bg=SURFACE).pack(side="left", pady=12)

        body = tk.Frame(r, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=12)

        # ── Network ───────────────────────────────────────────────────
        _section_bar(body, "Network")

        port_val = self._cfg.get("app", "listen_port", fallback="9999")
        self._port_var = tk.StringVar(value=port_val)
        self._entry_row(body, "Listen Port",
                        tk.Entry(body, textvariable=self._port_var,
                                 bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                                 relief="flat", font=("Segoe UI", 10), width=12,
                                 highlightthickness=1, highlightcolor=ACCENT,
                                 highlightbackground=BORDER))

        host_val = self._cfg.get("app", "listen_host",
                                 fallback=os.environ.get("LISTEN_HOST", "0.0.0.0"))
        self._host_var = tk.StringVar(value=host_val)
        self._entry_row(body, "Listen Host",
                        tk.Entry(body, textvariable=self._host_var,
                                 bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                                 relief="flat", font=("Segoe UI", 10), width=16,
                                 highlightthickness=1, highlightcolor=ACCENT,
                                 highlightbackground=BORDER))

        self._discover_var = tk.BooleanVar(
            value=self._cfg.getboolean("app", "auto_discover", fallback=True)
        )
        _check_row(body, "Broadcast IP for Linux auto-discovery (UDP 9876)",
                   self._discover_var)

        # ── Controller ────────────────────────────────────────────────
        _section_bar(body, "Controller")

        self._rumble_var = tk.BooleanVar(
            value=self._cfg.getboolean("controller", "rumble_enabled", fallback=True)
        )
        _check_row(body, "Send rumble to the controller", self._rumble_var)

        try:
            warn_default = int(self._cfg.get("controller", "low_battery_warn_pct",
                                             fallback="15"))
        except ValueError:
            warn_default = 15
        self._warn_var = tk.StringVar(value=str(warn_default))
        spin = tk.Spinbox(
            body, textvariable=self._warn_var, from_=0, to=100, increment=1, width=5,
            bg=SURFACE, fg=TEXT, buttonbackground=SURFACE2,
            relief="flat", highlightthickness=1,
            highlightcolor=ACCENT, highlightbackground=BORDER,
        )
        self._entry_row(body, "Low-battery warn at (%)", spin)

        # ── Tray ──────────────────────────────────────────────────────
        _section_bar(body, "Tray")

        self._battery_tooltip_var = tk.BooleanVar(
            value=self._cfg.getboolean("tray", "show_battery", fallback=True)
        )
        _check_row(body, "Show battery in tray tooltip",
                   self._battery_tooltip_var)

        self._auto_start_var = tk.BooleanVar(
            value=self._cfg.getboolean("app", "auto_start", fallback=True)
        )
        _check_row(body, "Start with Windows (auto-start shortcut)",
                   self._auto_start_var)

        self._open_app_var = tk.BooleanVar(
            value=self._cfg.getboolean("tray", "open_app_on_launch", fallback=False)
        )
        _check_row(body, "Open the dashboard on launch", self._open_app_var)

        # ── Logging ───────────────────────────────────────────────────
        _section_bar(body, "Logging")

        raw_level   = logging.getLogger().level
        level_name  = logging.getLevelName(raw_level)
        if level_name not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            level_name = "INFO"
        self._level_var = tk.StringVar(value=level_name)
        om = _option_menu(body, self._level_var,
                          ["DEBUG", "INFO", "WARNING", "ERROR"])
        self._entry_row(body, "Log Level", om)

        # ── Note ──────────────────────────────────────────────────────
        _separator(body)
        note = tk.Frame(body, bg=SURFACE2, padx=12, pady=10)
        note.pack(fill="x")
        _lbl(note, "⚠  Port / host changes take effect after restart.",
             color="#ffb74d", bg=SURFACE2).pack(anchor="w")
        _lbl(note, "Log level, rumble, and tray changes apply immediately.",
             color=MUTED, bg=SURFACE2).pack(anchor="w", pady=(4, 0))

        # ── Path note ─────────────────────────────────────────────────
        _lbl(body, f"Config: {self._path}", 7, color=MUTED).pack(anchor="w", pady=(10, 0))

        # ── Footer ────────────────────────────────────────────────────
        ft = tk.Frame(r, bg=SURFACE, height=48)
        ft.pack(fill="x", side="bottom"); ft.pack_propagate(False)
        _btn(ft, "Cancel", self._close).pack(side="right", padx=8,  pady=10)
        _btn(ft, "Save",   self._save, accent=True).pack(side="right", padx=4, pady=10)

    def _entry_row(self, parent, label: str, widget: tk.Widget) -> None:
        f = tk.Frame(parent, bg=BG)
        f.pack(fill="x", pady=5)
        # 'width' is a widget option, NOT a pack option — must go in the Label constructor
        tk.Label(f, text=label, bg=BG, fg=MUTED,
                 font=("Segoe UI", 9), width=22, anchor="w").pack(side="left")
        widget.pack(side="left", ipady=4)

    def _save(self) -> None:
        try:
            port = int(self._port_var.get())
            if not 1024 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Port",
                                 "Port must be a whole number between 1024 and 65535.",
                                 parent=self._root)
            return

        try:
            warn = int(self._warn_var.get())
            if not 0 <= warn <= 100:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid value",
                                 "Low-battery threshold must be 0–100.",
                                 parent=self._root)
            return

        for section in ("app", "controller", "tray"):
            if not self._cfg.has_section(section):
                self._cfg.add_section(section)

        self._cfg.set("app",        "listen_port", str(port))
        self._cfg.set("app",        "listen_host", self._host_var.get().strip())
        self._cfg.set("app",        "auto_discover",
                      "true" if self._discover_var.get() else "false")
        self._cfg.set("app",        "auto_start",
                      "true" if self._auto_start_var.get() else "false")

        self._cfg.set("controller", "rumble_enabled",
                      "true" if self._rumble_var.get() else "false")
        self._cfg.set("controller", "low_battery_warn_pct", str(warn))

        self._cfg.set("tray",       "show_battery",
                      "true" if self._battery_tooltip_var.get() else "false")
        self._cfg.set("tray",       "open_app_on_launch",
                      "true" if self._open_app_var.get() else "false")

        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                self._cfg.write(f)
        except Exception as exc:
            messagebox.showerror("Save Error", str(exc), parent=self._root)
            return

        # Apply log level immediately
        new_level = self._level_var.get()
        logging.getLogger().setLevel(getattr(logging, new_level, logging.INFO))
        if self._on_lvl:
            try:
                self._on_lvl(new_level)
            except Exception:
                pass

        if self._on_chg:
            try:
                self._on_chg({
                    "listen_port":           port,
                    "listen_host":           self._host_var.get().strip(),
                    "auto_discover":         self._discover_var.get(),
                    "auto_start":            self._auto_start_var.get(),
                    "rumble_enabled":        self._rumble_var.get(),
                    "low_battery_warn_pct":  warn,
                    "show_battery":          self._battery_tooltip_var.get(),
                    "open_app_on_launch":    self._open_app_var.get(),
                    "log_level":             new_level,
                })
            except Exception:
                pass

        messagebox.showinfo(
            "Saved",
            "Settings saved.\n\nRestart Xbox Bridge for port/host changes to take effect.",
            parent=self._root,
        )
        self._close()

    def _close(self) -> None:
        try:
            _OPEN_SETTINGS.remove(self._root)
        except ValueError:
            pass
        try:
            self._root.destroy()
        except Exception:
            pass


# ── Open App / Dashboard ──────────────────────────────────────────────────────

_OPEN_DASHBOARDS: list[tk.Tk] = []


def open_dashboard(state, *, log_path: str, install_dir: str,
                   config_path: str, listen_addr: str) -> None:
    """Open (or raise) the 'Open App' dashboard window."""
    for w in list(_OPEN_DASHBOARDS):
        try:
            w.lift(); w.focus_force(); return
        except (tk.TclError, RuntimeError):
            _OPEN_DASHBOARDS.remove(w)
    threading.Thread(
        target=_run_dashboard,
        args=(state, log_path, install_dir, config_path, listen_addr),
        daemon=True,
    ).start()


def _run_dashboard(state, log_path, install_dir, config_path, listen_addr) -> None:
    try:
        _DashboardWindow(state, log_path, install_dir, config_path, listen_addr).run()
    except Exception:
        logger.error("Dashboard crashed:\n%s", traceback.format_exc())


class _DashboardWindow:
    def __init__(self, state, log_path: str, install_dir: str,
                 config_path: str, listen_addr: str) -> None:
        self._state        = state
        self._log_path     = log_path
        self._install_dir  = install_dir
        self._config_path  = config_path
        self._listen_addr  = listen_addr

        self._root = tk.Tk()

        # Dynamic state
        self._status_var   = tk.StringVar(value="○ Waiting for controller…")
        self._peer_var     = tk.StringVar(value="(none)")
        self._name_var     = tk.StringVar(value="(unknown)")
        self._mac_var      = tk.StringVar(value="(unknown)")
        self._battery_var  = tk.StringVar(value="Unknown")
        self._uptime_var   = tk.StringVar(value="00:00")
        self._packets_var  = tk.StringVar(value="0")
        self._rumble_l_var = tk.StringVar(value="L 0")
        self._rumble_r_var = tk.StringVar(value="R 0")
        self._listen_var   = tk.StringVar(value=listen_addr or "0.0.0.0:9999")
        self._last_event   = tk.StringVar(value="—")

        self._after_id = None

    def run(self) -> None:
        r = self._root
        _OPEN_DASHBOARDS.append(r)
        r.title("Xbox Bridge — Open App")
        r.configure(bg=BG)
        r.minsize(640, 480)
        _centre(r, 720, 540)
        r.protocol("WM_DELETE_WINDOW", self._close)
        self._build()
        self._refresh()
        r.mainloop()

    def _build(self) -> None:
        r = self._root

        # ── header ────────────────────────────────────────────────────
        hdr = tk.Frame(r, bg=SURFACE, height=56)
        hdr.pack(fill="x"); hdr.pack_propagate(False)
        tk.Frame(hdr, bg=ACCENT, width=4).pack(side="left", fill="y")
        _lbl(hdr, "  Xbox Bridge", 13, True, bg=SURFACE).pack(side="left", pady=12)
        self._status_pill = tk.Label(
            hdr, textvariable=self._status_var, bg=SURFACE, fg=ACCENT,
            font=("Segoe UI", 9, "bold"),
        )
        self._status_pill.pack(side="right", padx=14)

        # ── body: two cards side-by-side, then events, then buttons ──
        body = tk.Frame(r, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=12)

        # Two-column grid for the cards
        body.columnconfigure(0, weight=1, uniform="cards")
        body.columnconfigure(1, weight=1, uniform="cards")

        # ── Controller card ──────────────────────────────────────────
        ctrl = tk.Frame(body, bg=SURFACE, padx=14, pady=12)
        ctrl.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        _lbl(ctrl, "Controller", 10, True, color=ACCENT, bg=SURFACE).pack(anchor="w")
        tk.Frame(ctrl, bg=BORDER, height=1).pack(fill="x", pady=(2, 8))
        _kv_row(ctrl, "Name",      self._name_var)
        _kv_row(ctrl, "MAC",       self._mac_var)

        # Battery line + bar
        bat_f = tk.Frame(ctrl, bg=SURFACE)
        bat_f.pack(fill="x", pady=(6, 2))
        tk.Label(bat_f, text="Battery", bg=SURFACE, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left")
        self._bat_lbl = tk.Label(bat_f, textvariable=self._battery_var,
                                 bg=SURFACE, fg=TEXT, font=("Consolas", 9))
        self._bat_lbl.pack(side="right")
        self._bat_bar, self._bat_line = _bar(ctrl, height=8)
        self._bat_bar.bind("<Configure>",
                           lambda e: self._redraw_battery(self._last_battery))

        # Rumble line + bar (left motor shown; right below)
        rbl_f = tk.Frame(ctrl, bg=SURFACE)
        rbl_f.pack(fill="x", pady=(8, 2))
        tk.Label(rbl_f, text="Rumble L", bg=SURFACE, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left")
        tk.Label(rbl_f, textvariable=self._rumble_l_var, bg=SURFACE, fg=TEXT,
                 font=("Consolas", 9)).pack(side="right")
        self._rumble_l_bar, self._rumble_l_line = _bar(ctrl, height=6)
        self._rumble_l_bar.bind("<Configure>",
                                lambda e: self._redraw_rumble(self._last_rumble_l,
                                                             "left"))

        rbr_f = tk.Frame(ctrl, bg=SURFACE)
        rbr_f.pack(fill="x", pady=(8, 2))
        tk.Label(rbr_f, text="Rumble R", bg=SURFACE, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left")
        tk.Label(rbr_f, textvariable=self._rumble_r_var, bg=SURFACE, fg=TEXT,
                 font=("Consolas", 9)).pack(side="right")
        self._rumble_r_bar, self._rumble_r_line = _bar(ctrl, height=6)
        self._rumble_r_bar.bind("<Configure>",
                                lambda e: self._redraw_rumble(self._last_rumble_r,
                                                             "right"))

        # ── Network card ─────────────────────────────────────────────
        net = tk.Frame(body, bg=SURFACE, padx=14, pady=12)
        net.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        _lbl(net, "Network", 10, True, color=ACCENT, bg=SURFACE).pack(anchor="w")
        tk.Frame(net, bg=BORDER, height=1).pack(fill="x", pady=(2, 8))
        _kv_row(net, "Listen",     self._listen_var)
        _kv_row(net, "Peer IP",    self._peer_var)
        _kv_row(net, "Uptime",     self._uptime_var)
        _kv_row(net, "Packets",    self._packets_var)
        _kv_row(net, "Last event", self._last_event)

        # ── Recent events panel ──────────────────────────────────────
        ev = tk.Frame(body, bg=SURFACE, padx=14, pady=10)
        ev.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        body.rowconfigure(1, weight=1)
        _lbl(ev, "Recent Events", 10, True, color=ACCENT, bg=SURFACE).pack(anchor="w")
        tk.Frame(ev, bg=BORDER, height=1).pack(fill="x", pady=(2, 6))

        self._events = tk.Text(
            ev, wrap="none",
            bg="#090909", fg=TEXT,
            font=("Consolas", 9), relief="flat", bd=0,
            selectbackground=ACCENT, selectforeground="#000",
            height=10, state="disabled",
        )
        sy = tk.Scrollbar(ev, orient="vertical", command=self._events.yview,
                          bg=SURFACE, troughcolor=BG, width=10)
        self._events.configure(yscrollcommand=sy.set)
        sy.pack(side="right", fill="y")
        self._events.pack(side="left", fill="both", expand=True)
        self._events.tag_configure("ts",  foreground=_TIME_COLOR)
        self._events.tag_configure("lvl", foreground="#64b5f6",
                                   font=("Consolas", 9, "bold"))
        self._events.tag_configure("name", foreground=_NAME_COLOR)
        self._events.tag_configure("msg", foreground=TEXT)

        # ── Footer with action buttons ───────────────────────────────
        ft = tk.Frame(r, bg=SURFACE, height=48)
        ft.pack(fill="x", side="bottom"); ft.pack_propagate(False)
        _btn(ft, "Open Logs",    self._open_logs).pack(side="left",  padx=8,  pady=10)
        _btn(ft, "Open Settings",self._open_settings).pack(side="left", padx=4, pady=10)
        _btn(ft, "Open App Folder", self._open_folder).pack(side="left", padx=4, pady=10)
        _btn(ft, "Close",        self._close).pack(side="right", padx=8,  pady=10)

    # ------------------------------------------------------------------
    # Refresh loop
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        try:
            snap = self._state.snapshot()
        except Exception as exc:
            logger.debug("Snapshot error: %s", exc)
            return

        # Status pill
        if snap["connected"]:
            self._status_var.set(f"● Connected — {snap['peer_ip'] or 'PC'}")
            self._status_pill.configure(fg=ACCENT)
        else:
            self._status_var.set("○ Waiting for controller…")
            self._status_pill.configure(fg="#ffb74d")

        # Controller identity
        self._name_var.set(snap["controller_name"] or "(unknown)")
        self._mac_var.set(snap["controller_mac"]  or "(unknown)")

        # Battery + bar
        b = snap["battery"]
        self._last_battery = b
        self._battery_var.set(_format_battery(b, snap["charging"]))
        self._redraw_battery(b, snap["charging"])

        # Rumble
        self._last_rumble_l = snap["rumble_left"]
        self._last_rumble_r = snap["rumble_right"]
        self._rumble_l_var.set(f"L {snap['rumble_left']}")
        self._rumble_r_var.set(f"R {snap['rumble_right']}")
        self._redraw_rumble(snap["rumble_left"],  "left")
        self._redraw_rumble(snap["rumble_right"], "right")

        # Network / counters
        self._peer_var.set(snap["peer_ip"] or "(none)")
        self._uptime_var.set(_format_uptime(snap["uptime_s"]))
        self._packets_var.set(f"{snap['packets_sent']:,}")
        self._last_event.set(snap["last_event_ts"] or "—")

        # Events list — replace contents (cheap; 100 lines max)
        self._events.configure(state="normal")
        self._events.delete("1.0", "end")
        for line in snap["recent_events"]:
            # Format: "HH:MM:SS  LEVEL  name: message"
            m = re.match(r"^(\d{2}:\d{2}:\d{2})\s+(\S+)\s+([\w.]+):(.*)$", line)
            if m:
                ts, lvl, nm, msg = m.groups()
                self._events.insert("end", ts, "ts")
                self._events.insert("end", f"  {lvl:<7}", "lvl")
                self._events.insert("end", f"  {nm}:", "name")
                self._events.insert("end", msg + "\n", "msg")
            else:
                self._events.insert("end", line + "\n", "msg")
        self._events.see("end")
        self._events.configure(state="disabled")

        try:
            self._after_id = self._root.after(750, self._refresh)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Bar renderers
    # ------------------------------------------------------------------

    def _redraw_battery(self, b: int, charging: bool | None = None) -> None:
        if charging is None:
            charging = bool(self._state.snapshot().get("charging", False))
        try:
            w = self._bat_bar.winfo_width()
        except tk.TclError:
            return
        if w < 2:
            return
        if b is None or b < 0:
            self._bat_bar.coords(self._bat_line, 0, 4, 0, 4)
            self._bat_bar.itemconfigure(self._bat_line, fill=SURFACE2)
            return
        pct = max(0, min(100, b)) / 100.0
        x1 = int(w * pct)
        if b >= 50:    color = ACCENT
        elif b >= 20:  color = "#ffb74d"
        else:          color = "#ef5350"
        self._bat_bar.coords(self._bat_line, 0, 4, x1, 4)
        self._bat_bar.itemconfigure(self._bat_line, fill=color)

    def _redraw_rumble(self, value: int, which: str) -> None:
        try:
            if which == "left":
                w = self._rumble_l_bar.winfo_width()
                line = self._rumble_l_line
            else:
                w = self._rumble_r_bar.winfo_width()
                line = self._rumble_r_line
        except tk.TclError:
            return
        if w < 2:
            return
        pct = max(0, min(255, int(value))) / 255.0
        x1 = int(w * pct)
        self._bat_bar.coords(line, 0, 4, x1, 4) if False else None
        if which == "left":
            self._rumble_l_bar.coords(self._rumble_l_line, 0, 3, x1, 3)
            self._rumble_l_bar.itemconfigure(self._rumble_l_line, fill="#64b5f6")
        else:
            self._rumble_r_bar.coords(self._rumble_r_line, 0, 3, x1, 3)
            self._rumble_r_bar.itemconfigure(self._rumble_r_line, fill="#ba68c8")

    # ------------------------------------------------------------------
    # Footer actions
    # ------------------------------------------------------------------

    def _open_logs(self) -> None:
        open_log_viewer(self._log_path)

    def _open_settings(self) -> None:
        open_settings(self._config_path)

    def _open_folder(self) -> None:
        folder = self._install_dir or os.path.dirname(self._log_path or "")
        if not folder:
            folder = os.path.join(
                os.environ.get("LOCALAPPDATA", os.environ.get("USERPROFILE", ".")),
                "bluetooth_bridge",
            )
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _close(self) -> None:
        try:
            if self._after_id:
                self._root.after_cancel(self._after_id)
        except Exception:
            pass
        try:
            _OPEN_DASHBOARDS.remove(self._root)
        except ValueError:
            pass
        try:
            self._root.destroy()
        except Exception:
            pass
