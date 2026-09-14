"""Emits virtual Xbox controller state via vgamepad (wraps ViGEmBus on Windows).

    pip install vgamepad
    Also install ViGEmBus driver: https://github.com/ViGEm/ViGEmBus/releases
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("emitter")

_VGAMEPAD_AVAILABLE = False

try:
    import vgamepad as _vg
    _VGAMEPAD_AVAILABLE = True
    logger.debug("vgamepad loaded OK")
except ImportError:
    _vg = None
    logger.debug("vgamepad not installed — virtual controller disabled")


class XInputEmitter:
    """Sends XInput reports to ViGEmBus via vgamepad."""

    def __init__(self, slot: int = 0, on_rumble=None, config=None):
        # slot parameter is accepted for API compat
        # vgamepad uses a single default slot internally
        if not _VGAMEPAD_AVAILABLE:
            raise ImportError(
                "vgamepad not installed. Run:\n"
                "  pip install vgamepad\n"
                "Also install ViGEmBus driver from: https://github.com/ViGEm/ViGEmBus/releases"
            )

        self._pad = _vg.VX360Gamepad()
        self._active = True
        self._on_rumble = on_rumble
        self._config = config
        self._last_rumble = (0, 0)
        self._rumble_lock = threading.Lock()
        self._pad_lock = threading.Lock()
        self._rumble_enabled = True
        if config is not None:
            try:
                self._rumble_enabled = config.get_bool("controller.rumble_enabled")
            except Exception:
                pass

        # Register the rumble notification callback so the host's XInput
        # rumble commands reach us. The callback signature is fixed by vgamepad.
        if on_rumble is not None:
            try:
                # Bind `self` as the first arg so the staticmethod matches
                # the vgamepad-required signature.
                import functools
                bound = functools.partial(self._rumble_callback, self)
                self._pad.register_notification(bound)
            except Exception as exc:
                logger.warning("Could not register rumble callback: %s", exc)

        # Build button maps at runtime (vgamepad must be imported first)
        XUSB = _vg.XUSB_BUTTON
        self._BTN_MAP = {
            0: XUSB.XUSB_GAMEPAD_A,
            1: XUSB.XUSB_GAMEPAD_B,
            2: XUSB.XUSB_GAMEPAD_X,
            3: XUSB.XUSB_GAMEPAD_Y,
            4: XUSB.XUSB_GAMEPAD_LEFT_SHOULDER,
            5: XUSB.XUSB_GAMEPAD_RIGHT_SHOULDER,
            6: XUSB.XUSB_GAMEPAD_BACK,
            7: XUSB.XUSB_GAMEPAD_START,
        }
        # Dpad bitmask constants (must match linux/src/controller.py)
        self._DPAD_UP    = 0x01
        self._DPAD_RIGHT = 0x02
        self._DPAD_DOWN  = 0x04
        self._DPAD_LEFT  = 0x08
        self._DPAD_BTNS  = [
            (0x01, XUSB.XUSB_GAMEPAD_DPAD_UP),
            (0x02, XUSB.XUSB_GAMEPAD_DPAD_RIGHT),
            (0x04, XUSB.XUSB_GAMEPAD_DPAD_DOWN),
            (0x08, XUSB.XUSB_GAMEPAD_DPAD_LEFT),
        ]

        logger.info("vgamepad VX360Gamepad created (slot %d)", slot)

    # Required signature for vgamepad.register_notification.
    # vgamepad does a strict `inspect.signature(callback) == signature(dummy_callback)`
    # check, so this MUST be a staticmethod (bound methods include `self` in
    # their signature and would fail the check). The instance reference is
    # captured via the default-arg trick.
    @staticmethod
    def _rumble_callback(emitter, client, target, large_motor, small_motor,
                         led_number, user_data):
        """ViGEmBus rumble notification — called from a C callback thread."""
        left  = max(0, min(255, int(large_motor)))
        right = max(0, min(255, int(small_motor)))
        with emitter._rumble_lock:
            if not emitter._rumble_enabled:
                return
            if (left, right) == emitter._last_rumble:
                return
            emitter._last_rumble = (left, right)
            cb = emitter._on_rumble
        if cb is not None:
            try:
                cb(left, right)
            except Exception as exc:
                logger.debug("on_rumble callback error: %s", exc)

    def attach(self) -> bool:
        """Called by BridgeApp to confirm the controller is ready."""
        logger.info("Virtual Xbox 360 controller attached")
        return True

    def apply(self, state: dict) -> None:
        """Translate a parsed state dict into vgamepad calls."""
        if not self._active:
            return

        # Thumbsticks (0-65535 centred at 32768) → float -1.0 to 1.0
        lx, ly = self._stick(state["lthumb_x"], state["lthumb_y"])
        rx, ry = self._stick(state["rthumb_x"], state["rthumb_y"])
        ly = -ly
        ry = -ry
        if self._cfg_bool("controller.invert_left_y", False):
            ly = -ly
        if self._cfg_bool("controller.invert_right_y", False):
            ry = -ry

        with self._pad_lock:
            pad = self._pad
            pad.reset()

            # Triggers (0-255) → float 0.0-1.0
            pad.left_trigger_float(value_float=self._trigger(state["lt"]))
            pad.right_trigger_float(value_float=self._trigger(state["rt"]))
            pad.left_joystick_float(x_value_float=lx, y_value_float=ly)
            pad.right_joystick_float(x_value_float=rx, y_value_float=ry)

            # Buttons (buttons_low bits 0-7: A, B, X, Y, LB, RB, Back, Start)
            bl = state["buttons_low"]
            for bit, xbtn in self._BTN_MAP.items():
                if (bl >> bit) & 1:
                    pad.press_button(button=xbtn)

            # L3/R3 (buttons_high bits 0/1) and Xbox/Guide button (bit 2)
            bh = state["buttons_high"]
            if (bh >> 0) & 1:
                pad.press_button(button=_vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB)
            if (bh >> 1) & 1:
                pad.press_button(button=_vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB)
            if (bh >> 2) & 1:
                pad.press_button(button=_vg.XUSB_BUTTON.XUSB_GAMEPAD_GUIDE)

            # D-pad — bitmask, each bit is independent so diagonals work
            dpad = state.get("dpad", 0)
            for bit, btn in self._DPAD_BTNS:
                if dpad & bit:
                    pad.press_button(button=btn)

            # Flush to ViGEmBus driver
            try:
                pad.update()
            except Exception as exc:
                logger.warning("vgamepad update error: %s", exc)

    def clear(self) -> None:
        """Publish a neutral report so no input remains latched."""
        if not self._active:
            return
        with self._pad_lock:
            try:
                self._pad.reset()
                self._pad.update()
            except Exception as exc:
                logger.debug("vgamepad clear error: %s", exc)

    def _cfg_int(self, name: str, default: int) -> int:
        if self._config is None:
            return default
        try:
            return self._config.get_int(name)
        except Exception:
            return default

    def _cfg_bool(self, name: str, default: bool) -> bool:
        if self._config is None:
            return default
        try:
            return self._config.get_bool(name)
        except Exception:
            return default

    def _stick(self, raw_x: int, raw_y: int) -> tuple[float, float]:
        nx = (raw_x - 32768) / 32768.0
        ny = (raw_y - 32768) / 32768.0
        dz = self._cfg_int("controller.deadzone_stick", 15) / 100.0
        if abs(nx) < dz:
            nx = 0.0
        if abs(ny) < dz:
            ny = 0.0
        return nx, ny

    def _trigger(self, raw: int) -> float:
        value = max(0, min(255, int(raw))) / 255.0
        dz = self._cfg_int("controller.deadzone_trigger", 1) / 100.0
        return 0.0 if value < dz else value

    def set_rumble_enabled(self, enabled: bool) -> None:
        """Toggle rumble forwarding at runtime (called from the web UI)."""
        with self._rumble_lock:
            self._rumble_enabled = bool(enabled)
            if not enabled:
                self._last_rumble = (0, 0)
        logger.info("Rumble forwarding %s", "enabled" if enabled else "disabled")

    def last_rumble(self) -> tuple[int, int]:
        """Return the most recent (left, right) motor speeds seen."""
        with self._rumble_lock:
            return self._last_rumble

    def detach(self) -> None:
        """Release resources."""
        if not self._active:
            return
        self._active = False
        try:
            with self._pad_lock:
                self._pad.reset()
                self._pad.update()
        except Exception as exc:
            logger.debug("vgamepad reset on detach: %s", exc)
        logger.info("Virtual controller detached")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.detach()
        return False