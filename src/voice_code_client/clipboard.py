from __future__ import annotations

import contextlib
import ctypes
import logging
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

logger = logging.getLogger(__name__)

CF_UNICODETEXT = 13

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_RETURN = 0x0D
VK_INSERT = 0x2D
VK_V = 0x56
VK_LWIN = 0x5B

_MODIFIER_VKS: dict[str, int] = {
    "ctrl": VK_CONTROL,
    "control": VK_CONTROL,
    "shift": VK_SHIFT,
    "alt": VK_MENU,
    "option": VK_MENU,
    "win": VK_LWIN,
    "super": VK_LWIN,
    "meta": VK_LWIN,
    "cmd": VK_LWIN,
}

_KEY_VKS: dict[str, int] = {
    "v": VK_V,
    "insert": VK_INSERT,
    "ins": VK_INSERT,
}

_CLIPBOARD_ATTEMPTS = 5
_CLIPBOARD_RETRY_DELAY_S = 0.04

_WORD = ctypes.c_uint16
_DWORD = ctypes.c_uint32
_LONG = ctypes.c_int32
_ULONG_PTR = ctypes.c_size_t


class ClipboardError(RuntimeError):
    """A clipboard or synthetic-keystroke operation failed."""


class _KeyboardInput(ctypes.Structure):
    _fields_ = (
        ("wVk", _WORD),
        ("wScan", _WORD),
        ("dwFlags", _DWORD),
        ("time", _DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    )


class _MouseInput(ctypes.Structure):
    _fields_ = (
        ("dx", _LONG),
        ("dy", _LONG),
        ("mouseData", _DWORD),
        ("dwFlags", _DWORD),
        ("time", _DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    )


class _HardwareInput(ctypes.Structure):
    _fields_ = (
        ("uMsg", _DWORD),
        ("wParamL", _WORD),
        ("wParamH", _WORD),
    )


class _InputUnion(ctypes.Union):
    # MOUSEINPUT is the largest member and SendInput fails unless cbSize equals the size of
    # the full INPUT union, so the members we never send must stay declared.
    _fields_ = (
        ("ki", _KeyboardInput),
        ("mi", _MouseInput),
        ("hi", _HardwareInput),
    )


class _Input(ctypes.Structure):
    _fields_ = (
        ("type", _DWORD),
        ("union", _InputUnion),
    )


def parse_shortcut(spec: str) -> tuple[list[int], int]:
    """Parse a paste shortcut such as "ctrl+v" or "shift+insert".

    Returns (modifier virtual-key codes in the written order, trigger virtual-key code).
    Case and surrounding spaces are ignored. Raises ClipboardError for an empty spec, an
    unknown part, a missing or repeated trigger key, or a spec without any modifier.
    Pure logic: importable and callable on any operating system.
    """
    parts = [part.strip().lower() for part in spec.split("+")]
    parts = [part for part in parts if part]
    if not parts:
        raise ClipboardError(f"empty paste shortcut: {spec!r}")

    modifiers: list[int] = []
    trigger: int | None = None
    for part in parts:
        if part in _MODIFIER_VKS:
            vk = _MODIFIER_VKS[part]
            if vk not in modifiers:
                modifiers.append(vk)
            continue
        if part not in _KEY_VKS:
            raise ClipboardError(f"unsupported key {part!r} in paste shortcut {spec!r}")
        if trigger is not None:
            raise ClipboardError(f"paste shortcut {spec!r} has more than one trigger key")
        trigger = _KEY_VKS[part]
    if trigger is None:
        raise ClipboardError(f"paste shortcut {spec!r} has no trigger key")
    if not modifiers:
        raise ClipboardError(f"paste shortcut {spec!r} has no modifier key")
    return modifiers, trigger


def get_text() -> str | None:
    """Return the clipboard's Unicode text, or None when it holds no text.

    Raises ClipboardError when the clipboard stays locked by another process.
    """
    module = _clipboard_module()

    def read() -> str | None:
        module.OpenClipboard()
        try:
            if not module.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return None
            data = module.GetClipboardData(CF_UNICODETEXT)
        finally:
            module.CloseClipboard()
        return data if isinstance(data, str) else None

    return _retry(read, "read the clipboard")


def set_text(text: str) -> None:
    """Replace the clipboard contents with text as CF_UNICODETEXT.

    Retries a few times because other processes routinely hold the clipboard open for a
    few milliseconds. Raises ClipboardError when every attempt fails.
    """
    module = _clipboard_module()

    def write() -> None:
        module.OpenClipboard()
        try:
            module.EmptyClipboard()
            module.SetClipboardText(text, CF_UNICODETEXT)
        finally:
            module.CloseClipboard()

    _retry(write, "write the clipboard")


def send_paste(shortcut: str) -> None:
    """Synthesise shortcut (e.g. "ctrl+v") into the focused window with SendInput.

    Raises ClipboardError when the shortcut is unsupported or Windows refuses the
    injection, which happens when the focused window runs elevated and this process does
    not.
    """
    modifiers, trigger = parse_shortcut(shortcut)
    events: list[tuple[int, bool]] = [(vk, False) for vk in modifiers]
    events.append((trigger, False))
    events.append((trigger, True))
    events.extend((vk, True) for vk in reversed(modifiers))
    _send_keys(events)


def send_enter() -> None:
    """Press and release Enter in the focused window."""
    _send_keys([(VK_RETURN, False), (VK_RETURN, True)])


def foreground_window() -> int | None:
    """Return the focused window handle, or None when there is none or the call fails."""
    try:
        user32 = _user32()
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        handle = user32.GetForegroundWindow()
    except Exception:
        logger.debug("GetForegroundWindow failed", exc_info=True)
        return None
    return int(handle) if handle else None


def window_title(hwnd: int) -> str:
    """Return the window's title, or an empty string when it has none or the call fails."""
    try:
        user32 = _user32()
        user32.GetWindowTextLengthW.argtypes = (ctypes.c_void_p,)
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        length = int(user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd)))
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int)
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowTextW(ctypes.c_void_p(hwnd), buffer, length + 1)
    except Exception:
        logger.debug("GetWindowTextW failed", exc_info=True)
        return ""
    return buffer.value


