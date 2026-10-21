"""Xbox Bridge — Linux side. Reads a Bluetooth Xbox controller and streams state over TCP."""

import logging
import os

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

if os.getenv("LOG_FILE"):
    try:
        logging.getLogger().addHandler(
            logging.FileHandler(os.environ["LOG_FILE"], encoding="utf-8"))
    except OSError:
        logging.getLogger(__name__).warning("Could not open LOG_FILE")

# The input trace — a separate, millisecond-precise log of the whole input
# path.  It is the log to look at when a button gets stuck.
try:
    from . import trace as _trace

    _TRACE_PATH = _trace.setup()
    if _TRACE_PATH:
        logging.getLogger(__name__).info("Input trace log: %s", _TRACE_PATH)
except Exception as _exc:                       # pragma: no cover - defensive
    logging.getLogger(__name__).warning("Could not open the input trace: %s", _exc)
