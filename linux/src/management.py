"""Small HTTP control plane for the Linux container."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("management")

_ALLOWED = {"PC_HOST", "PC_PORT", "CONTROLLER_MAC", "LOG_LEVEL",
            "MANAGEMENT_PORT", "MANAGEMENT_TOKEN"}


class ManagementServer:
    """Expose status, recent logs, safe env settings, and container restart."""

    def __init__(self, app, host: str = "0.0.0.0", port: int = 9988,
                 config_dir: str = "/config") -> None:
        self._app = app
        self._host = host
        self._port = port
        self._config_path = Path(config_dir) / ".env"
        self._token = os.getenv("MANAGEMENT_TOKEN", "")
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> str:
        return f"{self._host}:{self._port}"

    def start(self) -> None:
        try:
            owner = self

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, fmt, *args):
                    logger.debug("HTTP %s - %s", self.address_string(), fmt % args)

                def do_GET(self):
                    if not owner._authorized(self):
                        owner._send(self, {"ok": False, "error": "unauthorized"}, 401)
                        return
                    parsed = urlparse(self.path)
                    if parsed.path == "/api/status":
                        owner._send(self, owner._status())
                    elif parsed.path == "/api/logs":
                        owner._send(self, {"lines": owner._read_logs()})
                    elif parsed.path == "/api/trace":
                        owner._send(self, {"lines": owner._read_trace()})
                    else:
                        owner._send(self, {"error": "not found"}, 404)

                def do_POST(self):
                    if not owner._authorized(self):
                        owner._send(self, {"ok": False, "error": "unauthorized"}, 401)
                        return
                    parsed = urlparse(self.path)
                    length = min(int(self.headers.get("Content-Length", "0")), 65536)
                    try:
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except (ValueError, TypeError):
                        owner._send(self, {"error": "invalid JSON"}, 400)
                        return
                    if parsed.path == "/api/config":
                        owner._send(self, owner._update_config(body))
                    elif parsed.path == "/api/restart":
                        owner._send(self, {"ok": True, "message": "restart requested"})
                        threading.Timer(0.2, owner._restart).start()
                    else:
                        owner._send(self, {"error": "not found"}, 404)

            self._server = ThreadingHTTPServer((self._host, self._port), Handler)
            self._thread = threading.Thread(target=self._server.serve_forever,
                                            name="LinuxManagement", daemon=True)
            self._thread.start()
            logger.info("Linux management API listening on %s", self.address)
        except OSError as exc:
            logger.error("Could not start Linux management API on %s: %s", self.address, exc)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def _send(self, handler, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    def _authorized(self, handler) -> bool:
        return not self._token or handler.headers.get("X-Bridge-Token", "") == self._token

    def _status(self) -> dict:
        state = self._app._state
        return {
            "ok": True,
            "pid": os.getpid(),
            "uptime_s": int(__import__("time").monotonic() - self._app._start_time),
            "controller": bool(self._app._device),
            "controller_path": getattr(self._app._device, "path", "") if self._app._device else "",
            "tcp_connected": bool(self._app._tcp and self._app._tcp.connected),
            "config": {key: os.getenv(key, "") for key in sorted(_ALLOWED)},
            "log_path": os.getenv("LOG_FILE", "/config/xbox-bridge.log"),
            "state": {
                "buttons_low": getattr(state, "buttons_low", 0) if state else 0,
                "buttons_high": getattr(state, "buttons_high", 0) if state else 0,
            },
        }

    def _read_logs(self) -> list[str]:
        path = Path(os.getenv("LOG_FILE", "/config/xbox-bridge.log"))
        try:
            return path.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]
        except OSError:
            return []

    def _read_trace(self) -> list[str]:
        """The last lines of the input trace log (see the trace module)."""
        path = Path(os.getenv(
            "TRACE_FILE",
            str(Path(os.getenv("LOG_FILE", "/config/xbox-bridge.log")).with_name(
                "input-trace.log"))))
        try:
            return path.read_text(encoding="utf-8", errors="replace").splitlines()[-4000:]
        except OSError:
            return []

    def _update_config(self, values: dict) -> dict:
        values = values.get("values", values)
        invalid = sorted(set(values) - _ALLOWED)
        if invalid:
            return {"ok": False, "error": f"unsupported settings: {', '.join(invalid)}"}
        lines = {}
        try:
            if self._config_path.exists():
                for line in self._config_path.read_text(encoding="utf-8").splitlines():
                    if "=" in line and not line.lstrip().startswith("#"):
                        key, value = line.split("=", 1)
                        lines[key.strip()] = value.strip()
            for key, value in values.items():
                if key in _ALLOWED:
                    lines[key] = str(value)
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            self._config_path.write_text(
                "# Managed by Bluetooth Bridge\n" +
                "\n".join(f"{key}={value}" for key, value in sorted(lines.items())) + "\n",
                encoding="utf-8",
            )
            logger.info("Linux settings updated: %s", ", ".join(sorted(values)) or "none")
            return {"ok": True, "restart_required": True, "config": lines}
        except OSError as exc:
            logger.error("Could not update Linux settings: %s", exc)
            return {"ok": False, "error": str(exc)}

    def _restart(self) -> None:
        logger.warning("Restart requested through Linux management API")
        os.kill(os.getpid(), signal.SIGTERM)
