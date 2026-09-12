"""Push-to-talk hotkey logic, free of any OS or device dependency.

Everything here is pure: the OS layer feeds normalised key names in and acts on the events
that come out, so the whole interaction model is testable without a keyboard.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

MODIFIERS = frozenset({"ctrl", "alt", "shift", "win"})

_MODIFIER_ORDER = ("ctrl", "alt", "shift", "win")

_ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "option": "alt",
    "alt_l": "alt",
    "alt_r": "alt",
    "alt_gr": "alt",
    "altgr": "alt",
    "shift_l": "shift",
    "shift_r": "shift",
    "cmd": "win",
    "cmd_l": "win",
    "cmd_r": "win",
    "super": "win",
    "meta": "win",
    "windows": "win",
    "escape": "esc",
    "spacebar": "space",
}


def canonical_key(name: str) -> str:
    """Return the canonical lowercase name for a key or modifier token.

    Case- and whitespace-insensitive. Left/right modifier variants and the usual aliases
    (``control``, ``option``, ``cmd``, ``escape``, ``spacebar``) collapse onto the canonical
    names used everywhere else. An unknown token is returned lowercased and stripped.
    """
    token = name.strip().lower()
    return _ALIASES.get(token, token)


class Phase(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"


@dataclass(frozen=True, slots=True)
class Hotkey:
    """A push-to-talk combo: a set of modifiers plus the single key that triggers it."""

    mods: frozenset[str]
    key: str

    @classmethod
    def parse(cls, spec: str) -> Hotkey:
        """Parse ``"ctrl+alt+space"`` into a Hotkey.

        Case- and space-insensitive; aliases are resolved by :func:`canonical_key`. Raises
        ValueError when the spec has no trigger key or more than one.
        """
        mods: set[str] = set()
        trigger: str | None = None
        for part in spec.split("+"):
            token = canonical_key(part)
            if not token:
                continue
            if token in MODIFIERS:
                mods.add(token)
                continue
            if trigger is not None:
                raise ValueError(f"{spec!r} has more than one trigger key")
            trigger = token
        if trigger is None:
            raise ValueError(f"{spec!r} has no trigger key")
        return cls(mods=frozenset(mods), key=trigger)

    def __str__(self) -> str:
        parts = [mod for mod in _MODIFIER_ORDER if mod in self.mods]
        parts.append(self.key)
        return "+".join(parts)


@dataclass(frozen=True, slots=True)
class Ignore:
    """The key press changes nothing the caller has to act on."""


@dataclass(frozen=True, slots=True)
class StartRecording:
    mode: str


@dataclass(frozen=True, slots=True)
class StopRecording:
    mode: str


@dataclass(frozen=True, slots=True)
class CancelRecording:
    mode: str


type Event = Ignore | StartRecording | StopRecording | CancelRecording


class HotkeyStateMachine:
    """Turns key presses into recording events for one recording at a time.

    Recording starts when a binding's trigger key goes down while exactly that binding's
    modifiers are held, and stops when that same trigger key is released; modifiers may be
    released first, in any order. While recording or processing, every other binding is
    inert, so two recordings can never overlap. The cancel key aborts an in-flight recording
    and is ignored otherwise; after a cancel the trigger key has to be released before it can
    start anything again.
    """

    def __init__(self, bindings: Mapping[str, Hotkey], cancel: Hotkey | None) -> None:
        self._bindings = dict(bindings)
        self._cancel = cancel
        self._held: set[str] = set()
        self._pressed: set[str] = set()
        self._blocked: set[str] = set()
        self._phase = Phase.IDLE
        self._active_mode: str | None = None
        self._active_key: str | None = None

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def active_mode(self) -> str | None:
        return self._active_mode

    @property
    def held(self) -> frozenset[str]:
        """The modifiers currently held down."""
        return frozenset(self._held)

    def key_down(self, key: str) -> Ignore | StartRecording | CancelRecording:
        """Feed a key press; returns the event the caller must act on."""
        name = canonical_key(key)
        if not name:
            return Ignore()
        if name in MODIFIERS:
            self._held.add(name)
            return Ignore()

        self._pressed.add(name)

        if self._phase is Phase.RECORDING and self._matches_cancel(name):
            mode = self._active_mode or ""
            if self._active_key is not None:
                # Cancelling leaves the trigger key down: without this its auto-repeat would
                # immediately start the recording the user just threw away.
                self._blocked.add(self._active_key)
            self._clear_active()
            return CancelRecording(mode)

        if self._phase is not Phase.IDLE or name in self._blocked:
            return Ignore()

        for mode, hotkey in self._bindings.items():
            if hotkey.key == name and hotkey.mods == self._held:
                self._phase = Phase.RECORDING
                self._active_mode = mode
                self._active_key = name
                return StartRecording(mode)
        return Ignore()

    def key_up(self, key: str) -> Ignore | StopRecording:
        """Feed a key release; only the active recording's trigger key stops it."""
        name = canonical_key(key)
        if not name:
            return Ignore()
        if name in MODIFIERS:
            self._held.discard(name)
            return Ignore()
        self._blocked.discard(name)
        if name not in self._pressed:
            return Ignore()
        self._pressed.discard(name)
        if self._phase is Phase.RECORDING and name == self._active_key:
            mode = self._active_mode or ""
            return StopRecording(mode)
        return Ignore()

    def processing_started(self) -> None:
        """Mark the captured audio as handed off; further hotkeys stay inert until done."""
        if self._phase is Phase.RECORDING:
            self._phase = Phase.PROCESSING

    def processing_finished(self) -> None:
        """Return to idle. Safe to call from an error path in any phase."""
        self._clear_active()

    def reset(self) -> None:
        """Drop all state, including which keys are believed to be held."""
        self._held.clear()
        self._pressed.clear()
        self._blocked.clear()
        self._clear_active()

    def _matches_cancel(self, name: str) -> bool:
        if self._cancel is None or name != self._cancel.key:
            return False
        # Subset, not exact: cancel is pressed while the recording combo is still held down.
        return self._cancel.mods <= self._held

    def _clear_active(self) -> None:
        self._phase = Phase.IDLE
        self._active_mode = None
        self._active_key = None
