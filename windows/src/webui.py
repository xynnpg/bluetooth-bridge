"""Local Flask dashboard for the Windows bridge.

Replaces the old Tkinter windows. Runs a small HTTP server on localhost
(127.0.0.1 by default) that serves a single-page dark dashboard with:

  * live connection / controller / battery / rumble cards
  * a scrollable event feed
  * a filterable log viewer
  * every configuration option (and then some)
  * action buttons: reconnect, reset controller, reset settings,
    open folder, clear log, quit

The server runs on a daemon thread so it never blocks the tray.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import sys
import threading
import time
import webbrowser

logger = logging.getLogger("webui")

try:
    from flask import Flask, Response, jsonify, request
    _FLASK_AVAILABLE = True
except ImportError:
    _FLASK_AVAILABLE = False
    Flask = None  # type: ignore
    Response = jsonify = request = None  # type: ignore

try:
    from waitress import serve as _waitress_serve
    _WAITRESS_AVAILABLE = True
except ImportError:
    _WAITRESS_AVAILABLE = False
    _waitress_serve = None  # type: ignore


VERSION = "1.1.0"

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LEVEL_RE = re.compile(
    r"^(\d{2}:\d{2}:\d{2})\s+\[(\w+)\]\s+([\w.]+):\s?(.*)$"
)
_MODULES = ("main", "receiver", "emitter", "controller", "rumble",
            "tray", "webui", "state", "network", "discovery",
            "bluetooth", "config")


def _read_tail(path: str, limit: int = 4000) -> list[str]:
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip("\n") for ln in fh.readlines()[-limit:]]
    except OSError:
        return []


def _port_free(host: str, port: int) -> bool:
    if port <= 0:
        return False
    bind_host = "" if host in ("0.0.0.0", "::") else host
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((bind_host, port))
            return True
    except OSError:
        return False


def _pick_free_port(host: str) -> int:
    bind_host = "" if host in ("0.0.0.0", "::") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((bind_host, 0))
        return s.getsockname()[1]


class WebUI:
    """Owns the Flask app and its background server thread."""

    def __init__(self, *, state, config, log_path: str, install_dir: str,
                 config_path: str, listen_addr: str,
                 callbacks: dict | None = None) -> None:
        self._state        = state
        self._config       = config
        self._log_path     = log_path
        self._install_dir  = install_dir
        self._config_path  = config_path
        self._listen_addr  = listen_addr
        self._callbacks    = callbacks or {}
        self._thread: threading.Thread | None = None
        self._server       = None
        self._url          = ""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        return self._url

    @property
    def enabled(self) -> bool:
        return _FLASK_AVAILABLE and self._config.get_bool("webui.enabled")

    def start(self) -> None:
        if not _FLASK_AVAILABLE:
            logger.warning("Flask not installed — web UI disabled")
            return
        if not self._config.get_bool("webui.enabled"):
            logger.info("Web UI disabled in config")
            return
        if self._thread is not None:
            return

        host = self._config.get_str("webui.host") or "127.0.0.1"
        port = self._config.get_int("webui.port")
        if port <= 0 or not _port_free(host, port):
            new_port = _pick_free_port(host)
            logger.info("Web UI port %s unavailable — using random port %d",
                        port, new_port)
            try:
                self._config.update({"webui.port": new_port})
                self._config.save()
            except Exception as exc:
                logger.debug("Could not persist web UI port: %s", exc)
            port = new_port
        self._url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"

        app = self._build_app()
        self._thread = threading.Thread(
            target=self._serve, args=(app, host, port),
            name="WebUI", daemon=True,
        )
        self._thread.start()
        logger.info("Web UI starting at %s", self._url)

        if self._config.get_bool("webui.open_on_launch"):
            threading.Timer(1.0, self.open_browser).start()

    def _serve(self, app, host: str, port: int) -> None:
        try:
            if _WAITRESS_AVAILABLE:
                _waitress_serve(app, host=host, port=port, threads=4)
            else:
                app.run(host=host, port=port, debug=False,
                        use_reloader=False, threaded=True)
        except Exception as exc:
            logger.error("Web UI server stopped: %s", exc)

    def stop(self) -> None:
        try:
            if self._server is not None:
                self._server.close()
        except Exception:
            pass

    def open_browser(self) -> None:
        if not _FLASK_AVAILABLE:
            logger.error("Cannot open Web UI — Flask is not installed "
                         "(run: pip install flask waitress)")
            return
        if self._thread is None or not self._thread.is_alive():
            logger.info("Web UI not running — starting it now")
            self._thread = None
            self.start()
        url = self._url
        if not url:
            host = self._config.get_str("webui.host") or "127.0.0.1"
            port = self._config.get_int("webui.port")
            if port > 0:
                url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
        if not url:
            logger.warning("Web UI URL not available yet")
            return
        opened = False
        if sys.platform == "win32":
            try:
                os.startfile(url)  # type: ignore[attr-defined]
                opened = True
            except Exception as exc:
                logger.debug("os.startfile(%s) failed: %s", url, exc)
        if not opened:
            try:
                opened = webbrowser.open(url)
            except Exception as exc:
                opened = False
                logger.debug("webbrowser.open failed: %s", exc)
        if opened:
            logger.info("Opened Web UI at %s", url)
        else:
            logger.warning("Could not launch a browser — open %s manually", url)

    # ------------------------------------------------------------------
    # Flask app
    # ------------------------------------------------------------------

    def _build_app(self):
        app = Flask("xbox_bridge")
        app.logger.disabled = True
        logging.getLogger("werkzeug").setLevel(logging.ERROR)

        @app.get("/")
        def index():
            return Response(_PAGE, mimetype="text/html")

        @app.get("/api/bootstrap")
        def bootstrap():
            return jsonify({
                "version":     VERSION,
                "url":         self._url,
                "listen_addr": self._listen_addr,
                "log_path":    self._log_path,
                "config_path": self._config_path,
                "install_dir": self._install_dir,
                "modules":     list(_MODULES),
                "levels":      list(_LEVELS),
                "schema":      self._config.schema(),
            })

        @app.get("/api/state")
        def state():
            try:
                snap = self._state.snapshot()
            except Exception as exc:
                logger.debug("snapshot error: %s", exc)
                return jsonify({"error": str(exc)}), 500
            snap["config"] = {
                "low_battery_warn_pct": self._config.get_int(
                    "controller.low_battery_warn_pct"),
                "rumble_enabled": self._config.get_bool(
                    "controller.rumble_enabled"),
                "battery_display_enabled": self._config.get_bool(
                  "controller.battery_display_enabled"),
                "performance_warning_enabled": self._config.get_bool(
                  "controller.performance_warning_enabled"),
                "performance_warning_ms": self._config.get_int(
                  "controller.performance_warning_ms"),
                "refresh_ms": self._config.get_int("webui.auto_refresh_ms"),
            }
            return jsonify(snap)

        @app.get("/api/logs")
        def logs():
            level  = (request.args.get("level") or "ALL").upper()
            module = (request.args.get("module") or "ALL")
            search = (request.args.get("search") or "").lower()
            limit  = min(int(request.args.get("limit", 1500)), 8000)

            out: list[dict] = []
            for line in _read_tail(self._log_path):
                if level != "ALL" and f"[{level}]" not in line:
                    continue
                if module != "ALL":
                    m = _LEVEL_RE.match(line)
                    if not m or m.group(3) != module:
                        continue
                if search and search not in line.lower():
                    continue
                m = _LEVEL_RE.match(line)
                if m:
                    ts, lvl, name, msg = m.groups()
                else:
                    ts, lvl, name, msg = "", "", "", line
                out.append({"ts": ts, "level": lvl, "name": name,
                            "msg": msg, "raw": line})
            out = out[-limit:]
            try:
                size_kb = os.path.getsize(self._log_path) / 1024
            except OSError:
                size_kb = 0
            return jsonify({"lines": out, "count": len(out),
                            "size_kb": round(size_kb, 1)})

        @app.get("/api/config")
        def get_config():
            return jsonify({"values": self._config.as_dict(),
                            "schema": self._config.schema()})

        @app.post("/api/config")
        def set_config():
            data = request.get_json(silent=True) or {}
            values = data.get("values", data)
            try:
                before = self._config.as_dict()
                self._config.update(values)
                self._config.save()
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            except OSError as exc:
                return jsonify({"ok": False,
                                "error": f"Could not write config: {exc}"}), 500

            after = self._config.as_dict()
            changed = {k: v for k, v in after.items() if before.get(k) != v}
            restart = self._config.restart_required(changed)

            cb = self._callbacks.get("settings_applied")
            if cb:
                try:
                    cb(changed)
                except Exception as exc:
                    logger.error("settings_applied callback error: %s", exc)

            return jsonify({"ok": True, "changed": changed,
                            "restart_required": restart,
                            "values": after})

        @app.post("/api/config/reset")
        def reset_config():
            from .config import SPEC
            values = {f.name: f.default for f in SPEC}
            try:
                before = self._config.as_dict()
                self._config.update(values)
                self._config.save()
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 500
            after = self._config.as_dict()
            changed = {k: v for k, v in after.items() if before.get(k) != v}
            cb = self._callbacks.get("settings_applied")
            if cb:
                try:
                    cb(changed)
                except Exception:
                    pass
            return jsonify({"ok": True, "values": after,
                            "restart_required":
                                self._config.restart_required(changed)})

        @app.post("/api/action")
        def action():
            data = request.get_json(silent=True) or {}
            name = data.get("action", "")
            handler = self._callbacks.get(name)
            if handler is None:
                return jsonify({"ok": False,
                                "error": f"Unknown action: {name}"}), 400
            try:
                result = handler()
            except Exception as exc:
                logger.error("Action %s failed: %s", name, exc)
                return jsonify({"ok": False, "error": str(exc)}), 500
            return jsonify({"ok": True, "result": result,
                            "message": _ACTION_MESSAGES.get(name, "Done.")})

        @app.post("/api/logs/clear")
        def clear_logs():
            try:
                if os.path.exists(self._log_path):
                    open(self._log_path, "w", encoding="utf-8").close()
            except OSError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 500
            return jsonify({"ok": True})

        return app


_ACTION_MESSAGES = {
    "reconnect":        "TCP listener restarted.",
    "reset_controller": "Virtual controller reset.",
    "open_folder":      "Folder opened.",
    "open_logs":        "Log folder opened.",
    "quit":             "Shutting down…",
}


# ---------------------------------------------------------------------------
# Single-page dashboard (no Jinja — served verbatim)
# ---------------------------------------------------------------------------

_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Xbox Bridge</title>
<style>
  :root{--bg:#101513;--surface:#18201c;--surface2:#222c26;--border:#334139;
    --text:#e6eee7;--muted:#8e9b91;--accent:#48d18a;--accent-h:#6ce0a1;
    --warn:#f0b45f;--err:#f07d73;--blue:#76c7ed;--purple:#c4a0e2;
    --ink:#0b100e;--shadow:0 14px 34px rgba(0,0,0,.24)}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Trebuchet MS","Segoe UI",sans-serif}
  body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.2;background-image:radial-gradient(#718077 .7px,transparent .7px);background-size:16px 16px}
  a{color:var(--accent)}
  header{display:flex;align-items:center;gap:12px;padding:18px max(22px,calc((100vw - 1240px)/2));
    background:var(--ink);color:#edf5ee;position:sticky;top:0;z-index:10;box-shadow:0 2px 12px #0008}
  header .logo{font-weight:700;font-size:19px;letter-spacing:.01em}
  header .dot{width:11px;height:11px;border-radius:50%;background:#66736a;border:2px solid #d8e6da55}
  header .dot.on{background:#62d095;box-shadow:0 0 0 4px #62d09520}
  header .dot.warn{background:#f0b45f}
  header .spacer{flex:1}
  header .pill{font-size:12px;color:#c8d0c8}
  nav{display:flex;gap:4px;padding:0 max(16px,calc((100vw - 1240px)/2));background:var(--surface);
    border-bottom:1px solid var(--border);overflow:auto}
  nav button{background:none;border:0;color:var(--muted);padding:14px 17px;white-space:nowrap;
    font:700 13px "Trebuchet MS","Segoe UI",sans-serif;cursor:pointer;border-bottom:3px solid transparent}
  nav button:hover{color:var(--text);background:var(--surface2)}
  nav button.active{color:var(--accent);border-bottom-color:var(--accent)}
  main{position:relative;padding:30px 22px 42px;max-width:1240px;margin:0 auto}
  .tab{display:none}
  .tab.active{display:block}
    .grid{display:grid;gap:16px;grid-template-columns:1fr 1fr}.grid3{display:grid;gap:16px;grid-template-columns:repeat(3,1fr)}
    @media(max-width:820px){.grid,.grid3{grid-template-columns:1fr}main{padding:22px 14px 34px}}
    .card{background:var(--surface);border:1px solid var(--border);border-radius:7px;padding:20px;box-shadow:var(--shadow)}
    .card h3{margin:0 0 14px;font-size:12px;text-transform:uppercase;letter-spacing:.1em;color:var(--accent)}
  .card h3.b{color:var(--blue)} .card h3.p{color:var(--purple)}
  .kv{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-bottom:1px dashed var(--border)}
  .kv:last-child{border-bottom:none}
  .kv .k{color:var(--muted)}
    .kv .v{font-family:"Cascadia Code",Consolas,monospace;font-size:12px;text-align:right;word-break:break-all}
    .bar{height:9px;border-radius:20px;background:var(--surface2);overflow:hidden;margin-top:9px}
    .bar>i{display:block;height:100%;width:0;background:var(--accent);transition:width .18s ease,background .18s ease}
  .bar.sm{height:7px}
  .stat{font-size:30px;font-weight:700;font-family:"Cascadia Code",Consolas,monospace}
  .sub{color:var(--muted);font-size:12px}
    .btn{background:var(--surface);color:var(--text);border:1px solid #526258;border-radius:5px;padding:10px 15px;cursor:pointer;font:700 13px "Trebuchet MS","Segoe UI",sans-serif;transition:.15s}
    .btn:hover{background:var(--surface2);border-color:#74857a;transform:translateY(-1px)}
    .btn.accent{background:var(--accent);color:#07130c;border-color:var(--accent)}.btn.accent:hover{background:var(--accent-h)}
    .btn.danger{border-color:#77443f;color:var(--err)}.btn.danger:hover{background:#351c1b}
  .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  pre.log{background:#0b100e;color:#dfe8df;border:1px solid #26372b;border-radius:5px;padding:14px;margin:0;height:520px;overflow:auto;font:12px/1.6 "Cascadia Code",Consolas,monospace;white-space:pre}
  .lg-time{color:#4e9a06}.lg-name{color:#8b9dc3}
  .lg-DEBUG{color:#5a5a5a}.lg-INFO{color:#64b5f6}.lg-WARNING{color:#ffb74d}
  .lg-ERROR{color:#ef5350}.lg-CRITICAL{color:#ff1744;font-weight:700}
    select,input[type=text],input[type=number]{background:var(--surface);color:var(--text);border:1px solid #526258;border-radius:5px;padding:9px 10px;font:13px "Trebuchet MS","Segoe UI",sans-serif;outline:none}
    select:focus,input:focus,.btn:focus-visible,nav button:focus-visible{border-color:var(--accent);outline:3px solid #167a5430;outline-offset:1px}
  input[type=checkbox]{accent-color:var(--accent);width:16px;height:16px}
  .field{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:13px 0;border-bottom:1px solid var(--border)}
  .field:last-child{border-bottom:none}
  .field .meta{max-width:70%}
  .field .meta .lbl{font-weight:600}
  .field .meta .hint{color:var(--muted);font-size:12px;margin-top:2px}
  .field .ctl{flex:0 0 auto}
  .field .ctl input[type=number],.field .ctl input[type=text]{width:150px}
  .section{margin:0 0 10px;color:var(--accent);font-weight:700;font-size:12px;
           text-transform:uppercase;letter-spacing:.08em}
  .toast{position:fixed;right:18px;bottom:18px;background:var(--ink);color:#edf5ee;
         border:1px solid var(--border);border-left:3px solid var(--accent);
         border-radius:8px;padding:12px 16px;max-width:380px;
         box-shadow:0 8px 30px #0009;transform:translateY(20px);
         opacity:0;transition:.2s;pointer-events:none;z-index:50}
  .toast.show{transform:none;opacity:1}
  .toast.err{border-left-color:var(--err)}
  .ev{height:220px;overflow:auto;background:#0b100e;color:#dfe8df;border-radius:5px;padding:12px;font:12px/1.7 "Cascadia Code",Consolas,monospace;white-space:pre-wrap}
  .ev .t{color:#4e9a06}
  .footerbar{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}
  .tag{font-size:11px;color:var(--muted);border:1px solid var(--border);border-radius:20px;padding:3px 10px}
  .dash-intro{display:flex;align-items:end;justify-content:space-between;gap:20px;margin:0 0 22px}.dash-intro h1{margin:0;font-size:30px;line-height:1.1;letter-spacing:-.02em}.dash-intro p{margin:7px 0 0;color:var(--muted)}
  .eyebrow{margin:0 0 7px;color:var(--accent);font-size:11px;font-weight:700;letter-spacing:.13em;text-transform:uppercase}
  @media(max-width:620px){.dash-intro{display:block}.dash-intro h1{font-size:26px}.field{align-items:flex-start;flex-direction:column}.field .meta{max-width:none}.field .ctl{width:100%}.field .ctl input[type=number],.field .ctl input[type=text],.field .ctl select{width:100%}}
    .perf-warning{display:none;background:#4a350d;color:#ffd180;border:1px solid #a66b12;
      border-radius:8px;padding:12px 14px;margin-bottom:14px}
    .perf-warning.show{display:block}
    .perf-warning strong{color:#ffe0a3}
    .metric{font-family:Consolas,monospace;font-size:18px;font-weight:700}
    .button-history{max-height:360px;overflow:auto;background:#090909;border:1px solid var(--border);
      border-radius:8px;padding:10px;font:12px/1.7 Consolas,monospace}
    .button-history .pressed{color:var(--accent)}
    .button-history .released{color:var(--muted)}
</style>
</head>
<body>
<header>
  <span class="dot" id="hdot"></span>
  <span class="logo">Xbox Bridge</span>
  <span class="pill" id="hstatus">connecting…</span>
  <span class="spacer"></span>
  <span class="pill" id="hver"></span>
</header>

<nav>
  <button data-tab="dash" class="active">Dashboard</button>
  <button data-tab="events">Events</button>
  <button data-tab="logs">Logs</button>
  <button data-tab="settings">Settings</button>
  <button data-tab="about">About</button>
</nav>

<main>
  <!-- DASHBOARD -->
  <section class="tab active" id="tab-dash">
    <div class="dash-intro">
      <div>
        <div class="eyebrow">Local control surface</div>
        <h1>Bridge status</h1>
        <p>Keep an eye on the link between your Linux controller and Windows.</p>
      </div>
      <span class="tag">LIVE MONITORING · <span id="d-status">—</span></span>
    </div>
    <div class="perf-warning" id="perf-warning">
      <strong>Network performance is degraded.</strong>
      Requests or packet timing are slow. Disable vibration and/or battery display below to improve performance.
    </div>
    <div class="grid3">
      <div class="card">
        <h3>Connection</h3>
        <div class="kv"><span class="k">Status</span><span class="v" id="d-status-card">—</span></div>
        <div class="kv"><span class="k">Peer IP</span><span class="v" id="d-peer">—</span></div>
        <div class="kv"><span class="k">Listen</span><span class="v" id="d-listen">—</span></div>
        <div class="kv"><span class="k">Uptime</span><span class="v" id="d-uptime">—</span></div>
        <div class="kv"><span class="k">Packets</span><span class="v" id="d-packets">—</span></div>
      </div>
      <div class="card">
        <h3>Controller</h3>
        <div class="kv"><span class="k">Name</span><span class="v" id="d-name">—</span></div>
        <div class="kv"><span class="k">MAC</span><span class="v" id="d-mac">—</span></div>
        <div class="kv"><span class="k">Battery</span><span class="v" id="d-bat">—</span></div>
        <div class="bar"><i id="d-batbar"></i></div>
        <div class="kv" style="margin-top:10px"><span class="k">Last event</span><span class="v" id="d-lastev">—</span></div>
      </div>
      <div class="card">
        <h3 class="b">Rumble</h3>
        <div class="kv"><span class="k">Left</span><span class="v" id="d-rl">0</span></div>
        <div class="bar sm"><i id="d-rlbar" style="background:var(--blue)"></i></div>
        <div class="kv" style="margin-top:10px"><span class="k">Right</span><span class="v" id="d-rr">0</span></div>
        <div class="bar sm"><i id="d-rrbar" style="background:var(--purple)"></i></div>
      </div>
    </div>

    <div class="card" style="margin-top:14px">
      <h3 class="p">Performance</h3>
      <div class="grid3">
        <div><div class="sub">Dashboard request</div><div class="metric" id="d-request">—</div></div>
        <div><div class="sub">Packet interval</div><div class="metric" id="d-packet">—</div></div>
        <div><div class="sub">Packet rate</div><div class="metric" id="d-rate">—</div></div>
      </div>
      <div class="row" style="margin-top:14px">
        <label class="row" style="gap:6px"><input type="checkbox" id="quick-rumble"> Vibration</label>
        <label class="row" style="gap:6px"><input type="checkbox" id="quick-battery"> Battery display</label>
        <button class="btn" onclick="applyQuickSettings()">Apply</button>
      </div>
    </div>

    <div class="card" style="margin-top:14px">
      <h3>Recent events</h3>
      <div class="ev" id="d-events"></div>
    </div>

    <div class="footerbar">
      <button class="btn accent" onclick="act('reconnect')">Reconnect</button>
      <button class="btn" onclick="act('reset_controller')">Reset Controller</button>
      <button class="btn" onclick="act('open_folder')">Open App Folder</button>
      <button class="btn" onclick="clearLogs()">Clear Log</button>
      <button class="btn danger" onclick="quit()">Quit App</button>
    </div>
  </section>

  <!-- EVENTS -->
  <section class="tab" id="tab-events">
    <div class="card">
      <h3>Live event feed</h3>
      <div class="ev" id="e-events" style="height:620px"></div>
    </div>
  </section>

  <!-- LOGS -->
  <section class="tab" id="tab-logs">
    <div class="card">
      <div class="row" style="margin-bottom:12px">
        <select id="l-level"></select>
        <select id="l-module"></select>
        <input type="text" id="l-search" placeholder="Search…" style="min-width:220px">
        <label class="row" style="gap:6px"><input type="checkbox" id="l-auto" checked> Auto-refresh</label>
        <button class="btn" onclick="loadLogs()">Refresh</button>
        <button class="btn" onclick="copyLogs()">Copy</button>
        <button class="btn danger" onclick="clearLogs()">Clear</button>
        <span class="spacer" style="flex:1"></span>
        <span class="tag" id="l-stat">—</span>
      </div>
      <pre class="log" id="l-out"></pre>
    </div>
    <div class="card" style="margin-top:14px">
      <h3>Button history</h3>
      <div class="button-history" id="l-buttons"><span class="sub">No button transitions yet.</span></div>
    </div>
  </section>

  <!-- SETTINGS -->
  <section class="tab" id="tab-settings">
    <div class="card">
      <div id="s-form"></div>
      <div class="footerbar">
        <button class="btn accent" onclick="saveSettings()">Save Settings</button>
        <button class="btn" onclick="loadSettings()">Reload</button>
        <button class="btn danger" onclick="resetSettings()">Reset to Defaults</button>
      </div>
    </div>
  </section>

  <!-- ABOUT -->
  <section class="tab" id="tab-about">
    <div class="card">
      <h3>About</h3>
      <div class="kv"><span class="k">Version</span><span class="v" id="a-ver">—</span></div>
      <div class="kv"><span class="k">Dashboard URL</span><span class="v" id="a-url">—</span></div>
      <div class="kv"><span class="k">Listen address</span><span class="v" id="a-listen">—</span></div>
      <div class="kv"><span class="k">Config file</span><span class="v" id="a-config">—</span></div>
      <div class="kv"><span class="k">Log file</span><span class="v" id="a-log">—</span></div>
      <div class="kv"><span class="k">Install dir</span><span class="v" id="a-install">—</span></div>
    </div>
  </section>
</main>

<div class="toast" id="toast"></div>

<script>
const $ = s => document.querySelector(s);
let BOOT = null, REFRESH = 750, lastEventHtml = "";
let lastRequestMs = -1;

function toast(msg, isErr){
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.className = "toast", 3200);
}

async function api(path, opts){
  const r = await fetch(path, opts);
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((body && body.error) || ("HTTP " + r.status));
  return body;
}

/* ---- tabs ---- */
document.querySelectorAll("nav button").forEach(b => b.onclick = () => {
  document.querySelectorAll("nav button").forEach(x => x.classList.remove("active"));
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  $("#tab-" + b.dataset.tab).classList.add("active");
  if (b.dataset.tab === "logs") loadLogs();
});

function fmtUptime(s){
  s = Math.max(0, s|0);
  const h = (s/3600)|0, m = ((s%3600)/60)|0, sec = s%60;
  const p = n => String(n).padStart(2,"0");
  return h ? `${p(h)}:${p(m)}:${p(sec)}` : `${p(m)}:${p(sec)}`;
}

function battColor(b){
  if (b == null || b < 0) return "var(--muted)";
  if (b >= 50) return "var(--accent)";
  if (b >= 20) return "var(--warn)";
  return "var(--err)";
}

function renderEvents(snap){
  const lines = snap.recent_events || [];
  const html = lines.map(l => {
    const m = l.match(/^(\d{2}:\d{2}:\d{2})\s+(\S+)\s+([\w.]+):(.*)$/);
    if (!m) return l;
    return `<span class="t">${m[1]}</span> <b class="lg-${m[2]}">${m[2]}</b> ${m[3]}:${m[4]}`;
  }).join("\n");
  if (html !== lastEventHtml){
    lastEventHtml = html;
    for (const id of ["#d-events", "#e-events"]){
      const el = $(id); if(!el) continue;
      const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 24;
      el.innerHTML = html || '<span class="sub">No events yet.</span>';
      if (atBottom) el.scrollTop = el.scrollHeight;
    }
  }
}

function renderButtonHistory(events){
  const html = (events || []).slice().reverse().map(e =>
    `<div><span class="lg-time">${escapeHtml(e.ts)}</span> ` +
    `<b class="${e.action}">${escapeHtml(e.button)} ${escapeHtml(e.action)}</b></div>`
  ).join("");
  $("#l-buttons").innerHTML = html || '<span class="sub">No button transitions yet.</span>';
}

async function applyQuickSettings(){
  try{
    await api("/api/config", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({values: {
        "controller.rumble_enabled": $("#quick-rumble").checked,
        "controller.battery_display_enabled": $("#quick-battery").checked
      }})});
    toast("Performance settings applied.");
  }catch(e){ toast(e.message, true); }
}

async function pollState(){
  const requestStarted = performance.now();
  try{
    const snap = await api("/api/state");
    lastRequestMs = Math.round(performance.now() - requestStarted);
    REFRESH = (snap.config && snap.config.refresh_ms) || 750;

    const dot = $("#hdot"), hs = $("#hstatus");
    if (snap.pc_reachable){
      dot.className = "dot on";
      hs.textContent = "Connected — " + (snap.peer_ip || "PC");
    } else if (snap.connected){
      dot.className = "dot warn";
      hs.textContent = "Waiting for Linux…";
    } else {
      dot.className = "dot";
      hs.textContent = "Disconnected";
    }

    const bridgeStatus = snap.pc_reachable ? "Streaming" : (snap.connected ? "Partial" : "Offline");
    $("#d-status").textContent = bridgeStatus;
    $("#d-status-card").textContent = bridgeStatus;
    $("#d-peer").textContent = snap.peer_ip || "(none)";
    $("#d-uptime").textContent = fmtUptime(snap.uptime_s);
    $("#d-packets").textContent = (snap.packets_sent||0).toLocaleString();
    $("#d-request").textContent = lastRequestMs + " ms";
    $("#d-packet").textContent = snap.last_packet_ms >= 0 ? snap.last_packet_ms + " ms" : "—";
    $("#d-rate").textContent = snap.packet_rate > 0 ? snap.packet_rate + " /s" : "—";
    $("#d-name").textContent = snap.controller_name || "(unknown)";
    $("#d-mac").textContent = snap.controller_mac || "(unknown)";
    $("#d-lastev").textContent = snap.last_event_ts || "—";

    const batteryEnabled = !!(snap.config && snap.config.battery_display_enabled);
    $("#quick-rumble").checked = !!(snap.config && snap.config.rumble_enabled);
    $("#quick-battery").checked = batteryEnabled;
    $("#d-bat").parentElement.parentElement.style.display = batteryEnabled ? "" : "none";
    $("#d-batbar").parentElement.style.display = batteryEnabled ? "" : "none";
    const b = snap.battery;
    const glyph = snap.charging ? " ⚡" : "";
    $("#d-bat").textContent = (b == null || b < 0) ? "Unknown" : b + "%" + glyph;
    const bar = $("#d-batbar");
    bar.style.width = (b == null || b < 0) ? "0%" : Math.max(0, Math.min(100, b)) + "%";
    bar.style.background = battColor(b);

    const rl = snap.rumble_left|0, rr = snap.rumble_right|0;
    $("#d-rl").textContent = rl; $("#d-rr").textContent = rr;
    $("#d-rlbar").style.width = (rl/255*100) + "%";
    $("#d-rrbar").style.width = (rr/255*100) + "%";

    const threshold = (snap.config && snap.config.performance_warning_ms) || 180;
    const lagging = lastRequestMs >= threshold || (snap.last_packet_ms >= threshold);
    $("#perf-warning").classList.toggle("show",
      !!(snap.config && snap.config.performance_warning_enabled) && lagging);

    renderEvents(snap);
    renderButtonHistory(snap.button_events);
  }catch(e){
    $("#hdot").className = "dot warn";
    $("#hstatus").textContent = "Dashboard offline";
  }
  setTimeout(pollState, REFRESH);
}

/* ---- logs ---- */
function fillSelect(sel, items, allLabel){
  sel.innerHTML = "";
  const opt = (v,t) => { const o=document.createElement("option"); o.value=v; o.textContent=t; sel.appendChild(o); };
  opt("ALL", allLabel);
  items.forEach(i => opt(i, i));
}

async function loadLogs(){
  const lvl = $("#l-level").value, mod = $("#l-module").value,
        q   = $("#l-search").value;
  try{
    const data = await api(`/api/logs?level=${encodeURIComponent(lvl)}&module=${encodeURIComponent(mod)}&search=${encodeURIComponent(q)}`);
    const out = data.lines.map(l => {
      const t = l.ts ? `<span class="lg-time">${l.ts}</span>` : "";
      const lv = l.level ? ` <span class="lg-${l.level}">[${l.level}]</span>` : "";
      const nm = l.name ? ` <span class="lg-name">${l.name}:</span>` : "";
      return t + lv + nm + " " + escapeHtml(l.msg);
    }).join("\n");
    const el = $("#l-out");
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.innerHTML = out || '<span class="sub">No matching log lines.</span>';
    if (atBottom) el.scrollTop = el.scrollHeight;
    $("#l-stat").textContent = `${data.count} lines · ${data.size_kb} KB`;
  }catch(e){ toast("Log load failed: " + e.message, true); }
}

function escapeHtml(s){
  return (s||"").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
}

async function copyLogs(){
  const el = $("#l-out");
  try{ await navigator.clipboard.writeText(el.innerText); toast("Log copied."); }
  catch(e){ toast("Copy failed", true); }
}

async function clearLogs(){
  if(!confirm("Delete the log file contents?")) return;
  try{ await api("/api/logs/clear", {method:"POST"}); loadLogs(); toast("Log cleared."); }
  catch(e){ toast(e.message, true); }
}

/* ---- settings ---- */
let SCHEMA = [];
function buildSettings(values){
  const groups = {};
  SCHEMA.forEach(f => (groups[f.section] = groups[f.section] || []).push(f));
  const titles = {app:"Application", webui:"Web UI", controller:"Controller",
                  tray:"Tray", network:"Network"};
  let html = "";
  for (const sec of Object.keys(groups)){
    html += `<div class="section">${titles[sec] || sec}</div>`;
    for (const f of groups[sec]){
      const v = values[f.name];
      let ctl = "";
      if (f.kind === "bool"){
        ctl = `<input type="checkbox" data-name="${f.name}" ${v ? "checked" : ""}>`;
      } else if (f.kind === "choice"){
        ctl = `<select data-name="${f.name}">` +
          f.choices.map(c => `<option ${c===v?"selected":""}>${c}</option>`).join("") +
          `</select>`;
      } else if (f.kind === "int" || f.kind === "float"){
        const step = f.step || (f.kind === "int" ? 1 : 0.1);
        ctl = `<input type="number" data-name="${f.name}" value="${v}"
                 step="${step}" ${f.min!=null?`min="${f.min}"`:""} ${f.max!=null?`max="${f.max}"`:""}>`;
      } else {
        ctl = `<input type="text" data-name="${f.name}" value="${String(v??"")}">`;
      }
      const restart = f.restart ? ' <span class="tag">restart</span>' : "";
      html += `<div class="field">
          <div class="meta"><div class="lbl">${f.label}${restart}</div>
          <div class="hint">${f.help||""}</div></div>
          <div class="ctl">${ctl}</div></div>`;
    }
  }
  $("#s-form").innerHTML = html;
}

async function loadSettings(){
  const data = await api("/api/config");
  SCHEMA = data.schema;
  buildSettings(data.values);
}

function collectSettings(){
  const out = {};
  $("#s-form").querySelectorAll("[data-name]").forEach(el => {
    const name = el.dataset.name;
    if (el.type === "checkbox") out[name] = el.checked;
    else if (el.type === "number") out[name] = el.value === "" ? 0 : Number(el.value);
    else out[name] = el.value;
  });
  return out;
}

async function saveSettings(){
  try{
    const res = await api("/api/config", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({values: collectSettings()})
    });
    let msg = "Settings saved.";
    if (res.restart_required && res.restart_required.length)
      msg += " Restart required: " + res.restart_required.join(", ") + ".";
    toast(msg);
    await loadSettings();
  }catch(e){ toast(e.message, true); }
}

async function resetSettings(){
  if(!confirm("Reset every setting to its default value?")) return;
  try{
    const res = await api("/api/config/reset", {method:"POST"});
    toast("Defaults restored." + (res.restart_required?.length ? " Restart required." : ""));
    await loadSettings();
  }catch(e){ toast(e.message, true); }
}

/* ---- actions ---- */
async function act(name){
  try{
    const res = await api("/api/action", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:name})
    });
    toast(res.message || "Done.");
  }catch(e){ toast(e.message, true); }
}
async function quit(){
  if(!confirm("Quit Xbox Bridge? The controller will stop working.")) return;
  try{ await act("quit"); }catch(e){}
  setTimeout(() => { document.body.innerHTML =
    '<div style="padding:40px;font:16px Segoe UI;color:#e4e4e4">Xbox Bridge has shut down. You can close this tab.</div>'; }, 400);
}

/* ---- boot ---- */
(async function(){
  try{
    BOOT = await api("/api/bootstrap");
    $("#hver").textContent = "v" + BOOT.version;
    $("#d-listen").textContent = BOOT.listen_addr || "—";
    $("#a-ver").textContent = BOOT.version;
    $("#a-url").textContent = BOOT.url;
    $("#a-listen").textContent = BOOT.listen_addr;
    $("#a-config").textContent = BOOT.config_path;
    $("#a-log").textContent = BOOT.log_path;
    $("#a-install").textContent = BOOT.install_dir;
    fillSelect($("#l-level"), BOOT.levels, "ALL LEVELS");
    fillSelect($("#l-module"), BOOT.modules, "ALL MODULES");
    SCHEMA = BOOT.schema;
    await loadSettings();
  }catch(e){ toast("Bootstrap failed: " + e.message, true); }
  pollState();
  setInterval(() => { if($("#l-auto").checked && $("#tab-logs").classList.contains("active")) loadLogs(); }, 2000);
})();
</script>
</body>
</html>
"""
