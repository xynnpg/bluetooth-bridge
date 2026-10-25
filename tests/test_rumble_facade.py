"""Tests for the rumble writer facade and backend dispatch.

The two backends (HidrawRumbleWriter and ForceFeedbackRumbleWriter) talk
to real kernel / hidraw devices, so we don't exercise them end-to-end
here.  Instead we test the facade's dispatch logic and the
``is_force_feedback_available`` probe against a fake evdev module.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _load_rumble_with_fake_evdev(evdev_module):
    """Load rumble.py with ``evdev`` redirected to a fake module.

    Returns the loaded module.
    """
    sys.modules["evdev"] = evdev_module
    spec = importlib.util.spec_from_file_location(
        "rumble_under_test", ROOT / "linux" / "src" / "rumble.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_fake_evdev(*, has_ff: bool, has_rumble: bool, open_raises: bool = False):
    """Build a minimal fake evdev module for the test.

    The fake exposes:
    - ``ecodes`` with the codes the module looks up
    - ``InputDevice`` that records ``upload_effect``/``erase_effect``/``close`` calls
    - ``ff.Effect`` / ``ff.Rumble`` / etc. as plain attribute containers

    It is intentionally NOT a faithful evdev mock — the rumble code only
    touches a small surface area.
    """
    fake = types.ModuleType("evdev")

    class EvdevError(Exception):
        pass

    fake.EvdevError = EvdevError

    class _Codes:
        EV_FF = 0x15
        FF_RUMBLE = 0x50

        @staticmethod
        def get(code, default=None):
            return default
    fake.ecodes = _Codes

    class _InputDevice:
        instances: list = []

        def __init__(self, path):
            self.path = path
            self.name = "Xbox Wireless Controller"
            self.phys = ""
            self.uniq = ""
            self.uploads: list = []
            self.erased: list = []
            self.writes: list = []
            self.closed = False
            if open_raises:
                raise FileNotFoundError(f"fake: cannot open {path}")
            _InputDevice.instances.append(self)

        def capabilities(self, absinfo=True):
            codes = [_Codes.FF_RUMBLE] if has_rumble else []
            return {_Codes.EV_FF: codes}

        def upload_effect(self, effect):
            # Mimic the kernel: a new id is allocated only for id == -1;
            # re-uploading an existing id updates the effect in place.
            if effect.id == -1:
                effect.id = len(self.uploads) + 1
            self.uploads.append(effect)
            return effect.id

        def erase_effect(self, ff_id):
            self.erased.append(ff_id)

        def write(self, etype, code, value):
            self.writes.append((etype, code, value))

        def close(self):
            self.closed = True

    fake.InputDevice = _InputDevice
    fake.list_devices = staticmethod(
        lambda: [dev.path for dev in _InputDevice.instances])

    # Fake ff module — provide enough that the real module can import it
    ff = types.ModuleType("evdev.ff")

    class Effect:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class EffectType:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class Trigger:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Replay:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Rumble:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    ff.Effect       = Effect
    ff.EffectType   = EffectType
    ff.Trigger      = Trigger
    ff.Replay       = Replay
    ff.Rumble       = Rumble
    fake.ff = ff
    return fake


def test_is_force_feedback_available_true_when_rumble_present():
    fake = _make_fake_evdev(has_ff=True, has_rumble=True)
    module = _load_rumble_with_fake_evdev(fake)
    assert module.is_force_feedback_available("/dev/input/event99") is True


def test_is_force_feedback_available_false_when_no_ff():
    fake = _make_fake_evdev(has_ff=False, has_rumble=False)
    module = _load_rumble_with_fake_evdev(fake)
    assert module.is_force_feedback_available("/dev/input/event99") is False


def test_is_force_feedback_available_true_with_ff_but_not_rumble():
    fake = _make_fake_evdev(has_ff=True, has_rumble=False)
    module = _load_rumble_with_fake_evdev(fake)
    # EV_FF is present but FF_RUMBLE isn't — we still report False
    # because the user wants *rumble*, not other FF effects.
    assert module.is_force_feedback_available("/dev/input/event99") is False


def test_is_force_feedback_available_false_when_device_missing():
    fake = _make_fake_evdev(has_ff=True, has_rumble=True, open_raises=True)
    module = _load_rumble_with_fake_evdev(fake)
    assert module.is_force_feedback_available("/dev/input/event99") is False


def test_find_force_feedback_for_returns_selected_device():
    fake = _make_fake_evdev(has_ff=True, has_rumble=True)
    module = _load_rumble_with_fake_evdev(fake)
    device = fake.InputDevice("/dev/input/event7")

    assert module.find_force_feedback_for(device) == "/dev/input/event7"


def test_find_force_feedback_for_returns_none_without_rumble():
    fake = _make_fake_evdev(has_ff=True, has_rumble=False)
    module = _load_rumble_with_fake_evdev(fake)
    device = fake.InputDevice("/dev/input/event7")

    assert module.find_force_feedback_for(device) is None


def test_facade_dispatches_event_path_to_ff_backend():
    fake = _make_fake_evdev(has_ff=True, has_rumble=True)
    module = _load_rumble_with_fake_evdev(fake)
    rw = module.RumbleWriter()
    assert rw.set_device("/dev/input/event7") is True
    # The InputDevice we opened should have exactly one upload (the
    # initial effect) — the kernel wrote back an id.
    dev = fake.InputDevice.instances[-1]
    assert len(dev.uploads) == 1
    assert dev.uploads[0].id >= 1  # kernel-assigned


def test_facade_dispatches_hidraw_path_to_hidraw_backend():
    fake = _make_fake_evdev(has_ff=True, has_rumble=True)
    module = _load_rumble_with_fake_evdev(fake)
    rw = module.RumbleWriter()
    # We can't actually open a hidraw node in the test env without a
    # real device, so we only verify the dispatch path: the facade
    # must NOT touch the evdev InputDevice when given a hidraw path.
    fake.InputDevice.instances.clear()
    rw.set_device("/dev/hidraw0")
    assert fake.InputDevice.instances == []
    # Backend should have been selected but _open() failed because
    # there's no real hidraw file.  The method returns False.
    assert rw._active is None


def test_facade_rejects_unknown_path():
    fake = _make_fake_evdev(has_ff=True, has_rumble=True)
    module = _load_rumble_with_fake_evdev(fake)
    rw = module.RumbleWriter()
    assert rw.set_device("/dev/something/weird") is False
    assert rw._active is None


def test_facade_apply_uses_active_backend():
    fake = _make_fake_evdev(has_ff=True, has_rumble=True)
    module = _load_rumble_with_fake_evdev(fake)
    rw = module.RumbleWriter()
    rw.set_device("/dev/input/event7")
    dev = fake.InputDevice.instances[-1]

    rw.apply(128, 64)
    # The FF writer uploads on change; the first apply should produce
    # exactly one additional upload.
    assert len(dev.uploads) == 2
    effect = dev.uploads[-1]
    assert effect.u.ff_rumble_effect.strong_magnitude == \
        round(128 * module.ForceFeedbackRumbleWriter._MAX_FF / 255)
    assert effect.u.ff_rumble_effect.weak_magnitude == \
        round(64 * module.ForceFeedbackRumbleWriter._MAX_FF / 255)
    # Uploading an effect does not play it — an EV_FF event must follow.
    assert dev.writes == [(module.evdev.ecodes.EV_FF, effect.id, 1)]


def test_ff_zero_request_sends_a_zero_effect_instead_of_only_stopping():
    fake = _make_fake_evdev(has_ff=True, has_rumble=True)
    module = _load_rumble_with_fake_evdev(fake)
    rw = module.RumbleWriter()
    rw.set_device("/dev/input/event7")
    dev = fake.InputDevice.instances[-1]
    effect_id = dev.uploads[0].id

    rw.apply(200, 100)
    assert dev.writes == [(module.evdev.ecodes.EV_FF, effect_id, 1)]

    rw.apply(0, 0)
    effect = dev.uploads[-1]
    assert effect.u.ff_rumble_effect.strong_magnitude == 0
    assert effect.u.ff_rumble_effect.weak_magnitude == 0
    # EV_FF 0 alone lets ff-memless stop combining without emitting a
    # final zero report.  Play the zero effect so the motor actually stops.
    assert dev.writes[-1] == (module.evdev.ecodes.EV_FF, effect_id, 1)
    assert (module.evdev.ecodes.EV_FF, effect_id, 0) not in dev.writes

    # A second zero frame must be deduped (and must not spam EV_FF).
    writes = list(dev.writes)
    rw.apply(0, 0)
    assert dev.writes == writes


def test_facade_apply_dedupes_unchanged_values():
    fake = _make_fake_evdev(has_ff=True, has_rumble=True)
    module = _load_rumble_with_fake_evdev(fake)
    rw = module.RumbleWriter()
    rw.set_device("/dev/input/event7")
    dev = fake.InputDevice.instances[-1]

    rw.apply(128, 64)
    assert len(dev.uploads) == 2
    rw.apply(128, 64)  # same values
    assert len(dev.uploads) == 2  # no new upload


def test_ff_write_failure_does_not_raise():
    """A read-only /dev/input must disable rumble, not crash the loop.

    python-evdev raises EvdevError (not OSError) when the fd lacks write
    access, which is what happens with a read-only container bind mount.
    """
    fake = _make_fake_evdev(has_ff=True, has_rumble=True)
    module = _load_rumble_with_fake_evdev(fake)
    rw = module.RumbleWriter()
    rw.set_device("/dev/input/event7")
    dev = fake.InputDevice.instances[-1]

    def refuse(*_args):
        raise fake.EvdevError('no write access to device "/dev/input/event7"')

    dev.write = refuse

    rw.apply(128, 64)   # upload succeeds; EV_FF play must fail cleanly
    rw.apply(0, 0)      # stop must also fail cleanly

    assert dev.closed is False
    assert rw._active is rw._ff


def test_facade_close_forces_zero_then_erases_effect():
    fake = _make_fake_evdev(has_ff=True, has_rumble=True)
    module = _load_rumble_with_fake_evdev(fake)
    rw = module.RumbleWriter()
    rw.set_device("/dev/input/event7")
    dev = fake.InputDevice.instances[-1]
    effect_id = dev.uploads[0].id
    rw.apply(255, 255)

    rw.close()
    # Closing while rumbling must upload and play a zero effect before
    # erasing it, so the motors cannot be left running.
    zero = dev.uploads[-1]
    assert zero.u.ff_rumble_effect.strong_magnitude == 0
    assert zero.u.ff_rumble_effect.weak_magnitude == 0
    assert dev.writes[-1] == (module.evdev.ecodes.EV_FF, effect_id, 1)
    assert effect_id in dev.erased
    assert dev.closed is True


def test_facade_switching_backends_closes_other_on_success():
    """When a successful switch happens, the other backend is closed."""
    fake = _make_fake_evdev(has_ff=True, has_rumble=True)
    module = _load_rumble_with_fake_evdev(fake)
    rw = module.RumbleWriter()

    # Open as event first
    rw.set_device("/dev/input/event7")
    dev1 = fake.InputDevice.instances[-1]
    assert rw._active is rw._ff

    # Now request a hidraw path.  In the test env there's no real
    # hidraw device, so this will fail — meaning the previous
    # (event) backend must remain active.  Verify that contract.
    rw.set_device("/dev/hidraw0")
    assert rw._active is rw._ff, (
        "Event backend should remain active when hidraw open fails"
    )
    assert dev1.closed is False, (
        "Event backend must NOT be closed when the new backend failed to attach"
    )


def test_facade_successful_switch_closes_other():
    """When a switch succeeds, the previous backend is closed."""
    fake = _make_fake_evdev(has_ff=True, has_rumble=True)
    module = _load_rumble_with_fake_evdev(fake)
    rw = module.RumbleWriter()

    # Open as event first
    rw.set_device("/dev/input/event7")
    dev1 = fake.InputDevice.instances[-1]
    assert rw._active is rw._ff

    # Now switch to a *different* event path (the test only has a
    # fake evdev, so we use a different input path).  This should
    # succeed and the old event backend should be closed.
    rw.set_device("/dev/input/event8")
    assert rw._active is rw._ff
    # Two input devices opened total (one for each set_device call).
    assert len(fake.InputDevice.instances) == 2
    # The first one was closed when we switched.
    assert dev1.closed is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
