"""Emits virtual Xbox controller state via vgamepad (wraps ViGEmBus on Windows).

    pip install vgamepad
    Also install ViGEmBus driver: https://github.com/ViGEm/ViGEmBus/releases
"""

from __future__ import annotations

import logging

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

    def __init__(self, slot: int = 0):
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

    def attach(self) -> bool:
        """Called by BridgeApp to confirm the controller is ready."""
        logger.info("Virtual Xbox 360 controller attached")
        return True

    def apply(self, state: dict) -> None:
        """Translate a parsed state dict into vgamepad calls."""
        if not self._active:
            return

        pad = self._pad
        pad.reset()

        # Triggers (0-255) → float 0.0-1.0
        pad.left_trigger_float(value_float=state["lt"]  / 255.0)
        pad.right_trigger_float(value_float=state["rt"] / 255.0)

        # Thumbsticks (0-65535 centred at 32768) → float -1.0 to 1.0
        pad.left_joystick_float(
            x_value_float=(state["lthumb_x"] - 32768) / 32768.0,
            y_value_float=-(state["lthumb_y"] - 32768) / 32768.0,
        )
        pad.right_joystick_float(
            x_value_float=(state["rthumb_x"] - 32768) / 32768.0,
            y_value_float=-(state["rthumb_y"] - 32768) / 32768.0,
        )

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

    def detach(self) -> None:
        """Release resources."""
        if not self._active:
            return
        self._active = False
        try:
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