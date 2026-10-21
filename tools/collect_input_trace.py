#!/usr/bin/env python3
"""Collect and merge the input traces of the Linux and the Windows side.

Both sides write the same trace format (see ``linux/src/trace.py``)::

    21:14:23.481 LINUX  BTN press    A
    21:14:23.482 LINUX  TX          buttons=01/00 dpad=0
    21:14:23.512 WIN    RX          buttons=01/00 dpad=0
    21:14:23.513 WIN    PAD press   A

This script merges the two files into one chronologically sorted log and
reports the interesting part: a button that was pressed but never
released, or a release that never made it across.

Usage (on the Windows PC)::

    python tools/collect_input_trace.py --linux 192.168.0.10

Usage (on the Linux host)::

    python3 tools/collect_input_trace.py

Options::

    --linux IP     IP of the Linux container host (management API, port 9988)
    --token TOKEN  management token (MANAGEMENT_TOKEN in the Linux .env)
    --port PORT    management port (default 9988)
    --out FILE     write the merged log here (default: input-trace-<time>.txt)
    --no-analyse   only write the merged log
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime

DEFAULT_PORT = 9988

#: "21:14:23.481 LINUX  BTN press    A"
_LINE_RE = re.compile(
    r"^(?P<time>\d{2}:\d{2}:\d{2}\.\d{3})\s+(?P<src>LINUX|WIN)\s+(?P<rest>.*)$")

#: A button transition: "BTN A        press" / "PAD A        release"
_BTN_RE = re.compile(r"^(?:BTN|PAD)\s+(?P<name>\S+)\s+(?P<action>press|release)\b")


def _default_windows_trace() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("USERPROFILE") or "."
    return os.path.join(base, "bluetooth_bridge", "input-trace.log")


def _default_linux_trace() -> str:
    home = os.path.expanduser("~")
    return os.path.join(home, ".bluetooth-bridge", "input-trace.log")


def read_local(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip("\n") for ln in fh]
    except OSError:
        return []


def fetch_linux(host: str, port: int, token: str) -> list[str]:
    """Fetch the Linux trace through the management API."""
    url = f"http://{host}:{port}/api/trace"
    request = urllib.request.Request(url)
    if token:
        request.add_header("X-Bridge-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return [str(ln) for ln in payload.get("lines", [])]
    except (OSError, urllib.error.URLError, ValueError) as exc:
        print(f"! could not fetch the Linux trace from {url}: {exc}",
              file=sys.stderr)
        return []


def _config_value(section: str, option: str) -> str:
    """Read a value from the Windows config.ini (if it exists)."""
    candidates = [
        os.path.join(os.environ.get("USERPROFILE", "."), "bluetooth_bridge",
                     "config.ini"),
        os.path.join(os.getcwd(), "config.ini"),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        parser = configparser.ConfigParser()
        try:
            parser.read(path, encoding="utf-8")
        except (OSError, configparser.Error):
            continue
        if parser.has_option(section, option):
            return parser.get(section, option, fallback="").strip()
    return ""


def merge(*chunks: list[str]) -> list[str]:
    """Merge the trace lines of both sides, sorted by their timestamp."""
    lines: list[tuple[str, str]] = []
    for chunk in chunks:
        for line in chunk:
            match = _LINE_RE.match(line.strip())
            if match:
                lines.append((match.group("time"), line.rstrip()))
            elif line.strip():
                lines.append(("00:00:00.000", line.rstrip()))
    lines.sort(key=lambda item: item[0])
    return [text for _stamp, text in lines]


def analyse(lines: list[str]) -> list[str]:
    """Look for a button that was pressed but never released.

    This is the fingerprint of the "the button is still held" bug: the
    press arrives on both sides, the release does not.
    """
    held: dict[tuple[str, str], str] = {}     # (side, button) -> press time
    problems: list[str] = []

    for line in lines:
        match = _LINE_RE.match(line.strip())
        if not match:
            continue
        source = match.group("src")
        stamp = match.group("time")
        button = _BTN_RE.match(match.group("rest"))
        if not button:
            continue
        name = button.group("name")
        action = button.group("action")
        key = (source, name)
        if action == "press":
            held[key] = stamp
        else:
            held.pop(key, None)

    for (source, name), stamp in sorted(held.items(), key=lambda i: i[1]):
        problems.append(
            f"{stamp}  {source:5s}  {name} was pressed and never released "
            f"— this button is still held")

    # A release on the Linux side that never reached the Windows side.
    linux_release: dict[str, str] = {}
    windows_release: dict[str, str] = {}
    for line in lines:
        match = _LINE_RE.match(line.strip())
        if not match:
            continue
        button = _BTN_RE.match(match.group("rest"))
        if not button or button.group("action") != "release":
            continue
        if match.group("src") == "LINUX":
            linux_release[button.group("name")] = match.group("time")
        else:
            windows_release[button.group("name")] = match.group("time")

    for name, stamp in sorted(linux_release.items(), key=lambda i: i[1]):
        if name not in windows_release:
            problems.append(
                f"{stamp}  LINUX  the release of {name} never reached the "
                f"Windows side — the button stays held in the game")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--linux", help="IP of the Linux host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", default="")
    parser.add_argument("--windows-trace", default=_default_windows_trace())
    parser.add_argument("--linux-trace", default=_default_linux_trace())
    parser.add_argument("--out", default="")
    parser.add_argument("--no-analyse", action="store_true")
    args = parser.parse_args()

    windows_lines = read_local(args.windows_trace)
    linux_lines: list[str] = []
    if args.linux:
        token = args.token or _config_value("linux", "management_token")
        linux_lines = fetch_linux(args.linux, args.port, token)
        if not linux_lines:
            linux_lines = read_local(args.linux_trace)
    else:
        linux_lines = read_local(args.linux_trace)

    if not windows_lines and not linux_lines:
        print("! no trace files found — has the bridge been running?",
              file=sys.stderr)
        return 1

    merged = merge(windows_lines, linux_lines)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = args.out or f"input-trace-{stamp}.txt"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(f"# Bluetooth Bridge input trace — {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        fh.write(f"# windows: {args.windows_trace} ({len(windows_lines)} lines)\n")
        fh.write(f"# linux:   {args.linux_trace} ({len(linux_lines)} lines)\n")
        fh.write("\n")
        fh.write("\n".join(merged))
        fh.write("\n")

    print(f"merged trace written to {out} ({len(merged)} lines)")

    problems = [] if args.no_analyse else analyse(merged)
    if problems:
        print("\n!! suspicious events:")
        for problem in problems:
            print("   " + problem)
    else:
        print("\nno stuck button found in this trace")
    return 0


if __name__ == "__main__":
    sys.exit(main())
