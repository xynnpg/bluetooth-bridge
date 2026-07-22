"""Custom UI windows — dark-themed log viewer and settings editor.

Each window runs its own Tkinter mainloop in a daemon thread so it
never blocks the tray or the bridge logic.
"""

from __future__ import annotations

import configparser
import logging
import os
import re
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

logger = logging.getLogger("ui")

# ── Palette ──────────────────────────────────────────────────────────────────
BG       = "#0e0e0e"
SURFACE  = "#1a1a1a"
SURFACE2 = "#232323"
BORDER   = "#2e2e2e"
TEXT     = "#e4e4e4"
MUTED    = "#777777"
ACCENT   = "#00b450"       # Xbox green
ACCENT_H = "#00d45e"       # hover
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
_TIME_COLOR  = "#4e9a06"   # dim green for timestamps
_NAME_COLOR  = "#8b9dc3"   # module name
_LEVEL_RE    = re.compile(
    r"^(\d{2}:\d{2}:\d{2})"       # group 1: time
    r" \[(\w+)\]"                  # group 2: level
    r" ([\w.]+):"                  # group 3: logger name
    r"(.*)$"                       # group 4: message
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _centre(win: tk.Tk | tk.Toplevel, w: int, h: int) -> None:
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")


def _styled_button(parent, text: str, command, accent: bool = False) -> tk.Button:
    bg = ACCENT if accent else BTN_BG
    fg = "#000000" if accent else TEXT
    btn = tk.Button(
        parent, text=text, command=command,
        bg=bg, fg=fg, activebackground=ACCENT_H if accent else BTN_H,
        activeforeground="#000000" if accent else TEXT,
        relief="flat", cursor="hand2",
        padx=14, pady=6, font=("Segoe UI", 9),
        bd=0,
    )
    return btn


def _label(parent, text: str, size: int = 9, bold: bool = False,
           color: str = TEXT) -> tk.Label:
    weight = "bold" if bold else "normal"
    return tk.Label(parent, text=text, bg=BG,
                    fg=color, font=("Segoe UI", size, weight))


def _section_header(parent, text: str) -> tk.Frame:
    """A green left-border section label."""
    frame = tk.Frame(parent, bg=BG)
    tk.Frame(frame, bg=ACCENT, width=3).pack(side="left", fill="y")
    tk.Label(frame, text=f"  {text}", bg=BG, fg=ACCENT,
             font=("Segoe UI", 9, "bold")).pack(side="left")
    return frame


# ── Log Viewer ───────────────────────────────────────────────────────────────

_OPEN_LOGVIEWER: list[tk.Tk] = []   # keep reference to avoid GC


def open_log_viewer(log_path: str) -> None:
    """Open (or raise) the log viewer window in a background thread."""
    # Bring existing window to front if already open
    for w in list(_OPEN_LOGVIEWER):
        try:
            w.lift()
            w.focus_force()
            return
        except tk.TclError:
            _OPEN_LOGVIEWER.remove(w)

    t = threading.Thread(target=_run_log_viewer, args=(log_path,), daemon=True)
    t.start()


def _run_log_viewer(log_path: str) -> None:
    try:
        _LogViewerWindow(log_path).run()
    except Exception as exc:
        logger.error("Log viewer error: %s", exc)


class _LogViewerWindow:
    def __init__(self, log_path: str) -> None:
        self._path     = log_path
        self._root     = tk.Tk()
        self._auto_var = tk.BooleanVar(value=True)
        self._filter   = tk.StringVar(value="ALL")
        self._search   = tk.StringVar()
        self._after_id = None

    # ------------------------------------------------------------------
    def run(self) -> None:
        root = self._root
        _OPEN_LOGVIEWER.append(root)
        root.title("Xbox Bridge — Logs")
        root.configure(bg=BG)
        _centre(root, 960, 640)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._load()
        self._schedule_refresh()
        root.mainloop()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = self._root

        # ── Title bar row ─────────────────────────────────────────────
        hdr = tk.Frame(root, bg=SURFACE, height=44)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="  📋  Log Viewer", bg=SURFACE, fg=TEXT,
                 font=("Segoe UI", 11, "bold")).pack(side="left", pady=8)
        tk.Label(hdr, text=self._path, bg=SURFACE, fg=MUTED,
                 font=("Segoe UI", 8)).pack(side="left", padx=8, pady=8)

        # ── Toolbar ───────────────────────────────────────────────────
        bar = tk.Frame(root, bg=SURFACE2, height=38)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        tk.Checkbutton(
            bar, text="Auto-scroll", variable=self._auto_var,
            bg=SURFACE2, fg=TEXT, selectcolor=SURFACE2,
            activebackground=SURFACE2, activeforeground=ACCENT,
            font=("Segoe UI", 9), cursor="hand2",
        ).pack(side="left", padx=10, pady=6)

        tk.Label(bar, text="Level:", bg=SURFACE2, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left")
        level_cb = ttk.Combobox(
            bar, textvariable=self._filter,
            values=["ALL", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            state="readonly", width=9,
        )
        level_cb.pack(side="left", padx=(2, 12), pady=6)
        level_cb.bind("<<ComboboxSelected>>", lambda _: self._load())

        tk.Label(bar, text="Search:", bg=SURFACE2, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left")
        se = tk.Entry(bar, textvariable=self._search,
                      bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                      relief="flat", font=("Segoe UI", 9), width=20)
        se.pack(side="left", padx=(2, 12), pady=6)
        se.bind("<KeyRelease>", lambda _: self._load())

        _styled_button(bar, "⟳  Refresh", self._load).pack(side="left", pady=4, padx=4)
        _styled_button(bar, "Copy All",   self._copy_all).pack(side="left", pady=4, padx=4)

        # ── Text area ─────────────────────────────────────────────────
        text_frame = tk.Frame(root, bg=BG)
        text_frame.pack(fill="both", expand=True, padx=0, pady=0)

        self._text = tk.Text(
            text_frame, wrap="none",
            bg="#0a0a0a", fg=TEXT,
            font=("Consolas", 9),
            relief="flat", borderwidth=0,
            selectbackground=ACCENT, selectforeground="#000",
            insertbackground=TEXT, state="disabled",
        )
        sb_y = tk.Scrollbar(text_frame, orient="vertical",
                            command=self._text.yview,
                            bg=SURFACE, troughcolor=BG)
        sb_x = tk.Scrollbar(text_frame, orient="horizontal",
                            command=self._text.xview,
                            bg=SURFACE, troughcolor=BG)
        self._text.configure(yscrollcommand=sb_y.set,
                             xscrollcommand=sb_x.set)

        sb_y.pack(side="right",  fill="y")
        sb_x.pack(side="bottom", fill="x")
        self._text.pack(fill="both", expand=True)

        # Colour tags
        self._text.tag_configure("time",     foreground=_TIME_COLOR)
        self._text.tag_configure("name",     foreground=_NAME_COLOR)
        self._text.tag_configure("msg",      foreground=TEXT)
        self._text.tag_configure("search_hl",background="#4a3600", foreground="#ffd740")
        for lvl, clr in _LEVEL_COLOR.items():
            self._text.tag_configure(lvl, foreground=clr,
                                     font=("Consolas", 9, "bold"))

        # ── Status / footer bar ───────────────────────────────────────
        foot = tk.Frame(root, bg=SURFACE, height=32)
        foot.pack(fill="x", side="bottom")
        foot.pack_propagate(False)
        self._status = tk.Label(foot, text="", bg=SURFACE, fg=MUTED,
                                font=("Segoe UI", 8))
        self._status.pack(side="left", padx=10, pady=6)
        _styled_button(foot, "Clear Log", self._clear_log).pack(side="right", padx=6, pady=4)
        _styled_button(foot, "Close",     self._on_close, accent=False).pack(side="right", padx=4, pady=4)

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self._path):
            self._set_text("[Log file not found]\n", plain=True)
            return

        with open(self._path, encoding="utf-8", errors="replace") as f:
            raw_lines = f.readlines()

        level_filter = self._filter.get()
        search_term  = self._search.get().lower()

        filtered = []
        for line in raw_lines:
            if level_filter != "ALL":
                if f"[{level_filter}]" not in line:
                    continue
            if search_term and search_term not in line.lower():
                continue
            filtered.append(line.rstrip("\n"))

        self._text.configure(state="normal")
        self._text.delete("1.0", "end")

        for line in filtered:
            m = _LEVEL_RE.match(line)
            if m:
                ts, lvl, name, msg = m.groups()
                self._text.insert("end", ts,         "time")
                self._text.insert("end", f" [{lvl}]", lvl if lvl in _LEVEL_COLOR else "msg")
                self._text.insert("end", f" {name}:", "name")
                self._text.insert("end", msg + "\n",  "msg")
            else:
                self._text.insert("end", line + "\n", "msg")

        # Highlight search term
        if search_term:
            start = "1.0"
            while True:
                pos = self._text.search(search_term, start, stopindex="end",
                                        nocase=True)
                if not pos:
                    break
                end = f"{pos}+{len(search_term)}c"
                self._text.tag_add("search_hl", pos, end)
                start = end

        self._text.configure(state="disabled")

        if self._auto_var.get():
            self._text.see("end")

        size_kb = os.path.getsize(self._path) / 1024
        self._status.configure(
            text=f"{len(filtered)} lines shown  ·  {size_kb:.1f} KB on disk"
        )

    def _set_text(self, content: str, *, plain: bool = False) -> None:
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.insert("end", content)
        self._text.configure(state="disabled")

    def _copy_all(self) -> None:
        try:
            self._text.configure(state="normal")
            content = self._text.get("1.0", "end")
            self._text.configure(state="disabled")
            self._root.clipboard_clear()
            self._root.clipboard_append(content)
        except Exception as exc:
            logger.error("Copy error: %s", exc)

    def _clear_log(self) -> None:
        if not messagebox.askyesno(
            "Clear Log", "Delete the log file contents?",
            parent=self._root
        ):
            return
        try:
            with open(self._path, "w", encoding="utf-8"):
                pass
            self._load()
        except Exception as exc:
            messagebox.showerror("Error", str(exc), parent=self._root)

    def _schedule_refresh(self) -> None:
        if self._auto_var.get():
            self._load()
        try:
            self._after_id = self._root.after(2000, self._schedule_refresh)
        except tk.TclError:
            pass

    def _on_close(self) -> None:
        try:
            if self._after_id:
                self._root.after_cancel(self._after_id)
        except Exception:
            pass
        try:
            _OPEN_LOGVIEWER.remove(self._root)
        except ValueError:
            pass
        self._root.destroy()


# ── Settings Window ───────────────────────────────────────────────────────────

_OPEN_SETTINGS: list[tk.Tk] = []


def open_settings(config_path: str,
                  on_log_level_change=None) -> None:
    """Open (or raise) the settings window."""
    for w in list(_OPEN_SETTINGS):
        try:
            w.lift()
            w.focus_force()
            return
        except tk.TclError:
            _OPEN_SETTINGS.remove(w)

    t = threading.Thread(
        target=_run_settings,
        args=(config_path, on_log_level_change),
        daemon=True,
    )
    t.start()


def _run_settings(config_path: str, on_log_level_change) -> None:
    try:
        _SettingsWindow(config_path, on_log_level_change).run()
    except Exception as exc:
        logger.error("Settings window error: %s", exc)


class _SettingsWindow:
    def __init__(self, config_path: str, on_log_level_change) -> None:
        self._path    = config_path
        self._on_lvl  = on_log_level_change
        self._root    = tk.Tk()
        self._cfg     = configparser.ConfigParser()

    # ------------------------------------------------------------------
    def run(self) -> None:
        root = self._root
        _OPEN_SETTINGS.append(root)
        root.title("Xbox Bridge — Settings")
        root.configure(bg=BG)
        root.resizable(False, False)
        _centre(root, 440, 480)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Try to load existing config
        if os.path.exists(self._path):
            self._cfg.read(self._path, encoding="utf-8")

        self._build_ui()
        root.mainloop()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = self._root

        # Title bar
        hdr = tk.Frame(root, bg=SURFACE, height=48)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="  ⚙   Settings", bg=SURFACE, fg=TEXT,
                 font=("Segoe UI", 12, "bold")).pack(side="left", pady=10)

        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, padx=24, pady=16)

        def row(parent, label_text: str, widget_factory) -> tk.Widget:
            f = tk.Frame(parent, bg=BG)
            f.pack(fill="x", pady=5)
            tk.Label(f, text=label_text, bg=BG, fg=MUTED,
                     font=("Segoe UI", 9), width=20, anchor="w").pack(side="left")
            w = widget_factory(f)
            w.pack(side="left", fill="x", expand=True)
            return w

        # ── Network section ───────────────────────────────────────────
        _section_header(body, "Network").pack(fill="x", pady=(0, 8))

        port_val = self._cfg.get("app", "listen_port", fallback="9999")
        self._port_var = tk.StringVar(value=port_val)

        def _port_widget(p):
            sb = tk.Spinbox(p, from_=1024, to=65535, textvariable=self._port_var,
                            bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                            buttonbackground=SURFACE2, relief="flat",
                            font=("Segoe UI", 10), width=10)
            return sb

        row(body, "Listen Port", _port_widget)

        host_val = os.environ.get("LISTEN_HOST", "0.0.0.0")
        self._host_var = tk.StringVar(value=host_val)

        def _host_widget(p):
            e = tk.Entry(p, textvariable=self._host_var,
                         bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                         relief="flat", font=("Segoe UI", 10), width=16)
            return e

        row(body, "Listen Host", _host_widget)

        # ── Logging section ───────────────────────────────────────────
        tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=12)
        _section_header(body, "Logging").pack(fill="x", pady=(0, 8))

        current_level = logging.getLevelName(logging.getLogger().level)
        self._level_var = tk.StringVar(value=current_level)

        def _level_widget(p):
            cb = ttk.Combobox(p, textvariable=self._level_var,
                              values=["DEBUG", "INFO", "WARNING", "ERROR"],
                              state="readonly", width=12)
            return cb

        row(body, "Log Level", _level_widget)

        # ── Note ──────────────────────────────────────────────────────
        tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=12)
        note = tk.Frame(body, bg=SURFACE2, padx=12, pady=10)
        note.pack(fill="x")
        tk.Label(note, text="⚠  Port and host changes take effect after restart.",
                 bg=SURFACE2, fg="#ffb74d",
                 font=("Segoe UI", 8), wraplength=360, justify="left").pack(anchor="w")
        tk.Label(note, text="Log level changes apply immediately.",
                 bg=SURFACE2, fg=MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 0))

        # ── Config file path ──────────────────────────────────────────
        tk.Label(body, text=f"Config: {self._path}",
                 bg=BG, fg=MUTED,
                 font=("Segoe UI", 7)).pack(anchor="w", pady=(10, 0))

        # ── Footer buttons ────────────────────────────────────────────
        foot = tk.Frame(root, bg=SURFACE, height=48)
        foot.pack(fill="x", side="bottom")
        foot.pack_propagate(False)
        _styled_button(foot, "Cancel", self._on_close).pack(side="right", padx=8, pady=8)
        _styled_button(foot, "Save",   self._save, accent=True).pack(side="right", padx=4, pady=8)

    # ------------------------------------------------------------------
    def _save(self) -> None:
        # Validate port
        try:
            port = int(self._port_var.get())
            if not (1024 <= port <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Port",
                                 "Port must be a number between 1024 and 65535.",
                                 parent=self._root)
            return

        # Write config.ini
        if not self._cfg.has_section("app"):
            self._cfg.add_section("app")
        if not self._cfg.has_section("network"):
            self._cfg.add_section("network")

        self._cfg.set("app", "listen_port", str(port))
        self._cfg.set("app", "listen_host", self._host_var.get().strip())

        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                self._cfg.write(f)
        except Exception as exc:
            messagebox.showerror("Save Error", str(exc), parent=self._root)
            return

        # Apply log level immediately (no restart needed)
        new_level = self._level_var.get()
        logging.getLogger().setLevel(getattr(logging, new_level, logging.INFO))
        if self._on_lvl:
            try:
                self._on_lvl(new_level)
            except Exception:
                pass

        messagebox.showinfo(
            "Saved",
            "Settings saved.\nRestart Xbox Bridge for port/host changes to take effect.",
            parent=self._root,
        )
        self._on_close()

    def _on_close(self) -> None:
        try:
            _OPEN_SETTINGS.remove(self._root)
        except ValueError:
            pass
        self._root.destroy()
