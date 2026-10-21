import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
LINUX_SRC = ROOT / "linux" / "src"
WINDOWS_SRC = ROOT / "windows" / "src"

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import load_module, load_package_module  # noqa: E402


def load_linux_module(name: str, module: str):
    """Import a module of the Linux ``src`` package (relative imports work)."""
    return load_package_module(f"bt_{name}", LINUX_SRC, module)


def test_linux_stale_socket_cannot_detach_replacement():
    network = load_linux_module("linux_network", "network")
    streamer = network.TCPStreamer("127.0.0.1", 1)
    old_socket = object()
    new_socket = object()
    notifications = []
    streamer.on_disconnect = lambda: notifications.append(True)

    streamer._sock = new_socket
    streamer._mark_disconnected(old_socket)

    assert streamer._sock is new_socket
    assert notifications == []


def test_linux_active_socket_disconnects_once():
    network = load_linux_module("linux_network_active", "network")
    streamer = network.TCPStreamer("127.0.0.1", 1)
    notifications = []
    streamer.on_disconnect = lambda: notifications.append(True)

    class Socket:
        def setsockopt(self, *_args):
            pass

        def recv(self, _size):
            return b""

        def shutdown(self, _how):
            pass

        def close(self):
            pass

    sock = Socket()
    streamer._sock = sock
    streamer._mark_disconnected(sock)
    streamer._mark_disconnected(sock)

    assert streamer._sock is None
    assert notifications == [True]


def test_windows_stale_generation_does_not_notify_disconnect():
    receiver_module = load_package_module("bt_windows_receiver_pkg", WINDOWS_SRC, "receiver")
    receiver = receiver_module.TCPReceiver("127.0.0.1", 1, lambda _state: None)
    notifications = []
    receiver.on_disconnect = lambda address: notifications.append(address)

    class Socket:
        def setsockopt(self, *_args):
            pass

        def recv(self, _size):
            return b""

        def shutdown(self, _how):
            pass

        def close(self):
            pass

    old_socket = Socket()
    new_socket = Socket()
    receiver._conn = new_socket
    receiver._conn_generation = 2

    receiver._serve(old_socket, ("old", 0), 1)

    assert receiver._conn is new_socket
    assert notifications == []
