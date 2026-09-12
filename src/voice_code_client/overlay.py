from __future__ import annotations

import ctypes
import logging
import queue
import tkinter as tk
from collections.abc import Callable

from voice_code_client.config import OverlayConfig

logger = logging.getLogger(__name__)

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

_PUMP_INTERVAL_MS = 50
_SCREEN_MARGIN_PX = 72
_RECORDING_BULLET = "●  "

_STYLES: dict[str, tuple[str, str]] = {
    "recording": ("#3a1418", "#ff7a7a"),
    "info": ("#17191d", "#dfe3ea"),
    "ok": ("#12241a", "#7ee2a8"),
    "error": ("#2a1416", "#ffb0b0"),
}
_DEFAULT_STYLE = "info"


def _foreground_window() -> int | None:
    try:
        return int(ctypes.windll.user32.GetForegroundWindow()) or None
    except Exception:
        return None


def _restore_foreground(hwnd: int | None) -> None:
    """Hand the foreground back to ``hwnd`` if building the overlay took it away.

    Best effort: Windows refuses SetForegroundWindow from a process that is not already
    foreground, and the overlay works fine either way, so a failure is only logged.
    """
    if hwnd is None:
        return
    try:
        user32 = ctypes.windll.user32
        if int(user32.GetForegroundWindow()) == hwnd:
            return
        user32.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
        user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
    except Exception as exc:
        logger.debug("could not hand the foreground back: %s", exc)


