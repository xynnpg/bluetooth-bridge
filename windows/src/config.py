"""Central configuration for the Windows bridge.

config.ini lives next to the executable. Every setting is declared once in
``SPEC`` so the Flask web UI can render and validate the form dynamically
instead of hard-coding it.
"""

from __future__ import annotations

import configparser
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("config")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Field:
    section: str
    key: str
    label: str
    kind: str                       # bool | int | float | str | choice
    default: object
    help: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    choices: tuple[str, ...] = ()
    restart: bool = False

    @property
    def name(self) -> str:
        return f"{self.section}.{self.key}"

    def as_dict(self) -> dict:
        return {
            "name":   self.name,
            "section": self.section,
            "key":    self.key,
            "label":  self.label,
            "kind":   self.kind,
            "help":   self.help,
            "min":    self.minimum,
            "max":    self.maximum,
            "step":   self.step,
            "choices": list(self.choices),
            "restart": self.restart,
        }


SPEC: tuple[Field, ...] = (
    Field("app", "listen_port", "Listen Port", "int", 9999,
          "TCP port the Windows app listens on for the Linux bridge.",
          1024, 65535, restart=True),
    Field("app", "listen_host", "Listen Host", "str", "0.0.0.0",
          "Bind address for the TCP server.", restart=True),
    Field("app", "auto_discover", "Auto-discovery broadcast", "bool", True,
          "Announce this PC over UDP so Linux finds it automatically."),
    Field("app", "auto_start", "Start with Windows", "bool", True,
          "Keep a shortcut in the Start Menu Startup folder."),
    Field("app", "log_level", "Log Level", "choice", "INFO",
          "Verbosity of the log file and event feed.",
          choices=("DEBUG", "INFO", "WARNING", "ERROR")),

    Field("webui", "enabled", "Web UI enabled", "bool", True,
          "Serve the dashboard over HTTP.", restart=True),
    Field("webui", "host", "Web UI host", "str", "127.0.0.1",
          "Interface the dashboard binds to. Keep 127.0.0.1 for local-only.",
          restart=True),
    Field("webui", "port", "Web UI port", "int", 8764,
          "Port the local dashboard listens on. 0 = pick a random free port.",
          0, 65535, restart=True),
    Field("webui", "open_on_launch", "Open dashboard on launch", "bool", True,
          "Open the dashboard in the browser when the app starts."),
    Field("webui", "auto_refresh_ms", "Dashboard refresh (ms)", "int", 750,
          "How often the dashboard polls for fresh state.", 250, 5000),

    Field("controller", "rumble_enabled", "Rumble enabled", "bool", True,
          "Forward game rumble to the physical controller."),
    Field("controller", "low_battery_warn_pct", "Low battery warning (%)", "int", 15,
          "Highlight the battery when it drops to or below this level.",
          0, 100),
    Field("controller", "deadzone_stick", "Stick deadzone (%)", "int", 15,
          "Ignore thumbstick movement below this percentage.", 0, 50),
    Field("controller", "deadzone_trigger", "Trigger deadzone (%)", "int", 1,
          "Ignore trigger pressure below this percentage.", 0, 50),
    Field("controller", "invert_left_y", "Invert left stick Y", "bool", False,
          "Flip the left thumbstick's vertical axis."),
    Field("controller", "invert_right_y", "Invert right stick Y", "bool", False,
          "Flip the right thumbstick's vertical axis."),
        Field("controller", "battery_display_enabled", "Battery display enabled", "bool", True,
            "Show battery data in the dashboard and tray tooltip."),
        Field("controller", "performance_warning_enabled", "Performance warning", "bool", True,
            "Warn when dashboard requests or packet timing indicate network lag."),
        Field("controller", "performance_warning_ms", "Performance warning threshold (ms)", "int", 180,
            "Show the dashboard warning when request or packet timing exceeds this value.",
            50, 5000),

    Field("tray", "show_battery", "Show battery in tray tooltip", "bool", True,
          "Include the controller battery level in the tray tooltip."),

    Field("network", "discovery_port", "Discovery port", "int", 9876,
          "UDP port used for auto-discovery beacons.", 1024, 65535,
          restart=True),
    Field("network", "keepalive_timeout_s", "Connection timeout (s)", "float", 6.0,
          "Treat the Linux side as gone after this many silent seconds.",
          1.0, 60.0, step=0.5),
)

