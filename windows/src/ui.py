"""Custom UI windows — dark-themed log viewer and settings editor.

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


# ── Log Viewer ────────────────────────────────────────────────────────────────

_OPEN_LOGVIEWER: list[tk.Tk] = []


def open_log_viewer(log_path: str) -> None:
    """Open (or raise) the log viewer in a daemon thread."""
    for w in list(_OPEN_LOGVIEWER):
        try:
            w.after(0, w.lift)
            w.after(0, w.focus_force)
            return
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
        m.pack(side="left", padx=(2, 12), pady=6)
        self._filter.trace_add("write", lambda *_: self._load())

        _lbl(bar, "Search:", bg=SURFACE2, color=MUTED).pack(side="left")
        se = tk.Entry(bar, textvariable=self._search,
                      bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                      relief="flat", font=("Segoe UI", 9), width=22,
                      highlightthickness=1, highlightcolor=ACCENT,
                      highlightbackground=BORDER)
        se.pack(side="left", padx=(2, 12), ipady=4)
        se.bind("<KeyRelease>", lambda _: self._load())

        _btn(bar, "⟳  Refresh", self._load).pack(side="left", pady=6, padx=4)
        _btn(bar, "Copy All",   self._copy).pack(side="left", pady=6, padx=4)

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

        level_f = self._filter.get()
        search  = self._search.get().lower()
        lines   = []
        for ln in raw:
            s = ln.rstrip("\n")
            if level_f != "ALL" and f"[{level_f}]" not in s:
                continue
            if search and search not in s.lower():
                continue
            lines.append(s)

        self._txt.configure(state="normal")
        self._txt.delete("1.0", "end")
        for ln in lines:
            m = _LEVEL_RE.match(ln)
            if m:
                ts, lvl, nm, msg = m.groups()
                self._txt.insert("end", ts,           "time")
                self._txt.insert("end", f" [{lvl}]",  lvl if lvl in _LEVEL_COLOR else "msg")
                self._txt.insert("end", f" {nm}:",    "name")
                self._txt.insert("end", msg + "\n",   "msg")
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
        kb = os.path.getsize(self._path) / 1024
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


def open_settings(config_path: str, on_log_level_change=None) -> None:
    """Open (or raise) the settings window."""
    for w in list(_OPEN_SETTINGS):
        try:
            w.after(0, w.lift)
            w.after(0, w.focus_force)
            return
        except (tk.TclError, RuntimeError):
            _OPEN_SETTINGS.remove(w)
    threading.Thread(
        target=_run_settings,
        args=(config_path, on_log_level_change),
        daemon=True,
    ).start()


def _run_settings(config_path: str, on_log_level_change) -> None:
    try:
        _SettingsWindow(config_path, on_log_level_change).run()
    except Exception:
        logger.error("Settings window crashed:\n%s", traceback.format_exc())


class _SettingsWindow:
    def __init__(self, config_path: str, on_log_level_change) -> None:
        self._path   = config_path
        self._on_lvl = on_log_level_change
        self._root   = tk.Tk()
        self._cfg    = configparser.ConfigParser()

    def run(self) -> None:
        r = self._root
        _OPEN_SETTINGS.append(r)
        r.title("Xbox Bridge — Settings")
        r.configure(bg=BG)
        r.resizable(False, False)
        _centre(r, 440, 500)
        r.protocol("WM_DELETE_WINDOW", self._close)
        if os.path.exists(self._path):
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
        _lbl(note, "Log level changes apply immediately.",
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
        _lbl(f, label, color=MUTED, bg=BG).pack(side="left", anchor="w", width=120)
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

        if not self._cfg.has_section("app"):
            self._cfg.add_section("app")
        if not self._cfg.has_section("network"):
            self._cfg.add_section("network")

        self._cfg.set("app", "listen_port", str(port))
        self._cfg.set("app", "listen_host", self._host_var.get().strip())

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