class Overlay:
    """Small always-on-top status window that never takes keyboard focus.

    The tk event loop owns the thread that calls run_forever(); show(), hide(), schedule(),
    after() and stop() are safe to call from any thread because they only put work on a
    queue that the tk thread drains. When the config has enabled false every method is a
    no-op and run_forever() returns immediately.
    """

    def __init__(self, config: OverlayConfig) -> None:
        self._config = config
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._root: tk.Tk | None = None
        self._window: tk.Toplevel | None = None
        self._label: tk.Label | None = None
        self._hide_job: str | None = None
        self._styled = False

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def start(self) -> None:
        """Build the hidden overlay window. Must be called on the thread that runs tk."""
        if not self._config.enabled or self._root is not None:
            return
        background, foreground = _STYLES[_DEFAULT_STYLE]
        # tk.Tk() maps its root window before withdraw() can hide it again, and Windows hands
        # that flash the foreground. Remember who had it so it can be given straight back.
        previous = _foreground_window()
        root = tk.Tk()
        root.withdraw()
        window = tk.Toplevel(root)
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.attributes("-alpha", 0.92)
        window.configure(bg=background)
        label = tk.Label(
            window,
            text="",
            font=("Segoe UI", 12),
            bg=background,
            fg=foreground,
            padx=18,
            pady=10,
        )
        label.pack()
        window.withdraw()
        self._root = root
        self._window = window
        self._label = label
        self._apply_no_activate()
        _restore_foreground(previous)
        root.after(_PUMP_INTERVAL_MS, self._pump)

    def stop(self) -> None:
        """Destroy the window and end run_forever()."""
        if not self._config.enabled:
            return
        self._queue.put(self._stop_now)

    def show(self, text: str, *, style: str = "info") -> None:
        """Display text until the next show()/hide(). Unknown styles fall back to "info"."""
        if not self._config.enabled:
            return
        self._queue.put(lambda: self._show_now(text, style))

    def hide(self, after_ms: int | None = None) -> None:
        """Hide the window after after_ms; None uses the configured hide delay, 0 is now."""
        if not self._config.enabled:
            return
        delay = self._config.hide_delay_ms if after_ms is None else after_ms
        self._queue.put(lambda: self._hide_now(max(0, delay)))

    def run_forever(self) -> None:
        """Run the tk main loop on the calling thread until stop() is processed."""
        if not self._config.enabled:
            return
        if self._root is None:
            self.start()
        root = self._root
        if root is None:
            return
        try:
            root.mainloop()
        except KeyboardInterrupt:
            logger.debug("overlay interrupted")

    def schedule(self, fn: Callable[[], None]) -> None:
        """Run fn on the tk thread as soon as the queue is drained."""
        if not self._config.enabled:
            return
        self._queue.put(lambda: self._run_guarded(fn))

    def after(self, delay_ms: int, fn: Callable[[], None]) -> None:
        """Run fn on the tk thread delay_ms from now. Safe to call from any thread."""
        if not self._config.enabled:
            return
        self._queue.put(lambda: self._after_now(max(0, delay_ms), fn))

    def _pump(self) -> None:
        root = self._root
        if root is None:
            return
        while True:
            try:
                action = self._queue.get_nowait()
            except queue.Empty:
                break
            self._run_guarded(action)
            if self._root is None:
                return
        root.after(_PUMP_INTERVAL_MS, self._pump)

    def _run_guarded(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            logger.warning("overlay action failed", exc_info=True)

    def _after_now(self, delay_ms: int, fn: Callable[[], None]) -> None:
        root = self._root
        if root is None:
            return
        root.after(delay_ms, lambda: self._run_guarded(fn))

    def _show_now(self, text: str, style: str) -> None:
        window = self._window
        label = self._label
        if window is None or label is None:
            return
        self._cancel_hide()
        background, foreground = _STYLES.get(style, _STYLES[_DEFAULT_STYLE])
        display = f"{_RECORDING_BULLET}{text}" if style == "recording" else text
        label.configure(text=display, bg=background, fg=foreground)
        window.configure(bg=background)
        window.deiconify()
        if not self._styled:
            self._apply_no_activate()
        self._place(window)
        window.lift()

    def _hide_now(self, delay_ms: int) -> None:
        window = self._window
        root = self._root
        if window is None or root is None:
            return
        self._cancel_hide()
        if delay_ms == 0:
            window.withdraw()
            return
        self._hide_job = root.after(delay_ms, self._withdraw)

    def _withdraw(self) -> None:
        self._hide_job = None
        window = self._window
        if window is not None:
            window.withdraw()

    def _cancel_hide(self) -> None:
        root = self._root
        if root is not None and self._hide_job is not None:
            root.after_cancel(self._hide_job)
        self._hide_job = None

    def _stop_now(self) -> None:
        root = self._root
        self._cancel_hide()
        self._root = None
        self._window = None
        self._label = None
        if root is None:
            return
        try:
            root.quit()
            root.destroy()
        except tk.TclError:
            logger.debug("overlay already destroyed", exc_info=True)

    def _place(self, window: tk.Toplevel) -> None:
        window.update_idletasks()
        width = window.winfo_reqwidth()
        height = window.winfo_reqheight()
        screen_width = window.winfo_screenwidth()
        screen_height = window.winfo_screenheight()
        x = max(0, (screen_width - width) // 2)
        if self._config.position == "top-center":
            y = _SCREEN_MARGIN_PX
        else:
            y = max(0, screen_height - height - _SCREEN_MARGIN_PX)
        window.geometry(f"{width}x{height}+{x}+{y}")

    def _apply_no_activate(self) -> None:
        window = self._window
        if window is None:
            return
        try:
            window.update_idletasks()
            user32 = ctypes.windll.user32
            user32.GetParent.argtypes = (ctypes.c_void_p,)
            user32.GetParent.restype = ctypes.c_void_p
            hwnd = ctypes.c_void_p(window.winfo_id())
            # winfo_id() of an overrideredirect Toplevel is tk's child window; the extended
            # styles only take effect on the real top-level window above it.
            parent = user32.GetParent(hwnd)
            while parent:
                hwnd = ctypes.c_void_p(parent)
                parent = user32.GetParent(hwnd)
            get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            get_long.argtypes = (ctypes.c_void_p, ctypes.c_int)
            get_long.restype = ctypes.c_ssize_t
            set_long.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t)
            set_long.restype = ctypes.c_ssize_t
            style = int(get_long(hwnd, GWL_EXSTYLE))
            set_long(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception:
            logger.warning("overlay may steal focus: no-activate styles failed", exc_info=True)
            return
        self._styled = True