_BY_NAME: dict[str, Field] = {f.name: f for f in SPEC}


def _coerce(field: Field, raw: str | None) -> object:
    if raw is None:
        return field.default
    text = str(raw).strip()
    try:
        if field.kind == "bool":
            low = text.lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            return field.default
        if field.kind == "int":
            return int(float(text))
        if field.kind == "float":
            return float(text)
        if field.kind == "choice":
            return text if text in field.choices else field.default
        return text
    except (TypeError, ValueError):
        return field.default


def _validate(field: Field, value: object) -> object:
    if field.kind in ("int", "float"):
        try:
            num = int(value) if field.kind == "int" else float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field.label} must be a number.") from None
        if field.minimum is not None and num < field.minimum:
            raise ValueError(f"{field.label} must be ≥ {field.minimum:g}.")
        if field.maximum is not None and num > field.maximum:
            raise ValueError(f"{field.label} must be ≤ {field.maximum:g}.")
        return num
    if field.kind == "bool":
        return bool(value)
    if field.kind == "choice":
        if value not in field.choices:
            raise ValueError(f"{field.label} must be one of {', '.join(field.choices)}.")
        return value
    text = str(value).strip()
    if field.name == "app.listen_host" and not text:
        raise ValueError("Listen Host cannot be empty.")
    if field.name in ("webui.host",) and not text:
        raise ValueError("Web UI host cannot be empty.")
    return text


def _fallback(section: str, key: str) -> object:
    """Default for a (section, key) that is not in SPEC, else None."""
    for f in SPEC:
        if f.section == section and f.key == key:
            return f.default
    return None


class Config:
    """Loads, validates and persists config.ini."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._values: dict[str, object] = {f.name: f.default for f in SPEC}
        self.reload()

    # ------------------------------------------------------------------

    def reload(self) -> None:
        cp = configparser.ConfigParser()
        if os.path.exists(self.path):
            try:
                cp.read(self.path, encoding="utf-8-sig")
            except (OSError, configparser.Error) as exc:
                logger.warning("Could not read %s — using defaults: %s",
                               self.path, exc)
        for f in SPEC:
            raw = cp.get(f.section, f.key, fallback=None)
            self._values[f.name] = _coerce(f, raw)

    # ------------------------------------------------------------------
    # Typed access
    # ------------------------------------------------------------------

    def get(self, name: str) -> object:
        return self._values.get(name, _fallback(*name.split(".", 1)))

    def get_bool(self, name: str) -> bool:
        return bool(self.get(name))

    def get_int(self, name: str) -> int:
        try:
            return int(self.get(name))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return int(_fallback(*name.split(".", 1)) or 0)

    def get_float(self, name: str) -> float:
        try:
            return float(self.get(name))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return float(_fallback(*name.split(".", 1)) or 0.0)

    def get_str(self, name: str) -> str:
        value = self.get(name)
        return "" if value is None else str(value)

    def as_dict(self) -> dict:
        return dict(self._values)

    def schema(self) -> list[dict]:
        return [f.as_dict() for f in SPEC]

    # ------------------------------------------------------------------
    # Update + persist
    # ------------------------------------------------------------------

    def update(self, incoming: dict) -> dict:
        """Validate and apply ``incoming``; returns the full new value map.

        Raises ValueError (with a human-readable message) on bad input. No
        file is written here — call :meth:`save` afterwards.
        """
        cleaned: dict[str, object] = {}
        for name, value in incoming.items():
            field = _BY_NAME.get(name)
            if field is None:
                continue
            cleaned[name] = _validate(field, value)
        self._values.update(cleaned)
        return dict(self._values)

    def save(self) -> None:
        cp = configparser.ConfigParser()
        for f in SPEC:
            if not cp.has_section(f.section):
                cp.add_section(f.section)
            value = self._values.get(f.name, f.default)
            if isinstance(value, bool):
                text = "true" if value else "false"
            elif isinstance(value, float):
                text = f"{value:g}"
            else:
                text = str(value)
            cp.set(f.section, f.key, text)

        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            cp.write(fh)

    def restart_required(self, changed: dict) -> list[str]:
        """Labels of changed fields that only apply after a restart."""
        labels = []
        for name in changed:
            field = _BY_NAME.get(name)
            if field is not None and field.restart:
                labels.append(field.label)
        return labels
