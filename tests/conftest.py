"""Pytest fixtures for the bluetooth-bridge regression suite.

The bridge ships two source trees that both use the package name ``src``:

    linux/src/*.py
    windows/src/*.py

so they cannot be imported side by side.  Every module is therefore loaded
from its file path (``importlib.util.spec_from_file_location``); the Windows
``main.py`` needs its siblings for its relative imports, so it is loaded as a
synthetic package instead.

``evdev`` and ``vgamepad`` are not installed in the test environment — the
fakes from :mod:`tests.fakes` are injected into ``sys.modules`` before the
module under test is imported.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import socket
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LINUX_SRC = ROOT / "linux" / "src"
WINDOWS_SRC = ROOT / "windows" / "src"

sys.path.insert(0, str(Path(__file__).resolve().parent))


# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------

def load_module(name: str, path: Path) -> types.ModuleType:
    """Load a source file as a stand-alone module."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses/typing can resolve the module.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_package(alias: str, package_dir: Path) -> types.ModuleType:
    """Create a synthetic package whose ``__path__`` is *package_dir*."""
    pkg = types.ModuleType(alias)
    pkg.__path__ = [str(package_dir)]
    sys.modules[alias] = pkg
    return pkg


def load_package_module(alias: str, package_dir: Path, module: str) -> types.ModuleType:
    """Import ``<alias>.<module>`` from *package_dir*.

    Any previously imported module of the same alias is dropped first so that
    every test gets a pristine copy of the source under test.
    """
    for name in [n for n in sys.modules
                 if n == alias or n.startswith(alias + ".")]:
        del sys.modules[name]
    load_package(alias, package_dir)
    return importlib.import_module(f"{alias}.{module}")


def call_with_timeout(func, timeout: float = 2.0, what: str = "call"):
    """Run *func* in a worker thread and fail the test if it hangs.

    Several code paths in the bridge are blocking by design (``select()`` on
    the evdev fd, ``recv()`` on the TCP socket).  Running them in a thread
    turns "this never returns" into a clean test failure instead of a hung
    test session.
    """
    box: dict = {}

    def runner():
        try:
            box["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise AssertionError(f"{what} did not return within {timeout:.2f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------------------------------------------------------------------
# evdev fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_evdev(monkeypatch):
    """Install a fake ``evdev`` module and return it."""
    from fakes import make_fake_evdev

    module = make_fake_evdev()
    monkeypatch.setitem(sys.modules, "evdev", module)
    return module


@pytest.fixture
def controller(fake_evdev):
    """``linux/src/controller.py`` loaded with the fake evdev in place.

    It is imported as part of a synthetic package so that its relative
    imports (the trace helper) work exactly like in production.
    """
    return load_package_module("bt_linux_ctrl_pkg", LINUX_SRC, "controller")


@pytest.fixture
def network():
    """``linux/src/network.py`` (as part of a synthetic package)."""
    return load_package_module("bt_linux_net_pkg", LINUX_SRC, "network")


@pytest.fixture
def linux_main(fake_evdev):
    """``linux/src/main.py`` (imported as part of a synthetic package)."""
    return load_package_module("bt_linux_pkg", LINUX_SRC, "main")


# ---------------------------------------------------------------------------
# vgamepad fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_vgamepad(monkeypatch):
    """Install a fake ``vgamepad`` module and return it."""
    from fakes import make_fake_vgamepad

    module = make_fake_vgamepad()
    monkeypatch.setitem(sys.modules, "vgamepad", module)
    return module


@pytest.fixture
def emitter(fake_vgamepad):
    """``windows/src/emitter.py`` loaded with the fake vgamepad in place.

    It is imported as part of a synthetic package so that its relative
    imports (the trace helper) work exactly like in production.
    """
    return load_package_module("bt_windows_emit_pkg", WINDOWS_SRC, "emitter")


@pytest.fixture
def receiver():
    """``windows/src/receiver.py`` (as part of a synthetic package)."""
    return load_package_module("bt_windows_recv_pkg", WINDOWS_SRC, "receiver")


@pytest.fixture
def windows_main(fake_vgamepad):
    """``windows/src/main.py`` (imported as part of a synthetic package)."""
    return load_package_module("bt_windows_pkg", WINDOWS_SRC, "main")


# ---------------------------------------------------------------------------
# Trace helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def linux_trace(tmp_path, monkeypatch):
    """The Linux trace module, writing into a temporary file."""
    target = tmp_path / "linux-input-trace.log"
    monkeypatch.setenv("TRACE_FILE", str(target))
    module = load_package_module("bt_linux_trace_pkg", LINUX_SRC, "trace")
    module.setup(str(target))
    yield module
    handler = getattr(module, "_handler", None)
    if handler is not None:
        handler.close()


@pytest.fixture
def windows_trace(tmp_path, monkeypatch):
    """The Windows trace module, writing into a temporary file."""
    target = tmp_path / "windows-input-trace.log"
    monkeypatch.setenv("TRACE_FILE", str(target))
    module = load_package_module("bt_windows_trace_pkg", WINDOWS_SRC, "trace")
    module.setup(str(target))
    yield module
    handler = getattr(module, "_handler", None)
    if handler is not None:
        handler.close()


# ---------------------------------------------------------------------------
# Networking helpers
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def listening_receiver(receiver, host: str = "127.0.0.1", port: int = 0):
    """Start *receiver* and yield the port it listens on."""
    receiver.start()
    deadline = time.monotonic() + 5.0
    chosen = None
    while time.monotonic() < deadline:
        listener = getattr(receiver, "_listener", None)
        if listener is not None:
            try:
                chosen = listener.getsockname()[1]
            except OSError:
                chosen = None
            if chosen:
                break
        time.sleep(0.002)
    assert chosen, "receiver did not start listening"
    try:
        yield chosen
    finally:
        receiver.stop()


@contextlib.contextmanager
def tcp_client(receiver, host: str = "127.0.0.1"):
    """Start *receiver* and yield a connected client socket."""
    with listening_receiver(receiver, host=host) as port:
        client = socket.create_connection((host, port), timeout=5.0)
        client.settimeout(5.0)
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and getattr(receiver, "_conn", None) is None:
                time.sleep(0.002)
            yield client
        finally:
            try:
                client.close()
            except OSError:
                pass


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.005) -> bool:
    """Poll *predicate* until it is true or *timeout* expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
