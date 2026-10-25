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
import urllib.error
import urllib.request

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


VERSION = "1.2.0"

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
            raw_limit = request.args.get("limit", "1500")
            try:
                limit = min(max(int(raw_limit), 1), 8000)
            except (TypeError, ValueError):
                return jsonify({"ok": False,
                                "error": "limit must be an integer between 1 and 8000."}), 400

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

        @app.get("/api/linux/status")
        def linux_status():
            return jsonify(self._linux_request("/api/status"))

        @app.get("/api/linux/logs")
        def linux_logs():
            return jsonify(self._linux_request("/api/logs"))

        @app.post("/api/linux/config")
        def linux_config():
            return jsonify(self._linux_request("/api/config", request.get_json(silent=True) or {}, "POST"))

        @app.post("/api/linux/restart")
        def linux_restart():
            return jsonify(self._linux_request("/api/restart", {}, "POST"))

        @app.post("/api/linux/rumble/test")
        def linux_rumble_test():
            return jsonify(self._linux_request("/api/rumble/test", {}, "POST"))

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

    def _linux_request(self, path: str, payload: dict | None = None,
                       method: str = "GET") -> dict:
        """Proxy a management request to the Linux peer shown by the bridge."""
        peer = self._state.snapshot().get("peer_ip", "")
        port = self._config.get_int("linux.management_port")
        if not peer:
            return {"ok": False, "error": "Linux peer is not connected."}
        url = f"http://{peer}:{port}{path}"
        data = json.dumps(payload).encode() if method == "POST" else None
        req = urllib.request.Request(url, data=data, method=method,
                   headers={"Content-Type": "application/json"})
        token = self._config.get_str("linux.management_token")
        if token:
            req.add_header("X-Bridge-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, ValueError) as exc:
            logger.warning("Linux management request %s failed: %s", path, exc)
            return {"ok": False, "error": str(exc)}


_ACTION_MESSAGES = {
    "reconnect":        "TCP listener restarted.",
    "reset_controller": "Virtual controller reset.",
    "open_folder":      "Folder opened.",
    "open_logs":        "Log folder opened.",
    "quit":             "Shutting down…",
    "test_rumble":      "Test pulse sent. You should feel both motors.",
    "stop_rumble":      "Motors stopped.",
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
  :root{
    color-scheme:dark;
    --ground:#17181c;--surface:#121317;--raised:#1e2025;--raised2:#26282e;
    --border:#2a2c32;--rule:#222429;--text:#f0f2f3;--muted:#9a9da5;--faint:#858891;
    --accent:#7ed99d;--accent-weak:rgba(126,217,157,.12);
    --warning:#e0b15c;--negative:#ee818a;--info:#8fb8d9;
    --radius:6px;--gap:16px;
    --font:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif;
    --mono:"Cascadia Mono","Cascadia Code",Consolas,"SFMono-Regular",Menlo,monospace;
  }
  *{box-sizing:border-box}
  html{scrollbar-color:#3d4047 #121317}
  body{margin:0;min-height:100vh;background:var(--ground);color:var(--text);
    font:14px/1.55 var(--font);-webkit-font-smoothing:antialiased}
  a{color:var(--accent)}
  code{font-family:var(--mono);font-size:.92em;color:var(--text)}
  ::selection{background:var(--accent-weak)}
  :focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  [hidden]{display:none!important}

  /* ---- shell ---- */
  header{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px;
    height:58px;padding:0 max(24px,calc((100vw - 1240px)/2));
    background:var(--ground);border-bottom:1px solid var(--border)}
  .brand{display:flex;align-items:center;gap:10px;min-width:0}
  .brand svg{width:25px;height:25px;color:var(--accent);flex:0 0 auto}
  .brand .logo{font-weight:600;font-size:15px;white-space:nowrap}
  .brand .scope{font-size:12px;color:var(--faint);white-space:nowrap}
  header .spacer{flex:1}
  header .pill{font-size:12px;color:var(--faint);white-space:nowrap}
  .status-pill{display:inline-flex;align-items:center;gap:7px;height:26px;padding:0 10px;
    border:1px solid var(--border);border-radius:999px;background:var(--surface);
    font-size:12px;color:var(--muted);white-space:nowrap}
  .status-pill.live{color:var(--accent);border-color:rgba(126,217,157,.38)}
  .status-pill.warn{color:var(--warning);border-color:rgba(224,177,92,.38)}
  .status-pill.off{color:var(--muted)}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--faint);flex:0 0 auto}
  .dot.on{background:var(--accent)}
  .dot.warn{background:var(--warning)}
  .dot.err{background:var(--negative)}

  nav{position:sticky;top:58px;z-index:19;display:flex;gap:2px;
    padding:0 max(24px,calc((100vw - 1240px)/2));background:var(--ground);
    border-bottom:1px solid var(--border);overflow-x:auto;scrollbar-width:none}
  nav::-webkit-scrollbar{display:none}
  nav button{background:none;border:0;border-bottom:1px solid transparent;
    color:var(--muted);padding:12px 13px 10px;font-size:13px;font-weight:500;
    white-space:nowrap;cursor:pointer;margin-bottom:-1px;
    transition:color .12s ease,border-color .12s ease}
  nav button:hover{color:var(--text)}
  nav button.active{color:var(--text);border-bottom-color:var(--accent)}

  main{max-width:1240px;margin:0 auto;padding:28px 24px 56px}
  .tab{display:none}
  .tab.active{display:block}

  .page-head{display:flex;align-items:flex-end;justify-content:space-between;
    gap:24px;margin:0 0 20px}
  .page-title{min-width:0}
  .page-head h1{margin:0;font-size:26px;font-weight:600;line-height:1.15;letter-spacing:-.02em}
  .page-head p{margin:7px 0 0;color:var(--muted);font-size:13px;max-width:72ch}
  .page-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap;justify-content:flex-end}
  .fresh{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--muted);
    font-variant-numeric:tabular-nums;white-space:nowrap}

  /* ---- surfaces ---- */
  .card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px}
  .card-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:14px}
  .card h2,.card h3{margin:0;font-size:15px;font-weight:600;color:var(--text)}
  .card .hint{font-size:12px;color:var(--muted);margin:0}
  .grid{display:grid;gap:var(--gap);grid-template-columns:repeat(2,minmax(0,1fr))}
  .grid3{display:grid;gap:var(--gap);grid-template-columns:repeat(3,minmax(0,1fr))}
  .dash-main{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);
    gap:var(--gap);margin-top:var(--gap)}
  .dash-detail{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,2fr);
    gap:var(--gap);margin-top:var(--gap)}
  .dash-detail > .card{display:flex;flex-direction:column;min-height:0}
  .dash-detail .ev{flex:1;height:auto;min-height:200px;max-height:340px}
  .stack{display:flex;flex-direction:column;gap:var(--gap)}

  /* ---- signal path (the product's own diagram) ---- */
  .signal-path{display:grid;grid-template-columns:minmax(0,1fr) 44px minmax(0,1fr) 44px minmax(0,1fr) 44px minmax(0,1fr);
    background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
    overflow:hidden;margin-bottom:var(--gap)}
  .sp-node{padding:15px 18px;min-width:0}
  .sp-controller{border-left:0}
  .sp-linux,.sp-network,.sp-pad{border-left:1px solid var(--border)}
  .sp-node .label{font-size:12px;color:var(--muted);margin-bottom:7px}
  .sp-node .value{font:600 16px/1.2 var(--font);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .sp-node .value.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
  .sp-node .sub{font-size:12px;color:var(--muted);margin-top:4px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .sp-link{position:relative;display:block;background:var(--surface)}
  .sp-link:before{content:"";position:absolute;left:0;right:0;top:50%;height:1px;background:var(--border)}
  .sp-link:after{content:"";position:absolute;right:8px;top:50%;width:5px;height:5px;
    border-top:1px solid var(--faint);border-right:1px solid var(--faint);
    transform:translateY(-50%) rotate(45deg)}
  .sp-link.on:before{height:2px;background:var(--accent);
    animation:signal-flow 1.8s ease-in-out infinite}
  .sp-link.on:after{border-color:var(--accent);
    animation:signal-flow 1.8s ease-in-out infinite}
  @keyframes signal-flow{0%,100%{opacity:.4}50%{opacity:1}}
  .sp-summary{display:none;margin:10px 0 0;font-size:12px;color:var(--muted);
    font-variant-numeric:tabular-nums}

  /* ---- figures, rows, bars ---- */
  .kv{display:flex;justify-content:space-between;gap:14px;padding:8px 0;border-bottom:1px solid var(--rule)}
  .kv:last-child{border-bottom:0}
  .kv .k{color:var(--muted);font-size:13px;flex:0 0 auto}
  .kv .v{font-family:var(--mono);font-size:13px;text-align:right;overflow-wrap:anywhere;
    font-variant-numeric:tabular-nums}
  .metric{font:600 18px/1.2 var(--mono);font-variant-numeric:tabular-nums}
  .metric.small{font-size:14px}
  .perf-strip{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;
    margin-top:14px;padding-top:14px;border-top:1px solid var(--rule)}
  .card-foot{display:flex;align-items:center;justify-content:space-between;gap:10px;
    flex-wrap:wrap;margin-top:14px}
  .sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
    clip:rect(0,0,0,0);white-space:nowrap;border:0}
  .sub{color:var(--muted);font-size:12px}
  .meter{height:6px;border-radius:999px;background:var(--raised);overflow:hidden;margin-top:6px}
  .meter>i{display:block;height:100%;width:0;background:var(--accent);
    transition:width .12s linear,background .12s linear}
  .meter.sm{height:5px}
  .meter.tall{height:8px}
  .meter-row{display:flex;justify-content:space-between;gap:12px;font-size:13px;color:var(--muted)}
  .meter-row b{font-family:var(--mono);font-weight:500;color:var(--text);font-variant-numeric:tabular-nums}
  .tag{display:inline-flex;align-items:center;height:22px;padding:0 8px;
    border:1px solid var(--border);border-radius:999px;font-size:11px;
    color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
  .tag.warn{color:var(--warning);border-color:rgba(224,177,92,.38)}
  .head-tags{display:inline-flex;align-items:center;justify-content:flex-end;
    gap:6px;flex-wrap:wrap}

  /* ---- live input ---- */
  .input-monitor{display:grid;grid-template-columns:112px 112px minmax(0,1fr);
    gap:22px;align-items:start}
  .stick{width:112px;min-width:0}
  .stick-pad{position:relative;width:112px;height:112px;border:1px solid var(--border);
    border-radius:var(--radius);background:var(--raised);overflow:hidden}
  .stick-pad:before,.stick-pad:after{content:"";position:absolute;background:var(--rule)}
  .stick-pad:before{left:50%;top:12%;bottom:12%;width:1px}
  .stick-pad:after{top:50%;left:12%;right:12%;height:1px}
  .stick-ring{position:absolute;left:50%;top:50%;width:30px;height:30px;
    border:1px solid var(--rule);border-radius:50%;transform:translate(-50%,-50%)}
  .stick-dot{position:absolute;left:50%;top:50%;width:10px;height:10px;border-radius:50%;
    background:var(--accent);transform:translate(-50%,-50%);
    outline:3px solid var(--accent-weak);
    transition:left .08s linear,top .08s linear}
  .stick-label{display:flex;justify-content:space-between;gap:8px;margin-top:8px;
    font-size:12px;color:var(--muted)}
  .stick-label b{font-family:var(--mono);font-weight:500;color:var(--text);
    font-variant-numeric:tabular-nums}
  .triggers{display:flex;flex-direction:column;gap:14px;min-width:0;padding-top:2px}
  .trigger-block .meter-row{margin-bottom:2px}
  .input-buttons{margin-top:18px;padding-top:14px;border-top:1px solid var(--rule)}
  .chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
  .chip{font:500 11px/1 var(--mono);padding:6px 7px;border:1px solid var(--border);
    border-radius:4px;color:var(--faint);background:transparent;
    transition:color .1s ease,border-color .1s ease,background .1s ease}
  .chip.on{color:var(--text);border-color:var(--accent);background:var(--accent-weak)}
  .raw-bits{margin-top:10px;font:12px var(--mono);color:var(--faint);
    font-variant-numeric:tabular-nums}

  /* ---- controls ---- */
  .btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;
    height:34px;padding:0 14px;border:1px solid var(--border);border-radius:5px;
    background:var(--raised);color:var(--text);font-size:13px;font-weight:500;
    cursor:pointer;white-space:nowrap;
    transition:background .12s ease,border-color .12s ease,color .12s ease}
  .btn:hover{background:var(--raised2);border-color:#3b3e45}
  .btn.primary{background:var(--text);border-color:var(--text);color:#111216;border-radius:999px;padding:0 16px}
  .btn.primary:hover{background:#dfe2e5;border-color:#dfe2e5}
  .btn.danger{color:var(--negative);border-color:#4a2d31}
  .btn.danger:hover{background:#2a1b1e}
  .btn:disabled{opacity:.45;cursor:not-allowed}
  .btn.small{height:30px;padding:0 11px;font-size:12px}
  select,input[type=text],input[type=number]{height:34px;background:var(--raised);color:var(--text);
    border:1px solid var(--border);border-radius:4px;padding:0 10px;font:13px var(--font);outline:none}
  select:focus,input[type=text]:focus,input[type=number]:focus{border-color:var(--accent)}
  input::placeholder{color:var(--faint)}
  .switch-row{display:inline-flex;align-items:center;gap:8px;min-height:28px;
    cursor:pointer;user-select:none;font-size:12px;color:var(--text)}
  .switch-row input{position:absolute;opacity:0;width:1px;height:1px}
  .switch{position:relative;width:30px;height:17px;flex:0 0 auto;border-radius:999px;
    background:var(--raised);border:1px solid #3b3e45;transition:background .12s ease,border-color .12s ease}
  .switch:after{content:"";position:absolute;top:2px;left:2px;width:11px;height:11px;
    border-radius:50%;background:var(--muted);transition:left .12s ease,background .12s ease}
  .switch-row input:checked+.switch{background:var(--accent-weak);border-color:var(--accent)}
  .switch-row input:checked+.switch:after{left:15px;background:var(--accent)}
  .switch-row input:focus-visible+.switch{outline:2px solid var(--accent);outline-offset:2px}
  .save-state{font-size:12px;color:var(--muted)}

  /* ---- warnings, toolbar, logs ---- */
  .perf-warning{display:none;align-items:flex-start;gap:10px;margin:0 0 var(--gap);
    padding:11px 13px;border:1px solid #4a3b22;border-radius:var(--radius);
    background:#211b11;color:#e7c78c;font-size:12px}
  .perf-warning.show{display:flex}
  .perf-warning:before{content:"";width:7px;height:7px;margin-top:5px;border-radius:50%;
    background:var(--warning);flex:0 0 auto}
  .perf-warning strong{color:#f0d9ac;font-weight:600}
  .toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
  .toolbar .spacer{flex:1}
  .ev{height:240px;overflow:auto;background:#0d0e11;color:#c7cad0;
    border:1px solid var(--rule);border-radius:4px;padding:12px;
    font:12px/1.75 var(--mono);white-space:pre-wrap}
  .ev.tall{height:620px}
  .ev .t{color:var(--faint)}
  pre.log{background:#0d0e11;color:#d5d8dc;border:1px solid var(--rule);border-radius:4px;
    padding:14px;margin:0;height:500px;overflow:auto;
    font:12px/1.65 var(--mono);white-space:pre-wrap}
  .lg-time{color:var(--faint)}
  .lg-name{color:var(--info)}
  .lg-DEBUG{color:var(--faint)}
  .lg-INFO{color:var(--info)}
  .lg-WARNING{color:var(--warning)}
  .lg-ERROR{color:var(--negative)}
  .lg-CRITICAL{color:var(--negative);font-weight:700}
  .button-history{max-height:360px;overflow:auto;font:12px/1.75 var(--mono)}
  .button-history .pressed{color:var(--accent);font-weight:500}
  .button-history .released{color:var(--muted);font-weight:500}

  /* ---- forms + settings ---- */
  .field{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:24px;
    align-items:center;padding:12px 0;border-bottom:1px solid var(--rule)}
  .field:last-child{border-bottom:0}
  .field .meta{min-width:0}
  .field .lbl{font-size:13px;font-weight:500}
  .field .hint{color:var(--muted);font-size:12px;margin-top:2px;max-width:64ch}
  .field .ctl{flex:0 0 auto}
  .field .ctl input[type=number],.field .ctl input[type=text]{width:170px}
  .field .ctl select{width:190px}
  .settings-group{display:grid;grid-template-columns:220px minmax(0,1fr);gap:24px;
    padding:22px 0;border-top:1px solid var(--border)}
  .settings-group:first-child{border-top:0;padding-top:0}
  .settings-group h2{margin:0;font-size:14px;font-weight:600}
  .settings-group .group-desc{margin:6px 0 0;font-size:12px;color:var(--muted);line-height:1.5}
  .settings-group .group-note{margin:12px 0 0;font-size:12px;color:var(--faint);line-height:1.6}
  .settings-panel{background:var(--surface);border:1px solid var(--border);
    border-radius:var(--radius);padding:2px 16px}
  #tab-settings{padding-bottom:56px}
  .savebar{position:sticky;bottom:0;z-index:10;display:flex;gap:8px;flex-wrap:wrap;
    align-items:center;padding:14px 0 0;margin-top:18px;background:var(--ground);
    border-top:1px solid var(--border)}
  .savebar .spacer{flex:1}
  .about-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 34px}
  .about-card{max-width:1000px}

  .toast{position:fixed;right:20px;bottom:20px;z-index:60;max-width:380px;
    background:var(--raised);color:var(--text);border:1px solid var(--border);
    border-radius:var(--radius);padding:12px 16px;box-shadow:0 12px 28px rgba(0,0,0,.35);
    transform:translateY(8px);opacity:0;transition:opacity .16s ease,transform .16s ease;
    pointer-events:none;font-size:13px}
  .toast.show{transform:none;opacity:1}
  .toast.err{border-color:#5a3338;color:#f3b6bb}
  .footerbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:16px}
  .footerbar .spacer{flex:1}

  @media(max-width:980px){
    .dash-main{grid-template-columns:1fr}
    .dash-detail{grid-template-columns:1fr}
    .grid3{grid-template-columns:1fr 1fr}
    .settings-group{grid-template-columns:180px minmax(0,1fr)}
  }
  @media(max-width:900px){
    .signal-path{grid-template-columns:1fr 1fr}
    .sp-node{padding:13px 14px}
    .sp-controller,.sp-linux{border-bottom:1px solid var(--border)}
    .sp-controller{border-left:0}
    .sp-linux{border-left:1px solid var(--border)}
    .sp-network{border-left:0}
    .sp-pad{border-left:1px solid var(--border)}
    .sp-link{display:none}
    .sp-summary{display:block}
  }
  @media(max-width:760px){
    header{padding:0 16px}
    .brand .scope{display:none}
    .brand svg{width:22px;height:22px}
    nav{padding:0 10px}
    main{padding:22px 16px 42px}
    .page-head{align-items:flex-start;flex-direction:column;gap:13px}
    .page-actions{justify-content:flex-start;width:100%}
    .page-head h1{font-size:23px}
    .grid,.grid3,.dash-detail{grid-template-columns:1fr}
    .about-grid{grid-template-columns:1fr}
    .settings-group{grid-template-columns:1fr;gap:10px;padding:20px 0}
    .settings-panel{padding:0 14px}
    .field{grid-template-columns:1fr;gap:8px}
    .field .ctl{width:100%}
    .field .ctl input[type=number],.field .ctl input[type=text],
    .field .ctl select{width:100%}
    .toolbar .spacer{display:none}
    .savebar .spacer{display:none}
    .footerbar .spacer{display:none}
    .btn{height:38px}
    .btn.small{height:34px}
    .ev.tall{height:56vh;min-height:300px}
    pre.log{height:52vh;min-height:260px}
    #linux-log{height:52vh!important;min-height:260px}
  }
  @media(max-width:680px){
    nav{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));
      overflow:visible;padding:0 8px;gap:0}
    nav button{border-bottom:0;border-radius:4px;margin:3px 2px;padding:9px 4px;font-size:12px}
    nav button.active{background:var(--raised);color:var(--text)}
  }
  @media(max-width:560px){
    header .pill{display:none}
    #hstatus{max-width:44vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .input-monitor{grid-template-columns:1fr 1fr}
    .triggers{grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr;gap:14px}
    .stick{width:100%}
    .stick-pad{width:100%;height:auto;aspect-ratio:1}
    .card{padding:16px}
  }
  @media(prefers-reduced-motion:reduce){
    *,*:before,*:after{transition:none!important;animation:none!important}
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M3.8 9.2h4.7l3.5 6.1 3.5-6.1h4.7" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="3.8" cy="9.2" r="2.1" fill="currentColor"/>
      <circle cx="20.2" cy="9.2" r="2.1" fill="currentColor"/>
    </svg>
    <span class="logo">Xbox Bridge</span>
    <span class="scope">Controller relay</span>
  </div>
  <span class="spacer"></span>
  <span class="status-pill"><span class="dot" id="hdot"></span><span id="hstatus">Connecting…</span></span>
  <span class="pill" id="hver"></span>
</header>

<nav aria-label="Sections">
  <button data-tab="dash" class="active" aria-current="page">Overview</button>
  <button data-tab="events">Events</button>
  <button data-tab="logs">Logs</button>
  <button data-tab="linux">Linux container</button>
  <button data-tab="settings">Settings</button>
  <button data-tab="about">About</button>
</nav>

<main>
  <!-- OVERVIEW -->
  <section class="tab active" id="tab-dash">
    <div class="page-head">
      <div class="page-title">
        <h1>Bridge overview</h1>
        <p id="d-finding">Checking the link between your controller and this PC…</p>
      </div>
      <div class="page-actions">
        <span class="fresh"><span class="dot" id="d-live-dot"></span><span id="d-status">Live</span> · <span id="d-fresh">—</span></span>
        <span class="save-state" id="quick-status" role="status" aria-live="polite"></span>
        <button class="btn primary" onclick="act('reconnect')">Reconnect</button>
      </div>
    </div>

    <div class="perf-warning" id="perf-warning">
      <div><strong>Network performance is degraded.</strong>
        Requests or packet timing are slow. Turning off vibration reduces feedback traffic; check the Wi-Fi link between the machines.</div>
    </div>

    <div class="signal-path" role="group" aria-label="Live signal path">
      <div class="sp-node sp-controller">
        <div class="label">Controller</div>
        <div class="value" id="sp-name">No controller</div>
        <div class="sub" id="sp-mac">Bluetooth input</div>
      </div>
      <span class="sp-link" id="sp-c1" aria-hidden="true"></span>
      <div class="sp-node sp-linux">
        <div class="label">Linux bridge</div>
        <div class="value mono" id="sp-peer">Waiting</div>
        <div class="sub" id="sp-link">Not connected</div>
      </div>
      <span class="sp-link" id="sp-c2" aria-hidden="true"></span>
      <div class="sp-node sp-network">
        <div class="label">Network</div>
        <div class="value mono" id="sp-rate">—</div>
        <div class="sub" id="sp-interval">No packets yet</div>
      </div>
      <span class="sp-link" id="sp-c3" aria-hidden="true"></span>
      <div class="sp-node sp-pad">
        <div class="label">Virtual pad</div>
        <div class="value">Xbox 360</div>
        <div class="sub" id="sp-pad">Waiting for input</div>
      </div>
    </div>
    <p class="sp-summary" id="sp-summary">—</p>

    <div class="dash-main">
      <div class="card">
        <div class="card-head">
          <h2>Live input</h2>
          <span class="tag" id="d-input-state">No packets</span>
        </div>
        <div class="input-monitor">
          <div class="stick">
            <div class="stick-pad"><span class="stick-ring" aria-hidden="true"></span><span class="stick-dot" id="stick-l-dot"></span></div>
            <div class="stick-label"><span>Left</span><b><span id="d-lx">32768</span>, <span id="d-ly">32768</span></b></div>
          </div>
          <div class="stick">
            <div class="stick-pad"><span class="stick-ring" aria-hidden="true"></span><span class="stick-dot" id="stick-r-dot"></span></div>
            <div class="stick-label"><span>Right</span><b><span id="d-rx">32768</span>, <span id="d-ry">32768</span></b></div>
          </div>
          <div class="triggers">
            <div class="trigger-block">
              <div class="meter-row"><span>Brake (LT)</span><b id="d-lt">0</b></div>
              <div class="meter sm"><i id="d-ltbar"></i></div>
            </div>
            <div class="trigger-block">
              <div class="meter-row"><span>Throttle (RT)</span><b id="d-rt">0</b></div>
              <div class="meter sm"><i id="d-rtbar"></i></div>
            </div>
          </div>
        </div>
        <div class="input-buttons">
          <div class="meter-row"><span>Pressed buttons</span><b id="d-buttons">0x00 / 0x00</b></div>
          <div class="chips" id="d-btnchips" aria-label="Pressed buttons">
            <span class="chip" data-btn="low:0">A</span>
            <span class="chip" data-btn="low:1">B</span>
            <span class="chip" data-btn="low:2">X</span>
            <span class="chip" data-btn="low:3">Y</span>
            <span class="chip" data-btn="low:4">LB</span>
            <span class="chip" data-btn="low:5">RB</span>
            <span class="chip" data-btn="low:6">Back</span>
            <span class="chip" data-btn="low:7">Start</span>
            <span class="chip" data-btn="high:0">L3</span>
            <span class="chip" data-btn="high:1">R3</span>
            <span class="chip" data-btn="high:2">Guide</span>
            <span class="chip" data-btn="dpad:1">D-pad up</span>
            <span class="chip" data-btn="dpad:2">D-pad right</span>
            <span class="chip" data-btn="dpad:4">D-pad down</span>
            <span class="chip" data-btn="dpad:8">D-pad left</span>
          </div>
          <span class="sr-only" id="d-pressed" aria-live="polite"></span>
          <p class="hint" style="margin-top:12px">These are the exact axes and buttons sent to the virtual pad.</p>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h2>Controller</h2>
          <span class="head-tags">
            <span class="tag" id="d-batstate">Unknown</span>
            <span class="tag" id="d-feedback-tag">Feedback</span>
          </span>
        </div>
        <div class="kv"><span class="k">Name</span><span class="v" id="d-name">—</span></div>
        <div class="kv"><span class="k">MAC</span><span class="v" id="d-mac">—</span></div>
        <div id="d-battery">
          <div class="meter-row" style="margin-top:12px"><span>Battery</span><b id="d-bat">—</b></div>
          <div class="meter"><i id="d-batbar"></i></div>
        </div>
        <div class="meter-row" style="margin-top:16px"><span>Left motor</span><b id="d-rl">0</b></div>
        <div class="meter tall"><i id="d-rlbar"></i></div>
        <div class="meter-row" style="margin-top:12px"><span>Right motor</span><b id="d-rr">0</b></div>
        <div class="meter tall"><i id="d-rrbar"></i></div>
        <div class="card-foot" style="margin-top:16px">
          <label class="switch-row">
            <input type="checkbox" id="quick-rumble">
            <span class="switch" aria-hidden="true"></span>
            <span>Forward vibration</span>
          </label>
          <button class="btn small" id="rumble-test" onclick="testRumble()">Test motors</button>
          <button class="btn small" onclick="act('stop_rumble')">Stop</button>
        </div>
        <label class="switch-row" style="margin-top:10px">
          <input type="checkbox" id="quick-battery">
          <span class="switch" aria-hidden="true"></span>
          <span>Show battery level</span>
        </label>
        <p class="hint" style="margin-top:10px" id="rumble-hint">A game's rumble is relayed to the controller.</p>
      </div>
    </div>

    <div class="dash-detail">
      <div class="card">
        <div class="card-head"><h2>Link</h2><span class="status-pill off" id="d-status-card">Offline</span></div>
        <div class="kv"><span class="k">Peer IP</span><span class="v" id="d-peer">—</span></div>
        <div class="kv"><span class="k">Listen</span><span class="v" id="d-listen">—</span></div>
        <div class="kv"><span class="k">Uptime</span><span class="v" id="d-uptime">—</span></div>
        <div class="kv"><span class="k">Packets</span><span class="v" id="d-packets">—</span></div>
        <div class="kv"><span class="k">Last event</span><span class="v" id="d-lastev">—</span></div>
        <div class="kv"><span class="k">Feedback</span><span class="v" id="d-feedback">Checking…</span></div>
        <div class="perf-strip">
          <div><div class="sub">Dashboard</div><div class="metric small" id="d-request">—</div></div>
          <div><div class="sub">Interval</div><div class="metric small" id="d-packet">—</div></div>
          <div><div class="sub">Rate</div><div class="metric small" id="d-rate">—</div></div>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h2>Recent events</h2><span class="tag">Newest last</span></div>
        <div class="ev" id="d-events"></div>
      </div>
    </div>

    <div class="footerbar">
      <button class="btn" onclick="act('reset_controller')">Reset controller</button>
      <button class="btn" onclick="act('open_folder')">Open app folder</button>
      <button class="btn" onclick="clearLogs()">Clear log</button>
      <span class="spacer"></span>
      <button class="btn danger" onclick="quit()">Quit app</button>
    </div>
  </section>

  <!-- EVENTS -->
  <section class="tab" id="tab-events">
    <div class="page-head">
      <div class="page-title">
        <h1>Events</h1>
        <p>Recent bridge, controller and connection events, as they happen.</p>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h2>Live event feed</h2><span class="tag">Newest last</span></div>
      <div class="ev tall" id="e-events"></div>
    </div>
  </section>

  <!-- LOGS -->
  <section class="tab" id="tab-logs">
    <div class="page-head">
      <div class="page-title">
        <h1>Logs</h1>
        <p>The Windows-side log file, filtered live.</p>
      </div>
    </div>
    <div class="card">
      <div class="toolbar">
        <select id="l-level" aria-label="Filter by log level"></select>
        <select id="l-module" aria-label="Filter by module"></select>
        <input type="text" id="l-search" placeholder="Search log lines…" aria-label="Search log lines" style="min-width:220px">
        <label class="switch-row"><input type="checkbox" id="l-auto" checked>
          <span class="switch" aria-hidden="true"></span><span>Auto-refresh</span></label>
        <button class="btn small" onclick="loadLogs()">Refresh</button>
        <button class="btn small" onclick="copyLogs()">Copy</button>
        <button class="btn small danger" onclick="clearLogs()">Clear</button>
        <span class="spacer"></span>
        <span class="tag" id="l-stat">—</span>
      </div>
      <pre class="log" id="l-out"></pre>
    </div>
    <div class="card" style="margin-top:16px">
      <div class="card-head"><h2>Button history</h2></div>
      <div class="button-history" id="l-buttons"><span class="sub">No button transitions yet.</span></div>
    </div>
  </section>

  <!-- LINUX CONTAINER -->
  <section class="tab" id="tab-linux">
    <div class="page-head">
      <div class="page-title">
        <h1>Linux container</h1>
        <p>Inspect the Linux bridge, read its logs, change connection settings, or restart it.</p>
      </div>
      <span class="status-pill off" id="linux-status">Offline</span>
    </div>
    <div class="grid">
      <div class="card">
        <div class="card-head"><h2>Container status</h2></div>
        <div class="kv"><span class="k">API</span><span class="v" id="linux-api">—</span></div>
        <div class="kv"><span class="k">PID</span><span class="v" id="linux-pid">—</span></div>
        <div class="kv"><span class="k">Uptime</span><span class="v" id="linux-uptime">—</span></div>
        <div class="kv"><span class="k">Controller device</span><span class="v" id="linux-device">—</span></div>
        <div class="kv"><span class="k">Rumble backend</span><span class="v" id="linux-rumble-backend">—</span></div>
        <div class="kv"><span class="k">Rumble device</span><span class="v" id="linux-rumble-path">—</span></div>
        <div class="kv"><span class="k">Rumble state</span><span class="v" id="linux-rumble-state">—</span></div>
        <p class="hint" id="linux-rumble-error" hidden></p>
        <div class="card-foot">
          <button class="btn small" onclick="loadLinux()">Refresh</button>
          <button class="btn small" id="linux-rumble-test" onclick="testLinuxRumble()">Test on Linux</button>
          <button class="btn small" onclick="restartLinux()">Restart container</button>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h2>Linux settings</h2></div>
        <div id="linux-form"></div>
        <div class="card-foot">
          <button class="btn primary small" onclick="saveLinux()">Save settings</button>
        </div>
        <p class="hint" style="margin-top:10px">Saved to the container's <code>.env</code> file. Restart the container to apply changes.</p>
      </div>
    </div>
    <div class="card" style="margin-top:16px">
      <div class="card-head"><h2>Linux logs</h2>
        <span class="head-tags">
          <button class="btn small" onclick="copyLinuxDiagnostics()">Copy diagnostics</button>
          <button class="btn small" onclick="loadLinuxLogs()">Refresh</button>
        </span>
      </div>
      <pre class="log" id="linux-log" style="height:440px"></pre>
    </div>
  </section>

  <!-- SETTINGS -->
  <section class="tab" id="tab-settings">
    <div class="page-head">
      <div class="page-title">
        <h1>Settings</h1>
        <p>Preferences for this PC. Fields marked restart apply after the app is restarted.</p>
      </div>
      <span class="status-pill" id="settings-state">Ready</span>
    </div>
    <div id="s-form"></div>
    <div class="savebar">
      <button class="btn primary" onclick="saveSettings()">Save changes</button>
      <button class="btn" onclick="loadSettings()">Reload</button>
      <span class="spacer"></span>
      <button class="btn danger" onclick="resetSettings()">Reset to defaults</button>
    </div>
  </section>

  <!-- ABOUT -->
  <section class="tab" id="tab-about">
    <div class="page-head">
      <div class="page-title">
        <h1>About</h1>
        <p>Build information and the files this app uses.</p>
      </div>
    </div>
    <div class="card about-card">
      <p class="hint" style="margin:0 0 10px;max-width:72ch">Xbox Bridge forwards controller input over TCP and returns game rumble to the physical controller. These are the files and addresses this build uses.</p>
      <div class="about-grid">
        <div class="kv"><span class="k">Version</span><span class="v" id="a-ver">—</span></div>
        <div class="kv"><span class="k">Dashboard URL</span><span class="v" id="a-url">—</span></div>
        <div class="kv"><span class="k">Listen address</span><span class="v" id="a-listen">—</span></div>
        <div class="kv"><span class="k">Config file</span><span class="v" id="a-config">—</span></div>
        <div class="kv"><span class="k">Log file</span><span class="v" id="a-log">—</span></div>
        <div class="kv"><span class="k">Install dir</span><span class="v" id="a-install">—</span></div>
      </div>
    </div>
  </section>
</main>

<div class="toast" id="toast" role="status" aria-live="polite"></div>

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
  document.querySelectorAll("nav button").forEach(x => {
    x.classList.remove("active");
    x.removeAttribute("aria-current");
  });
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  b.setAttribute("aria-current", "page");
  $("#tab-" + b.dataset.tab).classList.add("active");
  if (b.dataset.tab === "logs") loadLogs();
  if (b.dataset.tab === "linux") { loadLinux(); loadLinuxLogs(); }
});

function fmtUptime(s){
  s = Math.max(0, s|0);
  const h = (s/3600)|0, m = ((s%3600)/60)|0, sec = s%60;
  const p = n => String(n).padStart(2,"0");
  return h ? `${p(h)}:${p(m)}:${p(sec)}` : `${p(m)}:${p(sec)}`;
}

function battColor(b, low){
  if (b == null || b < 0) return "var(--faint)";
  if (b <= (low || 15)) return "var(--negative)";
  if (b <= 30) return "var(--warning)";
  return "var(--accent)";
}

function rumbleBackendLabel(backend){
  if (backend === "force feedback") return "Force feedback";
  if (backend === "hidraw") return "Hidraw";
  if (!backend || backend === "none") return "None";
  return backend;
}

function setStick(sel, x, y){
  const dot = $(sel);
  if (!dot) return;
  const px = Math.max(0, Math.min(65535, x|0));
  const py = Math.max(0, Math.min(65535, y|0));
  dot.style.left = (6 + (px / 65535) * 88) + "%";
  dot.style.top  = (6 + (py / 65535) * 88) + "%";
}

function updateButtonChips(low, high, dpad){
  const pressed = [];
  document.querySelectorAll("#d-btnchips .chip").forEach(chip => {
    const parts = (chip.dataset.btn || "").split(":");
    const kind = parts[0], bit = Number(parts[1]);
    let on = false;
    if (kind === "low") on = !!(low & (1 << bit));
    else if (kind === "high") on = !!(high & (1 << bit));
    else if (kind === "dpad") on = !!(dpad & bit);
    chip.classList.toggle("on", on);
    if (on) pressed.push(chip.textContent.trim());
  });
  const live = $("#d-pressed");
  if (live) live.textContent = pressed.length
    ? "Pressed: " + pressed.join(", ") : "No buttons pressed";
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

/* Quick performance toggles save immediately.  While a change is in
   flight the poller must not overwrite the checkbox with the previous
   server value — that was the "toggle snaps back" bug. */
const quickDirty = new Set();
let quickTimer = null;
let quickSaving = false;
let quickSaveQueued = false;

function setBatteryVisible(visible){
  const el = $("#d-battery");
  if (el) el.hidden = !visible;
  const state = $("#d-batstate");
  if (state) state.hidden = !visible;
}

function queueQuickSettings(){
  quickDirty.add("quick-rumble");
  quickDirty.add("quick-battery");
  setBatteryVisible($("#quick-battery").checked);
  $("#quick-status").textContent = "Saving…";
  clearTimeout(quickTimer);
  quickTimer = setTimeout(flushQuickSettings, 120);
}

async function flushQuickSettings(){
  quickTimer = null;
  if (quickSaving){
    quickSaveQueued = true;
    return;
  }
  quickSaving = true;
  const values = {
    "controller.rumble_enabled": $("#quick-rumble").checked,
    "controller.battery_display_enabled": $("#quick-battery").checked
  };
  try{
    await api("/api/config", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({values})});
    const unchanged =
      values["controller.rumble_enabled"] === $("#quick-rumble").checked &&
      values["controller.battery_display_enabled"] === $("#quick-battery").checked;
    if (unchanged) quickDirty.clear();
    else quickSaveQueued = true;
    setBatteryVisible(values["controller.battery_display_enabled"]);
    $("#quick-status").textContent = "Saved. Applies instantly.";
    toast("Settings saved.");
  }catch(e){
    // Fall back to the server's truth on the next poll.
    quickDirty.clear();
    $("#quick-status").textContent = "Could not save";
    toast(e.message, true);
  }finally{
    quickSaving = false;
    if (quickSaveQueued){
      quickSaveQueued = false;
      flushQuickSettings();
    }
  }
}

$("#quick-rumble").addEventListener("change", queueQuickSettings);
$("#quick-battery").addEventListener("change", queueQuickSettings);

async function pollState(){
  const requestStarted = performance.now();
  try{
    const snap = await api("/api/state");
    lastRequestMs = Math.round(performance.now() - requestStarted);
    REFRESH = (snap.config && snap.config.refresh_ms) || 750;

    const cfg = snap.config || {};
    const threshold = cfg.performance_warning_ms || 180;
    const lagging = lastRequestMs >= threshold || (snap.last_packet_ms >= threshold);
    const rate = Math.round(snap.packet_rate || 0);
    const batteryEnabled = !!cfg.battery_display_enabled;
    const rumbleEnabled = !!cfg.rumble_enabled;

    /* Header */
    const dot = $("#hdot"), hs = $("#hstatus");
    if (snap.pc_reachable){
      dot.className = "dot on";
      hs.textContent = "Linux connected · " + (snap.peer_ip || "PC");
    } else if (snap.connected){
      dot.className = "dot warn";
      hs.textContent = "Waiting for Linux…";
    } else {
      dot.className = "dot";
      hs.textContent = "Disconnected";
    }

    /* One question: is the whole path alive right now? */
    const status = !snap.pc_reachable ? (snap.connected ? "Partial" : "Offline")
                 : lagging ? "Degraded"
                 : rate > 0 ? (rate < 20 ? "Slow" : "Live") : "Idle";
    const liveClass = status === "Live" ? "live"
                    : (status === "Offline" || status === "Idle") ? "off" : "warn";

    let finding;
    if (!snap.pc_reachable){
      finding = snap.connected
        ? "Linux is connected, but no controller input is arriving. Check the controller and the Wi-Fi link."
        : "No Linux bridge is connected. Start the Linux container, then reconnect.";
    } else if (rate <= 0){
      finding = "The bridge is connected and waiting for the first controller packet.";
    } else if (rate < 20){
      finding = `Input is arriving at ${rate} Hz. Check the Wi-Fi link for packet loss.`;
    } else {
      finding = `Controller input is streaming to the virtual pad at ${rate} Hz.`;
    }
    if (lagging && snap.pc_reachable){
      finding += " Dashboard or packet timing is over the warning threshold.";
    }
    $("#d-finding").textContent = finding;
    $("#d-status").textContent = status;
    $("#d-live-dot").className = "dot " +
      (status === "Live" ? "on" : (status === "Offline" || status === "Idle") ? "" : "warn");
    $("#d-status-card").textContent = status;
    $("#d-status-card").className = "status-pill " + liveClass;
    $("#d-fresh").textContent = new Date().toLocaleTimeString([], {hour12:false});

    /* Signal path */
    $("#sp-name").textContent = snap.controller_name || "No controller";
    $("#sp-mac").textContent  = snap.controller_mac || "Bluetooth input";
    $("#sp-peer").textContent = snap.peer_ip || (snap.connected ? "Connected" : "Waiting");
    $("#sp-link").textContent = !snap.pc_reachable
      ? (snap.connected ? "Connected, no data" : "Not connected")
      : rate > 0 ? "Streaming input" : "Connected, waiting";
    $("#sp-rate").textContent = rate > 0 ? rate + " /s" : "—";
    $("#sp-interval").textContent = snap.last_packet_ms >= 0
      ? Number(snap.last_packet_ms).toFixed(1) + " ms interval"
      : "No packets yet";
    $("#sp-pad").textContent = snap.pc_reachable && rate > 0
      ? "Receiving input" : "Waiting for input";
    $("#sp-c1").classList.toggle("on", !!snap.connected);
    $("#sp-c2").classList.toggle("on", !!snap.pc_reachable);
    $("#sp-c3").classList.toggle("on", !!snap.pc_reachable && rate > 0);
    $("#sp-summary").textContent =
      (snap.controller_name || "No controller") + " · " +
      (snap.peer_ip || "no peer") + " · " +
      (rate > 0 ? rate + " packets/s" : "no packets") + " · " +
      (snap.pc_reachable && rate > 0 ? "virtual pad receiving"
        : snap.pc_reachable ? "waiting for input" : "virtual pad idle");

    /* Link card */
    $("#d-peer").textContent = snap.peer_ip || "(none)";
    $("#d-uptime").textContent = fmtUptime(snap.uptime_s);
    $("#d-packets").textContent = (snap.packets_sent||0).toLocaleString();
    $("#d-request").textContent = lastRequestMs + " ms";
    $("#d-packet").textContent = snap.last_packet_ms >= 0
      ? Number(snap.last_packet_ms).toFixed(1) + " ms" : "—";
    $("#d-rate").textContent = rate > 0 ? rate + " /s" : "—";
    $("#d-input-state").textContent = rate > 0 ? rate + " Hz" : (snap.pc_reachable ? "Idle" : "No packets");

    /* Controller card */
    $("#d-name").textContent = snap.controller_name || "(unknown)";
    $("#d-mac").textContent = snap.controller_mac || "(unknown)";
    $("#d-lastev").textContent = snap.last_event_ts || "—";
    if (!quickDirty.has("quick-rumble")) $("#quick-rumble").checked = rumbleEnabled;
    if (!quickDirty.has("quick-battery")) $("#quick-battery").checked = batteryEnabled;
    setBatteryVisible($("#quick-battery").checked);

    const b = snap.battery;
    const lowBat = cfg.low_battery_warn_pct || 15;
    const batteryKnown = !(b == null || b < 0);
    $("#d-bat").textContent = batteryKnown ? b + "%" : "Unknown";
    const batState = $("#d-batstate");
    batState.textContent = !batteryKnown ? "Unknown"
      : snap.charging ? "Charging"
      : b <= lowBat ? "Low battery" : "On battery";
    batState.className = "tag" +
      (batteryKnown && !snap.charging && b <= lowBat ? " warn" : "");
    const bar = $("#d-batbar");
    bar.style.width = batteryKnown ? Math.max(0, Math.min(100, b)) + "%" : "0%";
    bar.style.background = battColor(b, lowBat);

    /* Rumble */
    const rl = snap.rumble_left|0, rr = snap.rumble_right|0;
    $("#d-rl").textContent = rl; $("#d-rr").textContent = rr;
    $("#d-rlbar").style.width = Math.max(0, Math.min(100, rl/255*100)) + "%";
    $("#d-rrbar").style.width = Math.max(0, Math.min(100, rr/255*100)) + "%";
    const test = $("#rumble-test");
    const hasController = !!snap.controller_name;
    test.disabled = !snap.pc_reachable || !rumbleEnabled || !hasController;
    test.title = !rumbleEnabled ? "Turn vibration on first"
               : !snap.pc_reachable ? "Connect the Linux bridge first"
               : !hasController ? "No controller detected"
               : "Send a short pulse to both motors";
    $("#rumble-hint").textContent = !rumbleEnabled
      ? "Vibration is off. Game rumble is ignored."
      : !snap.pc_reachable ? "Waiting for the Linux bridge."
      : !hasController ? "No controller detected on the Linux side."
      : "A game's rumble is relayed to the controller.";

    /* Live input */
    const lt = snap.lt|0, rt = snap.rt|0;
    $("#d-lt").textContent = lt; $("#d-rt").textContent = rt;
    $("#d-ltbar").style.width = Math.max(0, Math.min(100, lt/255*100)) + "%";
    $("#d-rtbar").style.width = Math.max(0, Math.min(100, rt/255*100)) + "%";
    const lx = snap.lthumb_x|0, ly = snap.lthumb_y|0;
    const rx = snap.rthumb_x|0, ry = snap.rthumb_y|0;
    $("#d-lx").textContent = lx; $("#d-ly").textContent = ly;
    $("#d-rx").textContent = rx; $("#d-ry").textContent = ry;
    setStick("#stick-l-dot", lx, ly);
    setStick("#stick-r-dot", rx, ry);
    const low = snap.buttons_low|0, high = snap.buttons_high|0, dpad = snap.dpad|0;
    $("#d-buttons").textContent =
      "0x" + low.toString(16).padStart(2, "0") +
      " / 0x" + high.toString(16).padStart(2, "0");
    updateButtonChips(low, high, dpad);

    $("#perf-warning").classList.toggle("show", !!cfg.performance_warning_enabled && lagging);

    renderEvents(snap);
    renderButtonHistory(snap.button_events);
  }catch(e){
    $("#hdot").className = "dot err";
    $("#hstatus").textContent = "Dashboard offline";
    $("#d-status").textContent = "Offline";
    $("#d-live-dot").className = "dot err";
    $("#d-status-card").textContent = "Offline";
    $("#d-status-card").className = "status-pill off";
    $("#d-finding").textContent = "The dashboard cannot reach the Windows app.";
    $("#d-input-state").textContent = "No packets";
    $("#sp-summary").textContent = "Dashboard offline";
    $("#rumble-test").disabled = true;
    $("#sp-c1").classList.remove("on");
    $("#sp-c2").classList.remove("on");
    $("#sp-c3").classList.remove("on");
  }
  setTimeout(pollState, REFRESH);
}

/* The Linux management API knows which rumble backend is attached.  This
   is separate from pollState() so a slow/offline Linux peer cannot delay
   the main dashboard polling. */
async function pollFeedback(){
  const el = $("#d-feedback");
  const tag = $("#d-feedback-tag");
  try{
    const data = await api("/api/linux/status");
    if (!data || !data.ok) throw new Error("Linux offline");
    const r = data.rumble || {};
    if (!r.backend || r.backend === "none"){
      el.textContent = "No backend";
      el.style.color = "var(--warning)";
      el.title = r.error || "No rumble backend is attached on the Linux side.";
      tag.textContent = "No feedback";
      tag.className = "tag warn";
    } else {
      el.textContent = rumbleBackendLabel(r.backend) + (r.playing ? " · active" : " · ready");
      el.style.color = r.error ? "var(--warning)" : "var(--text)";
      el.title = (r.path || "") + (r.error ? " — " + r.error : "");
      tag.textContent = r.backend === "force feedback" ? "FF ready" : "Hidraw";
      tag.className = "tag" + (r.error ? " warn" : "");
    }
  }catch(e){
    el.textContent = "Linux offline";
    el.style.color = "var(--muted)";
    el.title = "";
    if (tag){ tag.textContent = "Feedback"; tag.className = "tag"; }
  }
  setTimeout(pollFeedback, document.hidden ? 12000 : 5000);
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

function escapeAttr(s){
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

async function copyLogs(){
  const el = $("#l-out");
  try{ await navigator.clipboard.writeText(el.innerText); toast("Log copied."); }
  catch(e){ toast("Copy failed", true); }
}

async function copyLinuxDiagnostics(){
  const error = $("#linux-rumble-error").hidden
    ? "(none)" : $("#linux-rumble-error").textContent;
  const text = [
    "Xbox Bridge Linux diagnostics",
    "status: " + $("#linux-status").textContent,
    "API: " + $("#linux-api").textContent,
    "controller: " + $("#linux-device").textContent,
    "rumble backend: " + $("#linux-rumble-backend").textContent,
    "rumble device: " + $("#linux-rumble-path").textContent,
    "rumble state: " + $("#linux-rumble-state").textContent,
    "rumble error: " + error,
    "",
    $("#linux-log").textContent || "(no Linux logs loaded)"
  ].join("\n");
  try{
    await navigator.clipboard.writeText(text);
    toast("Linux diagnostics copied.");
  }catch(e){
    toast("Copy failed", true);
  }
}

async function clearLogs(){
  if(!confirm("Delete the log file contents?")) return;
  try{ await api("/api/logs/clear", {method:"POST"}); loadLogs(); toast("Log cleared."); }
  catch(e){ toast(e.message, true); }
}

function linuxFields(config){
  const names = ["PC_HOST", "PC_PORT", "CONTROLLER_MAC", "LOG_LEVEL", "MANAGEMENT_TOKEN"];
  const labels = {PC_HOST:"Windows host", PC_PORT:"Windows port", CONTROLLER_MAC:"Controller MAC", LOG_LEVEL:"Log level", MANAGEMENT_TOKEN:"Management token"};
  return names.map(name => {
    const id = "linux-" + name.toLowerCase().replace(/_/g, "-");
    return `<div class="field">
      <div class="meta"><label class="lbl" for="${id}">${labels[name]}</label></div>
      <div class="ctl"><input id="${id}" type="text" data-linux="${name}" value="${escapeAttr(config?.[name] || "")}"></div>
    </div>`;
  }).join("");
}
async function loadLinux(){
  try{
    const data = await api("/api/linux/status");
    if (!data.ok) throw new Error(data.error || "Linux API unavailable");
    $("#linux-status").textContent = "Online";
    $("#linux-status").className = "status-pill live";
    $("#linux-api").textContent = (data.config?.MANAGEMENT_PORT || "9988");
    $("#linux-pid").textContent = data.pid;
    $("#linux-uptime").textContent = fmtUptime(data.uptime_s);
    $("#linux-device").textContent = data.controller_path || (data.controller ? "connected" : "not found");
    const rumble = data.rumble || {};
    $("#linux-rumble-backend").textContent = rumbleBackendLabel(rumble.backend);
    $("#linux-rumble-path").textContent = rumble.path || "—";
    $("#linux-rumble-state").textContent = rumble.error ? "Error"
      : rumble.playing ? "Rumbling"
      : (rumble.backend && rumble.backend !== "none") ? "Idle" : "Unavailable";
    const errEl = $("#linux-rumble-error");
    errEl.textContent = rumble.error || "";
    errEl.hidden = !rumble.error;
    $("#linux-rumble-test").disabled = !rumble.backend || rumble.backend === "none";
    $("#linux-form").innerHTML = linuxFields(data.config);
  }catch(e){
    $("#linux-status").textContent = "Offline";
    $("#linux-status").className = "status-pill off";
    $("#linux-api").textContent = "—";
    $("#linux-pid").textContent = "—";
    $("#linux-uptime").textContent = "—";
    $("#linux-device").textContent = "—";
    $("#linux-rumble-backend").textContent = "—";
    $("#linux-rumble-path").textContent = "—";
    $("#linux-rumble-state").textContent = "—";
    $("#linux-rumble-error").hidden = true;
    $("#linux-rumble-test").disabled = true;
    $("#linux-form").innerHTML = '<p class="hint">Connect the Linux bridge to edit its settings.</p>';
  }
}
async function loadLinuxLogs(){
  try{ const data = await api("/api/linux/logs"); $("#linux-log").textContent = (data.lines || []).join("\n") || "No Linux logs available."; }
  catch(e){ $("#linux-log").textContent = "Linux logs unavailable: " + e.message; }
}
async function saveLinux(){
  const values = {}; document.querySelectorAll("[data-linux]").forEach(el => values[el.dataset.linux] = el.value);
  try{ const data = await api("/api/linux/config", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({values})});
    if (!data.ok) throw new Error(data.error || "Save failed"); toast("Linux settings saved. Restart to apply.");
  }catch(e){ toast(e.message, true); }
}
async function restartLinux(){
  if(!confirm("Restart the Linux bridge container? Input will pause briefly.")) return;
  try{ const data = await api("/api/linux/restart", {method:"POST"}); if (!data.ok) throw new Error(data.error || "Restart failed"); toast("Linux restart requested."); }
  catch(e){ toast(e.message, true); }
}
async function testLinuxRumble(){
  const btn = $("#linux-rumble-test");
  if (btn){ btn.disabled = true; btn.textContent = "Testing…"; }
  try{
    const data = await api("/api/linux/rumble/test", {
      method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"
    });
    if (!data.ok) throw new Error(data.error || "Rumble test failed");
    toast("Linux-side rumble pulse sent. You should feel both motors.");
    await loadLinux();
  }catch(e){ toast(e.message, true); }
  finally{
    if (btn) btn.textContent = "Test on Linux";
    setTimeout(loadLinux, 1200);
  }
}

/* ---- settings ---- */
let SCHEMA = [];
function markSettingsDirty(){
  const state = $("#settings-state");
  if (!state) return;
  state.textContent = "Unsaved changes";
  state.className = "status-pill warn";
}
$("#s-form").addEventListener("input", markSettingsDirty);
$("#s-form").addEventListener("change", markSettingsDirty);
function buildSettings(values){
  const groups = {};
  SCHEMA.forEach(f => (groups[f.section] = groups[f.section] || []).push(f));
  const titles = {app:"Application", webui:"Web UI", controller:"Controller",
                  tray:"Tray", network:"Network"};
  const descriptions = {
    app: "Startup, logging and the address this app listens on.",
    webui: "Dashboard refresh rate and performance warnings.",
    controller: "Rumble, battery display, dead zones and packet rate.",
    tray: "Tray icon and tooltip behaviour.",
    network: "Discovery and the port the Linux bridge connects to."
  };
  let html = "";
  for (const sec of Object.keys(groups)){
    const note = {
      app: `Listening on ${values["app.listen_host"]}:${values["app.listen_port"]} · discovery ${values["app.auto_discover"] ? "on" : "off"}`,
      webui: `Refresh ${values["webui.auto_refresh_ms"]} ms · warning at ${values["controller.performance_warning_ms"]} ms`,
      controller: `Rumble ${values["controller.rumble_enabled"] ? "on" : "off"} · battery ${values["controller.battery_display_enabled"] ? "shown" : "hidden"} · ${values["controller.deadzone_stick"]}% stick dead zone`,
      tray: values["tray.show_battery"] ? "Battery level included in the tooltip" : "Tooltip shows connection only",
      network: `Discovery on UDP ${values["network.discovery_port"]} · ${values["network.keepalive_timeout_s"]} s timeout`,
      linux: `Management API on port ${values["linux.management_port"]}`
    }[sec] || "";
    html += `<section class="settings-group">
      <div class="settings-head">
        <h2>${titles[sec] || sec}</h2>
        <p class="group-desc">${descriptions[sec] || ""}</p>
        <p class="group-note">${escapeHtml(note)}</p>
      </div>
      <div class="settings-panel">`;
    for (const f of groups[sec]){
      const v = values[f.name];
      const id = "set-" + f.name.replace(/\./g, "-");
      const restart = f.restart ? ' <span class="tag">restart</span>' : "";
      let ctl = "";
      if (f.kind === "bool"){
        ctl = `<label class="switch-row" title="Toggle ${escapeAttr(f.label)}">
            <input id="${id}" type="checkbox" data-name="${escapeAttr(f.name)}"
                   aria-label="${escapeAttr(f.label)}" ${v ? "checked" : ""}>
            <span class="switch" aria-hidden="true"></span>
          </label>`;
      } else if (f.kind === "choice"){
        ctl = `<select id="${id}" data-name="${escapeAttr(f.name)}">` +
          f.choices.map(c => `<option ${c===v?"selected":""}>${escapeAttr(c)}</option>`).join("") +
          `</select>`;
      } else if (f.kind === "int" || f.kind === "float"){
        const step = f.step || (f.kind === "int" ? 1 : 0.1);
        ctl = `<input id="${id}" type="number" data-name="${escapeAttr(f.name)}" value="${escapeAttr(v)}"
                 step="${step}" ${f.min!=null?`min="${f.min}"`:""} ${f.max!=null?`max="${f.max}"`:""}>`;
      } else {
        ctl = `<input id="${id}" type="text" data-name="${escapeAttr(f.name)}" value="${escapeAttr(v)}">`;
      }
      html += `<div class="field">
          <div class="meta">
            <label class="lbl" for="${id}">${f.label}${restart}</label>
            <div class="hint">${f.help||""}</div>
          </div>
          <div class="ctl">${ctl}</div>
        </div>`;
    }
    html += `</div></section>`;
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
  const state = $("#settings-state");
  if (state){ state.textContent = "Saving…"; state.className = "status-pill warn"; }
  try{
    const res = await api("/api/config", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({values: collectSettings()})
    });
    let msg = "Settings saved.";
    if (res.restart_required && res.restart_required.length)
      msg += " Restart required: " + res.restart_required.join(", ") + ".";
    if (state){ state.textContent = "Saved"; state.className = "status-pill live"; }
    toast(msg);
    await loadSettings();
  }catch(e){
    if (state){ state.textContent = "Save failed"; state.className = "status-pill off"; }
    toast(e.message, true);
  }
}

async function resetSettings(){
  if(!confirm("Reset every setting to its default value?")) return;
  const state = $("#settings-state");
  if (state){ state.textContent = "Resetting…"; state.className = "status-pill warn"; }
  try{
    const res = await api("/api/config/reset", {method:"POST"});
    if (state){ state.textContent = "Defaults restored"; state.className = "status-pill live"; }
    toast("Defaults restored." + (res.restart_required?.length ? " Restart required." : ""));
    await loadSettings();
  }catch(e){
    if (state){ state.textContent = "Reset failed"; state.className = "status-pill off"; }
    toast(e.message, true);
  }
}

/* ---- actions ---- */
async function testRumble(){
  const btn = $("#rumble-test");
  if (btn){ btn.disabled = true; btn.textContent = "Testing…"; }
  try{ await act("test_rumble"); }
  finally{
    setTimeout(() => {
      if (btn){ btn.textContent = "Test motors"; }
    }, 1100);
  }
}

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
    `<div style="min-height:100vh;display:grid;place-items:center;padding:24px;text-align:center">
       <div><div style="font-size:20px;font-weight:600;margin-bottom:8px">Xbox Bridge stopped</div>
       <div style="color:#9a9da5;font-size:13px">You can close this tab.</div></div>
     </div>`; }, 400);
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
  pollFeedback();
  setInterval(() => { if($("#l-auto").checked && $("#tab-logs").classList.contains("active")) loadLogs(); }, 2000);
})();
</script>
</body>
</html>
"""