@contextlib.contextmanager
def preserved_clipboard(enabled: bool, restore_delay_ms: int) -> Iterator[None]:
    """Capture the clipboard text on entry and put it back on exit.

    The restore happens restore_delay_ms after the body finishes, so the target window has
    time to consume the paste. With enabled false this does nothing. Capture and restore
    failures are logged and swallowed: they must never fail the delivery.
    """
    previous: str | None = None
    if enabled:
        try:
            previous = get_text()
        except Exception:
            logger.debug("could not capture the previous clipboard", exc_info=True)
    try:
        yield
    finally:
        if enabled and previous is not None:
            if restore_delay_ms > 0:
                time.sleep(restore_delay_ms / 1000.0)
            try:
                set_text(previous)
            except Exception:
                logger.warning("could not restore the previous clipboard", exc_info=True)


def _clipboard_module() -> Any:
    try:
        import win32clipboard
    except ImportError as exc:
        raise ClipboardError("pywin32 is unavailable: the clipboard needs Windows") from exc
    return win32clipboard


def _user32() -> Any:
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        raise ClipboardError("user32 is unavailable: synthetic input needs Windows")
    return windll.user32


def _retry[T](action: Callable[[], T], description: str) -> T:
    last: Exception | None = None
    for attempt in range(_CLIPBOARD_ATTEMPTS):
        try:
            return action()
        except Exception as exc:
            last = exc
            if attempt + 1 < _CLIPBOARD_ATTEMPTS:
                time.sleep(_CLIPBOARD_RETRY_DELAY_S)
    raise ClipboardError(f"could not {description} after {_CLIPBOARD_ATTEMPTS} attempts") from last


def _send_keys(events: Sequence[tuple[int, bool]]) -> None:
    user32 = _user32()
    buffer = (_Input * len(events))()
    for index, (vk, is_up) in enumerate(events):
        buffer[index].type = INPUT_KEYBOARD
        buffer[index].union.ki.wVk = vk
        buffer[index].union.ki.wScan = 0
        buffer[index].union.ki.dwFlags = KEYEVENTF_KEYUP if is_up else 0
        buffer[index].union.ki.time = 0
        buffer[index].union.ki.dwExtraInfo = 0
    user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_Input), ctypes.c_int)
    user32.SendInput.restype = ctypes.c_uint
    sent = int(user32.SendInput(len(events), buffer, ctypes.sizeof(_Input)))
    if sent != len(events):
        raise ClipboardError(f"SendInput delivered {sent} of {len(events)} key events")
