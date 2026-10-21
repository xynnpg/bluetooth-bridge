"""Structured input trace — a precise, mergeable log of the input pipeline.

The regular log is meant for humans and uses a coarse ``H:M:SS``
timestamp.  When a button gets stuck it matters *exactly* when the press
was seen on the Linux side and when the release arrived on the Windows
side, so this log writes one line per event with millisecond precision
and a source tag.  Both sides write the same format, so the two files can
be merged and sorted by time to see the whole path of an input.

Example::

    21:14:23.481 LINUX  BTN press    A
    21:14:23.482 LINUX  TX          buttons=01/00 dpad=0
    21:14:23.512 WIN    RX          buttons=01/00 dpad=0
    21:14:23.513 WIN    PAD press   A
    21:14:27.902 WIN    PAD release A
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import time

#: Tag written in front of every line — "LINUX" or "WIN".
SOURCE = "WIN"

#: Where the trace is written (overridable with the TRACE_FILE variable).
DEFAULT_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA", os.environ.get("USERPROFILE", ".")),
    "bluetooth_bridge", "input-trace.log",
)

#: The trace never propagates into the regular log — it has its own file.
#: The logger name carries the side, so the two traces never mix.
_logger = logging.getLogger(f"inputtrace.{SOURCE.lower()}")
_logger.setLevel(logging.INFO)
_logger.propagate = False

_handler: logging.Handler | None = None


class _TraceFormatter(logging.Formatter):
    """``H:M:S.mmm SOURCE message`` — sortable and easy to grep."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        return f"{stamp}.{int(record.msecs):03d} {SOURCE} {record.getMessage()}"


def setup(path: str | None = None) -> str:
    """Open the trace file.  Returns the path ("" when it is disabled)."""
    global _handler
    target = path or os.getenv("TRACE_FILE") or DEFAULT_PATH
    if _handler is not None:
        return target
    try:
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            target, maxBytes=8 * 1024 * 1024, backupCount=2, encoding="utf-8")
    except OSError:
        return ""
    handler.setFormatter(_TraceFormatter())
    _logger.addHandler(handler)
    _handler = handler
    return target


def event(message: str, *args) -> None:
    """Write one trace line."""
    try:
        _logger.info(message, *args)
    except Exception:            # pragma: no cover - never break the bridge
        pass


def state(label: str, buttons_low: int, buttons_high: int, dpad: int,
          lt: int = 0, rt: int = 0) -> None:
    """Write a compact state line (the button / D-pad / trigger snapshot)."""
    event("%s buttons=%02x/%02x dpad=%x lt=%d rt=%d",
          label, buttons_low & 0xFF, buttons_high & 0xFF,
          dpad & 0x0F, lt & 0xFF, rt & 0xFF)
