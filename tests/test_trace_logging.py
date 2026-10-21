"""The input trace — the log to look at when a button gets stuck.

Contract items covered here:

* both sides write the same, mergeable format (``H:M:S.mmm SOURCE text``),
* the Linux side really opens the trace file when the package is imported
  (it used to be missing — the trace was silently never written),
* the trace does not flood the regular log,
* the collector merges the two sides and finds a button that stayed held.
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT, load_package_module

TRACE_LINE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3} (LINUX|WIN) .+$")


# ---------------------------------------------------------------------------
# The format
# ---------------------------------------------------------------------------

def test_the_trace_format_is_time_source_message(linux_trace, windows_trace):
    """Every line is ``H:M:S.mmm SOURCE message`` — sortable and mergeable."""
    for module, source in ((linux_trace, "LINUX"), (windows_trace, "WIN")):
        module.event("BTN A        press")
        path = module._handler.baseFilename
        lines = [ln for ln in open(path, encoding="utf-8").read().splitlines() if ln]
        assert lines, "the trace file is empty"
        for line in lines:
            assert TRACE_LINE.match(line), f"bad trace line: {line!r}"
            assert f" {source} " in line, f"wrong source tag: {line!r}"
            module._handler.close()


def test_the_trace_does_not_flood_the_regular_log(linux_trace):
    """The trace has its own file — it must not end up in the main log.

    The trace logger must not propagate, so the handlers of the root
    logger (the regular log) never see a trace line.
    """
    import logging

    seen: list = []

    class _RootHandler(logging.Handler):
        def emit(self, record):
            seen.append(record)

    root = logging.getLogger()
    handler = _RootHandler()
    root.addHandler(handler)
    try:
        linux_trace.event("BTN A        press")
    finally:
        root.removeHandler(handler)

    assert linux_trace._logger.propagate is False, \
        "the trace logger propagates into the regular log"
    assert not [r for r in seen if "BTN A" in r.getMessage()], \
        "the trace was written to the regular log"


def test_the_linux_package_opens_the_trace_file(tmp_path, monkeypatch):
    """Importing the Linux package must open the trace file.

    Regression: the trace setup was never written, so the Linux side
    produced no trace at all — and the bug it was built for would have
    been impossible to diagnose.
    """
    target = tmp_path / "input-trace.log"
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "xbox-bridge.log"))
    monkeypatch.setenv("TRACE_FILE", str(target))

    # A fresh import of the package with the new environment.
    for name in [n for n in list(sys.modules) if n == "src" or n.startswith("src.")]:
        del sys.modules[name]
    sys.path.insert(0, str(ROOT / "linux"))
    try:
        module = importlib.import_module("src")
        assert module._TRACE_PATH == str(target)
        assert target.exists(), "the trace file was not created"
    finally:
        sys.path.remove(str(ROOT / "linux"))
        for name in [n for n in list(sys.modules) if n == "src" or n.startswith("src.")]:
            del sys.modules[name]


# ---------------------------------------------------------------------------
# The collector
# ---------------------------------------------------------------------------

def _write(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_the_collector_merges_both_sides(tmp_path):
    """The two sides are merged into one chronologically sorted log."""
    linux = tmp_path / "linux.log"
    windows = tmp_path / "windows.log"
    _write(linux, [
        "10:00:00.100 LINUX BTN A        press",
        "10:00:00.101 LINUX TX buttons=01/00 dpad=0 lt=0 rt=0",
    ])
    _write(windows, [
        "10:00:00.150 WIN    RX buttons=01/00 dpad=0 lt=0 rt=0",
        "10:00:00.151 WIN    PAD A        press",
    ])

    out = tmp_path / "merged.txt"
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "collect_input_trace.py"),
         "--windows-trace", str(windows), "--linux-trace", str(linux),
         "--out", str(out)],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr

    merged = out.read_text(encoding="utf-8").splitlines()
    stamps = [ln.split()[0] for ln in merged if ln and not ln.startswith("#")]
    assert stamps == sorted(stamps), "the merged log is not sorted by time"
    assert any("LINUX" in ln for ln in merged)
    assert any("WIN" in ln for ln in merged)


def test_the_collector_finds_a_button_that_stayed_held(tmp_path):
    """A button that was pressed but never released must be reported."""
    linux = tmp_path / "linux.log"
    windows = tmp_path / "windows.log"
    _write(linux, [
        "10:00:00.100 LINUX BTN A        press",
        "10:00:00.150 LINUX BTN A        release",
    ])
    _write(windows, [
        "10:00:00.150 WIN    PAD A        press",
    ])

    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "collect_input_trace.py"),
         "--windows-trace", str(windows), "--linux-trace", str(linux),
         "--out", str(tmp_path / "merged.txt")],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "never released" in result.stdout, \
        f"the stuck button was not reported:\n{result.stdout}"
    assert "never reached the Windows side" in result.stdout, \
        f"the lost release was not reported:\n{result.stdout}"


def test_the_collector_is_quiet_when_everything_is_fine(tmp_path):
    """A clean run must not report a problem."""
    linux = tmp_path / "linux.log"
    windows = tmp_path / "windows.log"
    _write(linux, [
        "10:00:00.100 LINUX BTN A        press",
        "10:00:00.150 LINUX BTN A        release",
    ])
    _write(windows, [
        "10:00:00.150 WIN    PAD A        press",
        "10:00:00.151 WIN    PAD A        release",
    ])

    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "collect_input_trace.py"),
         "--windows-trace", str(windows), "--linux-trace", str(linux),
         "--out", str(tmp_path / "merged.txt")],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "no stuck button found" in result.stdout
