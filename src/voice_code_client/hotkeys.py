from __future__ import annotations

import logging
import string
from typing import TYPE_CHECKING, Any

from .state import HotkeyStateMachine, Ignore

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "alt": "alt",
    "alt_l": "alt",
    "alt_r": "alt",
    "alt_gr": "alt",
    "shift": "shift",
    "shift_l": "shift",
    "shift_r": "shift",
    "cmd": "win",
    "cmd_l": "win",
    "cmd_r": "win",
}

_NAMED_KEYS = frozenset(
    {
        "space",
        "esc",
        "enter",
        "tab",
        "backspace",
        "delete",
        "insert",
        "home",
        "end",
        "up",
        "down",
        "left",
        "right",
    }
) | frozenset(f"f{number}" for number in range(1, 25))
#: Every trigger key normalize_key can produce. A binding naming anything else would be
#: accepted by Hotkey.parse and then simply never fire, so config validation checks it.
TRIGGER_KEYS = _NAMED_KEYS | frozenset(string.ascii_lowercase) | frozenset(string.digits)


def normalize_key(key: object) -> str | None:
    """Map a pynput key object to the canonical lowercase name used by Hotkey.

    Accepts anything that duck-types a pynput key: an object with a ``.name`` string
    (``Key``) or with ``.char``/``.vk`` attributes (``KeyCode``). pynput itself is not
    imported, so this works on machines without it.

    Left/right modifier variants collapse to "ctrl"/"alt"/"shift"/"win"; letters and
    digits come back lowercase; f1..f24 and the common navigation keys pass through by
    name. Returns None for keys the client does not track, for a ``.char`` of None
    (dead keys) and for unknown virtual-key codes.
    """
    name = getattr(key, "name", None)
    if isinstance(name, str):
        lowered = name.lower()
        modifier = _MODIFIER_ALIASES.get(lowered)
        if modifier is not None:
            return modifier
        if lowered in _NAMED_KEYS:
            return lowered
        return None

    char = getattr(key, "char", None)
    if isinstance(char, str) and len(char) == 1 and char.isascii() and char.isalnum():
        return char.lower()

    vk = getattr(key, "vk", None)
    if isinstance(vk, int):
        # On a Russian layout .char is Cyrillic (or None while a modifier is held), so the
        # physical key is recovered from the Win32 virtual-key code instead.
        if 65 <= vk <= 90:
            return chr(vk + 32)
        if 48 <= vk <= 57:
            return chr(vk)
        if 96 <= vk <= 105:
            return chr(vk - 48)
    return None


class HotkeyListener:
    """Feeds global keyboard events into a HotkeyStateMachine.

    Runs a pynput listener on its own thread with suppress=False, so key presses still
    reach the focused application. Every non-Ignore event the machine produces is passed
    to ``on_event`` on that listener thread; callback exceptions are logged and never
    propagated back into pynput, which would kill the listener.

    Synthetic (injected) key events are ignored, so the companion never reacts to the
    keystrokes it sends itself when pasting.
    """

    def __init__(self, machine: HotkeyStateMachine, on_event: Callable[[object], None]) -> None:
        self._machine = machine
        self._on_event = on_event
        self._listener: Any = None

    def start(self) -> None:
        """Start the listener thread; a second call while running does nothing.

        Raises ImportError when pynput is not installed.
        """
        if self._listener is not None:
            return
        from pynput import keyboard

        listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
            suppress=False,
        )
        listener.start()
        self._listener = listener

    def stop(self) -> None:
        """Stop the listener thread. Safe to call when not running."""
        listener, self._listener = self._listener, None
        if listener is None:
            return
        try:
            listener.stop()
        except Exception:
            logger.exception("failed to stop the keyboard listener")

    def _on_press(self, key: object, injected: bool = False) -> None:
        self._dispatch(key, pressed=True, injected=injected)

    def _on_release(self, key: object, injected: bool = False) -> None:
        self._dispatch(key, pressed=False, injected=injected)

    def _dispatch(self, key: object, *, pressed: bool, injected: bool = False) -> None:
        try:
            if injected:
                # Our own paste is SendInput'd, and the low-level hook reports it back to us.
                # Feeding that synthetic ctrl-up to the machine clears a ctrl the user is still
                # physically holding, after which the chord no longer matches and push-to-talk
                # goes dead until they let go of ctrl. pynput's win32 backend passes this flag;
                # backends that do not simply leave it False.
                return
            name = normalize_key(key)
            if name is None:
                return
            event = self._machine.key_down(name) if pressed else self._machine.key_up(name)
            if isinstance(event, Ignore):
                return
            self._on_event(event)
        except Exception:
            logger.exception("hotkey dispatch failed")
