"""Backend selection for rumble.

Bluetooth controllers expose a hidraw node on many kernels, but raw
output reports through that node do not reliably reach the motors.  The
kernel force-feedback path is the canonical one for Bluetooth, so it must
be tried first; USB still prefers hidraw.
"""

from __future__ import annotations

import types

import pytest


class _FakeRumble:
    def __init__(self, working_paths):
        self.working_paths = set(working_paths)
        self.calls: list[str] = []

    def set_device(self, path: str) -> bool:
        self.calls.append(path)
        return path in self.working_paths


def _device(bustype: int, phys: str = "", path: str = "/dev/input/event7"):
    return types.SimpleNamespace(
        path=path,
        phys=phys,
        name="Xbox Wireless Controller",
        uniq="44:16:22:15:A1:31",
        info=types.SimpleNamespace(bustype=bustype),
    )


def _app(linux_main, bustype: int, phys: str = ""):
    app = linux_main.BridgeApp()
    app._device = _device(bustype, phys)
    app._controller_mac = ""
    return app


def test_bluetooth_prefers_force_feedback(linux_main, monkeypatch):
    app = _app(linux_main, bustype=0x0005, phys="5c:3a:45:21:3e:a4")
    rumble = _FakeRumble({"/dev/input/event7", "/dev/hidraw1"})
    app._rumble = rumble
    monkeypatch.setattr(linux_main, "find_force_feedback_for",
                        lambda dev: "/dev/input/event7")
    monkeypatch.setattr(linux_main, "find_hidraw_for",
                        lambda path: "/dev/hidraw1")

    app._open_rumble()

    assert rumble.calls == ["/dev/input/event7"], (
        "Bluetooth must not choose hidraw before the FF backend"
    )


def test_usb_prefers_hidraw(linux_main, monkeypatch):
    app = _app(linux_main, bustype=0x0003)
    rumble = _FakeRumble({"/dev/input/event7", "/dev/hidraw1"})
    app._rumble = rumble
    monkeypatch.setattr(linux_main, "find_force_feedback_for",
                        lambda dev: "/dev/input/event7")
    monkeypatch.setattr(linux_main, "find_hidraw_for",
                        lambda path: "/dev/hidraw1")

    app._open_rumble()

    assert rumble.calls == ["/dev/hidraw1"]


def test_bluetooth_falls_back_to_hidraw_when_ff_is_unusable(
        linux_main, monkeypatch):
    app = _app(linux_main, bustype=0x0005)
    rumble = _FakeRumble({"/dev/hidraw1"})
    app._rumble = rumble
    monkeypatch.setattr(linux_main, "find_force_feedback_for",
                        lambda dev: "/dev/input/event7")
    monkeypatch.setattr(linux_main, "find_hidraw_for",
                        lambda path: "/dev/hidraw1")

    app._open_rumble()

    assert rumble.calls == ["/dev/input/event7", "/dev/hidraw1"]


def test_no_backend_is_logged_and_rumble_stays_disabled(
        linux_main, monkeypatch, caplog):
    app = _app(linux_main, bustype=0x0005)
    rumble = _FakeRumble(set())
    app._rumble = rumble
    monkeypatch.setattr(linux_main, "find_force_feedback_for",
                        lambda dev: None)
    monkeypatch.setattr(linux_main, "find_hidraw_for",
                        lambda path: None)

    app._open_rumble()

    assert rumble.calls == []
    assert "No rumble backend available" in caplog.text


class _PulseRumble:
    available = True

    def __init__(self):
        self.values: list[tuple[int, int]] = []

    def apply(self, left: int, right: int) -> None:
        self.values.append((left, right))

    def describe(self) -> dict:
        return {"backend": "force feedback", "path": "/dev/input/event7"}


def test_linux_self_rumble_pulses_then_stops(linux_main):
    import time

    app = linux_main.BridgeApp()
    rumble = _PulseRumble()
    app._rumble = rumble

    status = app.test_rumble(left=180, right=90, duration=0.05)

    assert status["backend"] == "force feedback"
    assert rumble.values[:2] == [(0, 0), (180, 90)]
    time.sleep(0.12)
    assert rumble.values[-1] == (0, 0)


def test_marked_rumble_packet_runs_the_linux_self_test(linux_main):
    import time

    app = linux_main.BridgeApp()
    rumble = _PulseRumble()
    app._rumble = rumble
    app._state = types.SimpleNamespace(rumble_left=0, rumble_right=0)

    packet = bytearray(54)
    packet[16] = 200
    packet[17] = 120
    packet[40:48] = b"RUMBTEST"

    app._on_recv_packet(bytes(packet))

    assert rumble.values[:2] == [(0, 0), (200, 120)]
    time.sleep(1.2)
    assert rumble.values[-1] == (0, 0)
