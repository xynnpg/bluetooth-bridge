"""Emits virtual Xbox controller state via vgamepad (wraps ViGEmBus on Windows).

    pip install vgamepad
    Also install ViGEmBus driver: https://github.com/ViGEm/ViGEmBus/releases
"""

from __future__ import annotations

import logging
import threading
import time

from . import trace

logger = logging.getLogger("emitter")

#: How often the emitter re-asserts the complete state (every button that
#: is held is pressed again).  The virtual pad normally only receives the
#: transitions, but a periodic full report protects against the driver
#: losing a report — without it the pad could drift away from the
#: physical controller until the next press.
_REASSERT_INTERVAL_S = 5.0

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
        # Tracked button state so a lost release event on the wire is
        # self-corrected by the next packet.  Without this, an apply()
        # pattern of "reset + press(set bits) + update" leaves any
        # previously-pressed button latched if the release packet is
        # dropped (WiFi jitter, TCP retransmit, momentary stall).
        self._last_buttons_low  = 0
        self._last_buttons_high = 0
        self._last_dpad         = 0
        self._last_reassert_at  = time.monotonic()
        # Trace state: the last reported button/D-pad signature and the
        # time of the last trace line (for the periodic heartbeat).
        self._trace_sig: tuple | None = None
        self._trace_at = 0.0
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
        # Bit POSITION (0..3) is the dict key so the change mask can be
        # shifted by the position consistently with _BTN_MAP.
        self._DPAD_BTNS  = [
            (0, XUSB.XUSB_GAMEPAD_DPAD_UP),
            (1, XUSB.XUSB_GAMEPAD_DPAD_RIGHT),
            (2, XUSB.XUSB_GAMEPAD_DPAD_DOWN),
            (3, XUSB.XUSB_GAMEPAD_DPAD_LEFT),
        ]
        # L3 / R3 / Guide live in buttons_high (bits 0-2).
        self._HIGH_BTNS = {
            0: XUSB.XUSB_GAMEPAD_LEFT_THUMB,
            1: XUSB.XUSB_GAMEPAD_RIGHT_THUMB,
            2: XUSB.XUSB_GAMEPAD_GUIDE,
        }
        # Human readable names for the trace log.
        self._BUTTON_NAMES = {
            XUSB.XUSB_GAMEPAD_A: "A",
            XUSB.XUSB_GAMEPAD_B: "B",
            XUSB.XUSB_GAMEPAD_X: "X",
            XUSB.XUSB_GAMEPAD_Y: "Y",
            XUSB.XUSB_GAMEPAD_LEFT_SHOULDER:  "LB",
            XUSB.XUSB_GAMEPAD_RIGHT_SHOULDER: "RB",
            XUSB.XUSB_GAMEPAD_BACK:            "Back",
            XUSB.XUSB_GAMEPAD_START:           "Start",
            XUSB.XUSB_GAMEPAD_LEFT_THUMB:      "L3",
            XUSB.XUSB_GAMEPAD_RIGHT_THUMB:     "R3",
            XUSB.XUSB_GAMEPAD_GUIDE:           "Guide",
            XUSB.XUSB_GAMEPAD_DPAD_UP:         "DPadUp",
            XUSB.XUSB_GAMEPAD_DPAD_RIGHT:      "DPadRight",
            XUSB.XUSB_GAMEPAD_DPAD_DOWN:       "DPadDown",
            XUSB.XUSB_GAMEPAD_DPAD_LEFT:       "DPadLeft",
        }

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
        """Translate a parsed state dict into vgamepad calls.

        Every frame is an authoritative snapshot of the physical controller.
        The emitter remembers which buttons it has already reported and
        issues an explicit ``press_button()`` for every button that became
        held and an explicit ``release_button()`` for every button that was
        let go of.

        This is what makes a lost release harmless: if the frame that
        carried the release never arrives, the *next* frame still says
        "not pressed" and the emitter releases the button then.  Relying on
        ``pad.reset()`` to release buttons instead (the old behaviour) left
        the button latched in the game until some later packet happened to
        reset the pad -- the "I let go of the button and it is still held"
        symptom.
        """
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

        bl   = state["buttons_low"]   & 0xFF
        bh   = state["buttons_high"]  & 0xFF
        dpad = state.get("dpad", 0)   & 0x0F

        with self._pad_lock:
            pad = self._pad

            now = time.monotonic()

            # Every few seconds the complete state is re-asserted: the pad
            # is reset and every button that is held is pressed again, so
            # the virtual pad can never drift away from the physical one.
            reassert = (now - self._last_reassert_at) >= _REASSERT_INTERVAL_S
            if reassert:
                self._last_reassert_at = now
                try:
                    pad.reset()
                except Exception as exc:
                    logger.warning("vgamepad reset error: %s", exc)

            # Triggers (0-255) → float 0.0-1.0
            pad.left_trigger_float(value_float=self._trigger(state["lt"]))
            pad.right_trigger_float(value_float=self._trigger(state["rt"]))
            pad.left_joystick_float(x_value_float=lx, y_value_float=ly)
            pad.right_joystick_float(x_value_float=rx, y_value_float=ry)

            self._log_button_deltas(bl, self._last_buttons_low, self._BTN_MAP)
            self._log_high_deltas(bh, self._last_buttons_high)

            # Explicit transitions.  ``pad.reset()`` is deliberately NOT
            # used per frame: it would clear the buttons we are holding
            # and only the newly pressed ones would be re-asserted.
            if reassert:
                self._press_button_mask(bl, self._BTN_MAP)
                self._press_high_buttons(bh)
                self._press_button_mask(dpad, dict(self._DPAD_BTNS))
            else:
                self._sync_buttons(bl, self._BTN_MAP, self._last_buttons_low)
                self._sync_high_buttons(bh, self._last_buttons_high)
                self._sync_buttons(dpad, dict(self._DPAD_BTNS), self._last_dpad)

            self._last_buttons_low  = bl
            self._last_buttons_high = bh
            self._last_dpad         = dpad
            self._trace_state(bl, bh, dpad, state)

            # Flush to ViGEmBus driver
            try:
                pad.update()
            except Exception as exc:
                logger.warning("vgamepad update error: %s", exc)

    def _trace_state(self, bl: int, bh: int, dpad: int, state: dict) -> None:
        """Write a trace line with the state that is applied to the pad.

        A line is written whenever the button / D-pad state changes and
        once per second otherwise.
        """
        sig = (bl, bh, dpad)
        now = time.monotonic()
        if sig == self._trace_sig and now - self._trace_at < 1.0:
            return
        self._trace_sig = sig
        self._trace_at = now
        trace.state("PAD state", bl, bh, dpad,
                    int(self._trigger(state["lt"]) * 255),
                    int(self._trigger(state["rt"]) * 255))

    def _log_button_deltas(self, new_mask: int, last_mask: int,
                           bit_to_xbtn: dict) -> None:
        """Log transitions without making the virtual report delta-based."""
        changed = (new_mask ^ last_mask) & 0xFF
        if not changed:
            return
        for bit, xbtn in bit_to_xbtn.items():
            if not (changed >> bit) & 1:
                continue
            if (new_mask >> bit) & 1:
                logger.info("button press bit=%d", bit)
            else:
                logger.info("button release bit=%d", bit)

    def _press_button_mask(self, mask: int, bit_to_xbtn: dict) -> None:
        """Press every button that is set in *mask*."""
        for bit, xbtn in bit_to_xbtn.items():
            if (mask >> bit) & 1:
                self._pad.press_button(button=xbtn)

    def _sync_buttons(self, mask: int, bit_to_xbtn: dict, last_mask: int) -> None:
        """Press/release the buttons that changed since the previous frame.

        Only transitions are reported: a button that is still held is not
        pressed again, and a button that was already released is not
        released again.
        """
        for bit, xbtn in bit_to_xbtn.items():
            was = (last_mask >> bit) & 1
            now = (mask >> bit) & 1
            if now and not was:
                self._pad.press_button(button=xbtn)
                trace.event("PAD %-8s press", self._name_of(xbtn))
            elif was and not now:
                self._pad.release_button(button=xbtn)
                trace.event("PAD %-8s release", self._name_of(xbtn))

    def _name_of(self, xbtn) -> str:
        """The human readable name of a virtual button."""
        return self._BUTTON_NAMES.get(xbtn, str(xbtn))

    def _sync_high_buttons(self, mask: int, last_mask: int) -> None:
        """Same as :meth:`_sync_buttons` for L3 / R3 / Guide."""
        self._sync_buttons(mask, self._HIGH_BTNS, last_mask)

    def _release_all(self) -> None:
        """Release every button the emitter believes is still held."""
        self._sync_buttons(0x00, self._BTN_MAP, self._last_buttons_low)
        self._sync_high_buttons(0x00, self._last_buttons_high)
        self._sync_buttons(0x00, dict(self._DPAD_BTNS), self._last_dpad)

    def _log_high_deltas(self, new_mask: int, last_mask: int) -> None:
        """Log L3 / R3 / Guide transitions."""
        changed = (new_mask ^ last_mask) & 0xFF
        if not changed:
            return
        XUSB = _vg.XUSB_BUTTON
        for bit, xbtn in (
            (0, XUSB.XUSB_GAMEPAD_LEFT_THUMB),
            (1, XUSB.XUSB_GAMEPAD_RIGHT_THUMB),
            (2, XUSB.XUSB_GAMEPAD_GUIDE),
        ):
            if (changed >> bit) & 1:
                if (new_mask >> bit) & 1:
                    logger.info("button press high-bit=%d", bit)
                else:
                    logger.info("button release high-bit=%d", bit)

    def _press_high_buttons(self, mask: int) -> None:
        """Press the L3 / R3 / Guide buttons that are set in *mask*."""
        for bit, xbtn in self._HIGH_BTNS.items():
            if (mask >> bit) & 1:
                self._pad.press_button(button=xbtn)

    def clear(self) -> None:
        """Publish a neutral report so no input remains latched.

        The buttons are released *explicitly* first: ``pad.reset()`` only
        rebuilds the report, it does not guarantee that the driver lets go
        of a button that was pressed before.  Releasing first makes the pad
        neutral in every case.
        """
        if not self._active:
            return
        with self._pad_lock:
            self._release_all()
            trace.event("PAD clear — releasing every button")
            try:
                self._pad.reset()
                self._pad.update()
            except Exception as exc:
                logger.debug("vgamepad clear error: %s", exc)
            # The pad is now zeroed — sync our tracked button state to
            # match so the next apply() doesn't see phantom deltas.
            self._last_buttons_low  = 0
            self._last_buttons_high = 0
            self._last_dpad         = 0

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
                self._release_all()
                self._pad.reset()
                self._pad.update()
        except Exception as exc:
            logger.debug("vgamepad reset on detach: %s", exc)
        self._last_buttons_low  = 0
        self._last_buttons_high = 0
        self._last_dpad         = 0
        logger.info("Virtual controller detached")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.detach()
        return False